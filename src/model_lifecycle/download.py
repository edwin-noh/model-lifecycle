"""Hugging Face Hub 모델을 다운로드하고 sha256sum 호환 체크섬 파일을 생성한다.

생성된 체크섬 파일은 이후 다음 명령으로 검증할 수 있다.

    sha256sum -c SHA256SUMS          # Linux
    shasum -a 256 -c SHA256SUMS      # macOS
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.hf_api import RepoSibling

from model_lifecycle.common import (
    DEFAULT_CHECKSUM_FILE,
    MODEL_INFO_FILE,
    hash_file,
    iter_files,
    setup_logging,
)
from model_lifecycle.metadata import build_model_info

log = logging.getLogger("model_lifecycle")

MAX_WORKERS = 8


def verify_against_hub(
    rel: str, sha256: str, git_sha1: str, sibling: RepoSibling | None
) -> bool:
    """Hub 메타데이터와 로컬 해시를 비교한다. LFS 파일은 sha256, 일반 파일은 git blob id."""
    if sibling is None:
        return True  # Hub에 없는 파일 (예: MODEL_INFO.json)
    if sibling.lfs is not None:
        expected, actual = sibling.lfs.sha256, sha256
    elif sibling.blob_id is not None:
        expected, actual = sibling.blob_id, git_sha1
    else:
        log.warning("Hub 해시 정보 없음, 교차검증 생략: %s", rel)
        return True
    if expected != actual:
        log.error("해시 불일치: %s (expected=%s, actual=%s)", rel, expected, actual)
        return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hugging Face 모델을 ./models/<owner>__<name> 에 다운로드하고 "
        f"{DEFAULT_CHECKSUM_FILE}, {MODEL_INFO_FILE} 를 생성합니다.",
        epilog="gated/private 모델은 HF_TOKEN 환경변수(또는 hf auth login 캐시)를 사용합니다.",
    )
    parser.add_argument("repo_id", help="예: Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument(
        "--revision", default="main", help="브랜치/태그/커밋 (기본: main)"
    )
    parser.add_argument(
        "--allow-patterns",
        nargs="+",
        help='받을 파일 glob. 예: "*.safetensors" "*.json"',
    )
    parser.add_argument(
        "--ignore-patterns",
        nargs="+",
        help='제외할 파일 glob. 예: "original/*" "*.pth"',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    args = parse_args(argv)
    output_dir = Path("models") / args.repo_id.replace("/", "__")
    checksum_file = DEFAULT_CHECKSUM_FILE

    # revision 을 커밋 sha 로 고정해서 메타데이터 조회와 다운로드가 같은 스냅샷을 보도록 한다.
    # 토큰은 huggingface_hub 가 HF_TOKEN 환경변수 / 로그인 캐시에서 직접 읽는다.
    api = HfApi()
    info = api.model_info(args.repo_id, revision=args.revision, files_metadata=True)
    commit = info.sha
    log.info("%s @ %s (commit %s)", args.repo_id, args.revision, commit)

    log.info("다운로드 시작 → %s", output_dir)
    snapshot_download(
        args.repo_id,
        revision=commit,
        local_dir=output_dir,
        allow_patterns=args.allow_patterns,
        ignore_patterns=args.ignore_patterns,
        max_workers=MAX_WORKERS,
    )

    siblings = {s.rfilename: s for s in info.siblings or []}
    files = iter_files(output_dir, exclude={checksum_file, MODEL_INFO_FILE})

    log.info("체크섬 계산 및 Hub 메타데이터 교차검증 (%d개 파일)", len(files))
    checksums: dict[str, str] = {}
    ok = True
    for rel in files:
        key = rel.as_posix()
        sha256, git_sha1 = hash_file(output_dir / rel)
        ok &= verify_against_hub(key, sha256, git_sha1, siblings.get(key))
        checksums[key] = sha256
        log.info("  %s  %s", sha256, key)
    if not ok:
        log.error(
            "Hub 해시와 일치하지 않는 파일이 있습니다. 체크섬 파일을 생성하지 않습니다."
        )
        return 1

    log.info("모델 메타데이터 수집")
    model_info = build_model_info(
        api=api,
        repo_id=args.repo_id,
        revision=args.revision,
        commit=commit,
        output_dir=output_dir,
        checksums=checksums,
        allow_patterns=args.allow_patterns,
        ignore_patterns=args.ignore_patterns,
        checksum_file=checksum_file,
    )
    info_path = output_dir / MODEL_INFO_FILE
    info_path.write_text(json.dumps(model_info, indent=2, ensure_ascii=False) + "\n")
    checksums[MODEL_INFO_FILE] = hash_file(info_path)[0]

    checksum_path = output_dir / checksum_file
    with checksum_path.open("w", encoding="utf-8", newline="\n") as f:
        for key in sorted(checksums):
            f.write(f"{checksums[key]}  {key}\n")

    log.info("완료: %s", checksum_path)
    log.info("검증: cd %s && sha256sum -c %s", output_dir, checksum_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
