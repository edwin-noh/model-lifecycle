"""Hugging Face Hub 모델을 다운로드하고 sha256sum 호환 체크섬 파일을 생성한다.

생성된 체크섬 파일은 이후 다음 명령으로 검증할 수 있다.

    sha256sum -c SHA256SUMS          # Linux
    shasum -a 256 -c SHA256SUMS      # macOS
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.hf_api import RepoSibling

from model_lifecycle.metadata import build_model_info

log = logging.getLogger("model_lifecycle")

CHUNK_SIZE = 8 * 1024 * 1024
ENV_PREFIX = "HF_DOWNLOAD_"
MODEL_INFO_FILE = "MODEL_INFO.json"
# snapshot_download(local_dir=...) 가 남기는 메타데이터 디렉터리
HF_LOCAL_CACHE_DIR = ".cache"


def hash_file(path: Path) -> tuple[str, str]:
    """파일의 (sha256, git blob sha1) 을 한 번의 읽기로 계산한다."""
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1()
    git_sha1.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            sha256.update(chunk)
            git_sha1.update(chunk)
    return sha256.hexdigest(), git_sha1.hexdigest()


def iter_files(root: Path, exclude: set[str]) -> list[Path]:
    """root 하위 파일을 상대경로 기준 정렬해서 반환한다 (HF 로컬 캐시 디렉터리 제외)."""
    files = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] == HF_LOCAL_CACHE_DIR or rel.as_posix() in exclude:
            continue
        if p.is_file():
            files.append(rel)
    return sorted(files, key=lambda r: r.as_posix())


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


def _env_list(name: str) -> list[str] | None:
    """콤마로 구분된 환경변수 값을 리스트로 변환한다 (예: "*.safetensors,*.json")."""
    value = os.environ.get(name)
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()] or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """우선순위: 명령행 인자 > 환경변수 > 기본값."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="Hugging Face 모델을 다운로드하고 SHA256SUMS 파일을 생성합니다.",
        epilog="각 옵션은 괄호 안의 환경변수로도 지정할 수 있습니다. "
        "목록형 환경변수는 콤마로 구분합니다.",
    )
    parser.add_argument(
        "repo_id",
        nargs="?",
        default=env(f"{ENV_PREFIX}REPO_ID"),
        help=f"예: meta-llama/Llama-3.1-8B-Instruct (env: {ENV_PREFIX}REPO_ID)",
    )
    parser.add_argument(
        "--revision",
        default=env(f"{ENV_PREFIX}REVISION", "main"),
        help=f"특정 리비전을 지정합니다 (기본: main, env: {ENV_PREFIX}REVISION)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=env(f"{ENV_PREFIX}OUTPUT_DIR"),
        help=f"저장 경로 (기본: ./models/<owner>__<name>, env: {ENV_PREFIX}OUTPUT_DIR)",
    )
    parser.add_argument(
        "--token",
        default=env("HF_TOKEN"),
        help="HF 토큰 (env: HF_TOKEN, 둘 다 없으면 로그인 캐시 사용)",
    )
    parser.add_argument(
        "--allow-patterns",
        nargs="+",
        default=_env_list(f"{ENV_PREFIX}ALLOW_PATTERNS"),
        help=f'예: "*.safetensors" "*.json" (env: {ENV_PREFIX}ALLOW_PATTERNS)',
    )
    parser.add_argument(
        "--ignore-patterns",
        nargs="+",
        default=_env_list(f"{ENV_PREFIX}IGNORE_PATTERNS"),
        help=f'예: "*.bin" "*.pth" (env: {ENV_PREFIX}IGNORE_PATTERNS)',
    )
    parser.add_argument(
        "--checksum-file",
        default=env(f"{ENV_PREFIX}CHECKSUM_FILE", "SHA256SUMS"),
        help=f"기본: SHA256SUMS (env: {ENV_PREFIX}CHECKSUM_FILE)",
    )
    # argparse 는 문자열 default 에도 type 변환을 적용한다
    parser.add_argument(
        "--max-workers",
        type=int,
        default=env(f"{ENV_PREFIX}MAX_WORKERS", "8"),
        help=f"동시 다운로드 수 (기본: 8, env: {ENV_PREFIX}MAX_WORKERS)",
    )
    args = parser.parse_args(argv)
    if not args.repo_id:
        parser.error(f"repo_id 를 인자 또는 {ENV_PREFIX}REPO_ID 환경변수로 지정하세요")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    args = parse_args(argv)
    output_dir: Path = args.output_dir or Path("models") / args.repo_id.replace(
        "/", "__"
    )

    # revision 을 커밋 sha 로 고정해서 메타데이터 조회와 다운로드가 같은 스냅샷을 보도록 한다.
    api = HfApi(token=args.token)
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
        token=args.token,
        max_workers=args.max_workers,
    )

    siblings = {s.rfilename: s for s in info.siblings or []}
    files = iter_files(output_dir, exclude={args.checksum_file, MODEL_INFO_FILE})

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
        checksum_file=args.checksum_file,
    )
    info_path = output_dir / MODEL_INFO_FILE
    info_path.write_text(json.dumps(model_info, indent=2, ensure_ascii=False) + "\n")
    checksums[MODEL_INFO_FILE] = hash_file(info_path)[0]

    checksum_path = output_dir / args.checksum_file
    with checksum_path.open("w", encoding="utf-8", newline="\n") as f:
        for key in sorted(checksums):
            f.write(f"{checksums[key]}  {key}\n")

    log.info("완료: %s", checksum_path)
    log.info("검증: cd %s && sha256sum -c %s", output_dir, args.checksum_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
