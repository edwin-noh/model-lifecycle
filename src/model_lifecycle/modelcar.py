"""검사를 통과한 모델 디렉터리로 KServe ModelCar(OCI) 이미지를 만든다.

1. 검사 게이트: SHA256SUMS 무결성, 스캔 리포트 통과 여부, 리포트가 지금 내용을 검사한 것인지 확인
2. SHA256SUMS 에 있는 파일만 이미지 /models 에 넣고, 스캔 리포트는 /metadata 에 넣는다
3. 출처/라이선스/검사 결과를 이미지 라벨로 남긴다
4. 빌드된 이미지 내용을 sha256sum -c 로 다시 검증한다
5. (선택) 레지스트리 push 또는 oci-archive 저장
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from model_lifecycle.download import MODEL_INFO_FILE
from model_lifecycle.scan import verify_checksums

log = logging.getLogger("model_lifecycle")

ENV_PREFIX = "HF_MODELCAR_"
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONTAINERFILE = PROJECT_DIR / "modelcar" / "Containerfile"
DEFAULT_BASE_IMAGE = "registry.access.redhat.com/ubi9/ubi-micro:latest"
# 빌드 후 검증용 (호스트 아키텍처로 실행). 베이스(ubi-micro)와 다른 이미지를 써서
# 교차 빌드로 받은 다른 아키텍처의 같은 태그와 섞이지 않게 한다
VERIFY_IMAGE = "registry.access.redhat.com/ubi9/ubi-minimal:latest"
LABEL_PREFIX = "io.kblife"


class GateError(Exception):
    """검사 게이트 실패."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise GateError(f"파일이 없습니다: {path}") from e
    except json.JSONDecodeError as e:
        raise GateError(f"JSON 형식이 아닙니다: {path}") from e


def check_gate(
    model_dir: Path, report_path: Path, checksum_file: str
) -> tuple[dict, dict]:
    """빌드 전 검사 게이트. 통과하면 (MODEL_INFO, 스캔 리포트) 를 반환한다."""
    integrity = verify_checksums(model_dir, checksum_file, exclude=set())
    if not integrity["ok"]:
        raise GateError(f"무결성 검증 실패: {integrity}")

    report = _load_json(report_path)
    info = _load_json(model_dir / MODEL_INFO_FILE)

    if not report.get("summary", {}).get("passed"):
        raise GateError(f"스캔을 통과하지 않은 리포트입니다: {report.get('summary')}")
    scanned = report.get("integrity", {}).get("checksum_file_sha256")
    if scanned != integrity["checksum_file_sha256"]:
        raise GateError(
            f"리포트가 지금 모델 내용을 검사한 것이 아닙니다 "
            f"(리포트 {checksum_file}={scanned}, 현재={integrity['checksum_file_sha256']})"
        )
    commit = info.get("source", {}).get("commit_sha")
    if report.get("commit_sha") != commit:
        raise GateError(
            f"commit 불일치: 리포트={report.get('commit_sha')}, MODEL_INFO={commit}"
        )
    return info, report


def _escape_pattern(name: str) -> str:
    # ignorefile 패턴의 glob 메타문자를 이스케이프한다
    return re.sub(r"([*?\[\]\\])", r"\\\1", name)


def write_ignorefile(path: Path, model_dir: Path, checksum_file: str) -> list[str]:
    """SHA256SUMS 에 있는 파일만 빌드 컨텍스트에 포함하는 화이트리스트 ignorefile 을 만든다."""
    files = [checksum_file]
    for line in (model_dir / checksum_file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            files.append(line.split(maxsplit=1)[1].lstrip("*"))
    path.write_text("*\n" + "".join(f"!{_escape_pattern(f)}\n" for f in files))
    return files


def default_image(info: dict[str, Any], registry: str | None) -> str:
    repo = info["source"]["repo_id"].lower()
    tag = info["source"]["commit_sha"][:12]
    return f"{registry.rstrip('/') if registry else 'localhost'}/models/{repo}:{tag}"


def build_labels(
    info: dict[str, Any], report: dict[str, Any], report_path: Path
) -> dict[str, str]:
    src, lic, tech = info["source"], info.get("license", {}), info.get("technical", {})
    labels = {
        "org.opencontainers.image.title": src["repo_id"],
        "org.opencontainers.image.description": f"KServe ModelCar for {src['repo_id']}",
        "org.opencontainers.image.source": f"https://huggingface.co/{src['repo_id']}",
        "org.opencontainers.image.revision": src["commit_sha"],
        "org.opencontainers.image.version": src.get("revision") or "",
        "org.opencontainers.image.licenses": lic.get("id") or "",
        "org.opencontainers.image.created": datetime.now(UTC).isoformat(),
        f"{LABEL_PREFIX}.model.repo-id": src["repo_id"],
        f"{LABEL_PREFIX}.model.commit-sha": src["commit_sha"],
        f"{LABEL_PREFIX}.model.license": lic.get("id") or "",
        f"{LABEL_PREFIX}.model.parameters": str(tech.get("parameters") or ""),
        f"{LABEL_PREFIX}.model.sha256sums-digest": report["integrity"][
            "checksum_file_sha256"
        ],
        f"{LABEL_PREFIX}.scan.passed": str(report["summary"]["passed"]).lower(),
        f"{LABEL_PREFIX}.scan.scanned-at": report.get("scanned_at") or "",
        f"{LABEL_PREFIX}.scan.report-digest": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
    }
    return {k: v for k, v in labels.items() if v}


def podman(*args: str) -> None:
    cmd = ["podman", *args]
    log.info("$ %s", shlex.join(cmd))
    subprocess.run(cmd, check=True)


def _host_platform() -> str:
    arch = platform.machine().lower()
    return "linux/" + {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """우선순위: 명령행 인자 > 환경변수 > 기본값."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="검사를 통과한 모델 디렉터리로 KServe ModelCar 이미지를 만듭니다.",
        epilog="각 옵션은 괄호 안의 환경변수로도 지정할 수 있습니다.",
    )
    parser.add_argument(
        "model_dir",
        nargs="?",
        type=Path,
        default=env(f"{ENV_PREFIX}MODEL_DIR"),
        help=f"hf-download 로 받은 모델 디렉터리 (env: {ENV_PREFIX}MODEL_DIR)",
    )
    parser.add_argument(
        "--scan-report",
        type=Path,
        default=env(f"{ENV_PREFIX}SCAN_REPORT"),
        help="스캔 리포트 (기본: <부모>/<모델명>.SCAN_REPORT.json, "
        f"env: {ENV_PREFIX}SCAN_REPORT)",
    )
    parser.add_argument(
        "--registry",
        default=env(f"{ENV_PREFIX}REGISTRY"),
        help=f"레지스트리 (예: quay.internal/ai, env: {ENV_PREFIX}REGISTRY)",
    )
    parser.add_argument(
        "--image",
        default=env(f"{ENV_PREFIX}IMAGE"),
        help="이미지 전체 이름 (기본: <registry|localhost>/models/<repo_id>:<commit 12자>, "
        f"env: {ENV_PREFIX}IMAGE)",
    )
    parser.add_argument(
        "--platform",
        default=env(f"{ENV_PREFIX}PLATFORM", "linux/amd64"),
        help=f"클러스터 노드 아키텍처 (기본: linux/amd64, env: {ENV_PREFIX}PLATFORM)",
    )
    parser.add_argument(
        "--base-image",
        default=env(f"{ENV_PREFIX}BASE_IMAGE", DEFAULT_BASE_IMAGE),
        help=f"sh/ln/sleep 이 있는 베이스 이미지 (env: {ENV_PREFIX}BASE_IMAGE)",
    )
    parser.add_argument(
        "--containerfile",
        type=Path,
        default=env(f"{ENV_PREFIX}CONTAINERFILE", str(DEFAULT_CONTAINERFILE)),
        help=f"Containerfile 경로 (env: {ENV_PREFIX}CONTAINERFILE)",
    )
    parser.add_argument(
        "--checksum-file",
        default=env(f"{ENV_PREFIX}CHECKSUM_FILE", "SHA256SUMS"),
        help=f"체크섬 파일명 (기본: SHA256SUMS, env: {ENV_PREFIX}CHECKSUM_FILE)",
    )
    parser.add_argument("--push", action="store_true", help="빌드 후 레지스트리에 push")
    parser.add_argument(
        "--archive",
        type=Path,
        default=env(f"{ENV_PREFIX}ARCHIVE"),
        help=f"oci-archive 로 저장할 경로 (폐쇄망 반입용, env: {ENV_PREFIX}ARCHIVE)",
    )
    args = parser.parse_args(argv)
    if not args.model_dir:
        parser.error(
            f"model_dir 를 인자 또는 {ENV_PREFIX}MODEL_DIR 환경변수로 지정하세요"
        )
    if not args.model_dir.is_dir():
        parser.error(f"디렉터리가 아닙니다: {args.model_dir}")
    if args.push and not (args.registry or args.image):
        parser.error("--push 에는 --registry 또는 --image 가 필요합니다")
    return args


def main(argv: list[str] | None = None) -> int:
    """종료 코드: 0 성공, 1 검사 게이트 실패, 2 빌드/검증/push 실패."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    model_dir: Path = args.model_dir.resolve()
    report_path: Path = (
        args.scan_report or model_dir.parent / f"{model_dir.name}.SCAN_REPORT.json"
    ).resolve()

    if shutil.which("podman") is None:
        log.error("podman 이 설치되어 있지 않습니다")
        return 2

    log.info("검사 게이트: %s (리포트 %s)", model_dir, report_path)
    try:
        info, report = check_gate(model_dir, report_path, args.checksum_file)
    except GateError as e:
        log.error("검사 게이트 실패, 이미지를 만들지 않습니다: %s", e)
        return 1

    image = args.image or default_image(info, args.registry)
    labels = build_labels(info, report, report_path)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            ignorefile = Path(tmp) / "ignorefile"
            files = write_ignorefile(ignorefile, model_dir, args.checksum_file)
            metadata = Path(tmp) / "metadata"
            metadata.mkdir()
            shutil.copy(report_path, metadata / "SCAN_REPORT.json")

            log.info(
                "이미지 빌드: %s (%s, 파일 %d개)", image, args.platform, len(files)
            )
            label_args = [a for k, v in labels.items() for a in ("--label", f"{k}={v}")]
            podman(
                "build",
                "--platform", args.platform,
                "--build-arg", f"BASE_IMAGE={args.base_image}",
                "--build-context", f"metadata={metadata}",
                "--ignorefile", str(ignorefile),
                "-f", str(args.containerfile),
                *label_args,
                "-t", image,
                str(model_dir),
            )  # fmt: skip

        # 이미지를 실행하지 않고 파일시스템만 마운트하므로 아키텍처와 무관하게 검증된다
        log.info("이미지 내용 검증 (sha256sum -c)")
        podman(
            "run", "--rm", "--network", "none",
            "--platform", _host_platform(),
            "--mount", f"type=image,src={image},dst=/check",
            VERIFY_IMAGE,
            "sh", "-c", f"cd /check/models && sha256sum -c --quiet {shlex.quote(args.checksum_file)}"
            " && test -f /check/metadata/SCAN_REPORT.json",
        )  # fmt: skip

        if args.archive:
            archive = args.archive.resolve()
            log.info("oci-archive 저장: %s", archive)
            podman("save", "--format", "oci-archive", "-o", str(archive), image)

        if args.push:
            with tempfile.TemporaryDirectory() as tmp:
                digestfile = Path(tmp) / "digest"
                podman("push", "--digestfile", str(digestfile), image)
                digest = digestfile.read_text().strip()
            repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            log.info(
                "push 완료. InferenceService storageUri: oci://%s@%s", repo, digest
            )
    except subprocess.CalledProcessError as e:
        log.error("podman 명령 실패 (exit %d)", e.returncode)
        return 2

    log.info("완료: %s", image)
    if not args.push:
        log.info(
            "배포 시에는 push 후 출력되는 digest(oci://...@sha256:...)로 지정하세요"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
