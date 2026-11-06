"""검사를 통과한 모델 디렉터리로 KServe ModelCar(OCI) 이미지를 만든다 (olot 방식).

Red Hat 공식 ModelCar 와 같은 구조로 만든다: ubi9-micro 베이스 위에 파일 하나를 무압축 레이어 하나로 붙인다.
컨테이너 빌드를 하지 않으므로 빌드 디스크는 모델의 약 2배(원본 + 레이어 blob)이고 podman VM 을 쓰지 않는다.

1. 검사 게이트: SHA256SUMS 무결성, 스캔 리포트 통과 여부, 리포트가 지금 내용을 검사한 것인지 확인
2. skopeo 로 베이스 이미지를 OCI 레이아웃으로 받고, olot 으로 SHA256SUMS 에 있는 파일만 /models 아래 레이어로 붙인다
3. 출처/라이선스/검사 결과를 config 라벨과 manifest annotation 으로 남긴다 (스캔 리포트는 digest 만)
4. 레이어 annotation 의 파일 해시(olot.layer.content.digest)를 SHA256SUMS 와 대조한다
5. (선택) 레지스트리 push(digest 보존) 또는 oci-archive 저장
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
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
# ModelCar 사이드카가 sh/ln/sleep 을 실행하므로 이들이 있는 ubi-micro 를 쓴다
DEFAULT_BASE_IMAGE = "registry.access.redhat.com/ubi9/ubi-micro:latest"
LABEL_PREFIX = "io.kblife"
MODELCARD_FILE = "README.md"
LAYOUT_TAG = "latest"
# olot 이 레이어마다 다는 annotation
ANN_CONTENT_DIGEST = "olot.layer.content.digest"
ANN_INLAYERPATH = "olot.layer.content.inlayerpath"
MODELS_PREFIX = "/models/"


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


def layer_files(model_dir: Path, checksum_file: str) -> tuple[list[Path], Path | None]:
    """SHA256SUMS 에 있는 파일과 SHA256SUMS 자신을 레이어 대상으로 돌려준다.

    README.md 는 ModelCarD 로 따로 돌려준다 (olot 규칙: modelcard 는 model_files 에 넣지 않는다).
    """
    names = [checksum_file]
    for line in (model_dir / checksum_file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            names.append(line.split(maxsplit=1)[1].lstrip("*"))
    modelcard = model_dir / MODELCARD_FILE if MODELCARD_FILE in names else None
    files = [model_dir / n for n in names if n != MODELCARD_FILE]
    return files, modelcard


def read_layout_manifest(layout: Path) -> tuple[dict[str, Any], str]:
    """OCI 레이아웃의 (단일 아키텍처) manifest 와 그 digest 를 읽는다."""
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    digest = index["manifests"][0]["digest"]
    blob = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
    manifest = json.loads(blob.read_text(encoding="utf-8"))
    if "manifests" in manifest:
        raise ValueError(f"단일 아키텍처 manifest 가 아닙니다: {digest}")
    return manifest, digest


def verify_layers(
    manifest: dict[str, Any], model_dir: Path, checksum_file: str
) -> dict[str, Any]:
    """레이어 annotation 의 파일 해시가 SHA256SUMS(와 SHA256SUMS 자신)와 정확히 같은지 확인한다.

    annotation 이 없는 레이어(베이스 이미지)는 대상이 아니다. 데이터를 다시 읽지 않고 manifest 만으로 검증한다.
    """
    expected: dict[str, str] = {}
    for line in (model_dir / checksum_file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.lstrip("*")] = digest
    expected[checksum_file] = hashlib.sha256(
        (model_dir / checksum_file).read_bytes()
    ).hexdigest()

    actual: dict[str, str] = {}
    for layer in manifest["layers"]:
        ann = layer.get("annotations") or {}
        if ANN_INLAYERPATH not in ann:
            continue
        name = ann[ANN_INLAYERPATH].removeprefix(MODELS_PREFIX)
        actual[name] = ann.get(ANN_CONTENT_DIGEST, "").removeprefix("sha256:")

    mismatched = sorted(n for n in expected.keys() & actual.keys() if expected[n] != actual[n])
    return {
        "ok": expected == actual,
        "layers": len(actual),
        "missing": sorted(expected.keys() - actual.keys()),
        "extra": sorted(actual.keys() - expected.keys()),
        "mismatched": mismatched,
    }


def skopeo(*args: str) -> None:
    cmd = ["skopeo", *args]
    log.info("$ %s", shlex.join(cmd))
    subprocess.run(cmd, check=True)


def build_layout(
    layout: Path,
    model_dir: Path,
    checksum_file: str,
    base_image: str,
    platform: str,
    labels: dict[str, str],
) -> int:
    """베이스 이미지를 OCI 레이아웃으로 받고 모델 파일을 레이어로 붙인다. 붙인 파일 수를 돌려준다."""
    from olot.basics import oci_layers_on_top

    os_name, _, arch = platform.partition("/")
    # Red Hat 이미지의 서명은 OCI 레이아웃에 저장할 수 없으므로 떼고 받는다 (레이어를 붙이면 어차피 새 이미지가 된다)
    skopeo(
        "--override-os", os_name, "--override-arch", arch,
        "copy", "--remove-signatures",
        f"docker://{base_image}", f"oci:{layout}:{LAYOUT_TAG}",
    )  # fmt: skip
    files, modelcard = layer_files(model_dir, checksum_file)
    log.info(
        "레이어 추가: 파일 %d개%s", len(files), " + ModelCarD" if modelcard else ""
    )
    oci_layers_on_top(
        layout,
        files,
        modelcard,
        labels=labels,
        annotations=labels,
        root_dir=model_dir,
    )
    return len(files) + (1 if modelcard else 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """우선순위: 명령행 인자 > 환경변수 > 기본값."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="검사를 통과한 모델 디렉터리로 KServe ModelCar 이미지를 만듭니다 (olot).",
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
    parser.add_argument(
        "--keep-layout",
        action="store_true",
        help="--push/--archive 후에도 작업용 OCI 레이아웃(<모델명>.oci-layout)을 남긴다 "
        "(둘 다 없으면 레이아웃이 결과물이므로 항상 남긴다)",
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
    if not args.platform.startswith("linux/") or args.platform.count("/") != 1:
        parser.error(f"--platform 은 linux/<arch> 형식이어야 합니다: {args.platform}")
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

    if shutil.which("skopeo") is None:
        log.error(
            "skopeo 가 설치되어 있지 않습니다 (macOS: brew install skopeo, RHEL: dnf install skopeo)"
        )
        return 2

    log.info("검사 게이트: %s (리포트 %s)", model_dir, report_path)
    try:
        info, report = check_gate(model_dir, report_path, args.checksum_file)
    except GateError as e:
        log.error("검사 게이트 실패, 이미지를 만들지 않습니다: %s", e)
        return 1

    image = args.image or default_image(info, args.registry)
    labels = build_labels(info, report, report_path)
    # 모델과 같은 디스크에 둔다 (레이어 blob 이 모델 크기만큼 필요)
    layout = model_dir.parent / f"{model_dir.name}.oci-layout"
    if layout.exists():
        log.error("작업 레이아웃이 이미 있습니다. 지우고 다시 실행하세요: %s", layout)
        return 2

    # --push/--archive 가 없으면 레이아웃 자체가 결과물이다
    keep = args.keep_layout or not (args.push or args.archive)
    ok = False
    try:
        log.info("이미지 구성: %s (%s)", image, args.platform)
        count = build_layout(
            layout, model_dir, args.checksum_file, args.base_image, args.platform, labels
        )
        manifest, manifest_digest = read_layout_manifest(layout)

        result = verify_layers(manifest, model_dir, args.checksum_file)
        if not result["ok"]:
            log.error("레이어 검증 실패 (SHA256SUMS 와 다름): %s", result)
            return 2
        log.info(
            "레이어 검증 통과: 모델 파일 %d개 = SHA256SUMS (manifest %s)",
            count,
            manifest_digest,
        )

        if args.archive:
            archive = args.archive.resolve()
            log.info("oci-archive 저장: %s", archive)
            skopeo(
                "copy", "--preserve-digests",
                f"oci:{layout}:{LAYOUT_TAG}", f"oci-archive:{archive}:{LAYOUT_TAG}",
            )  # fmt: skip

        if args.push:
            with tempfile.TemporaryDirectory() as tmp:
                digestfile = Path(tmp) / "digest"
                # 레지스트리 쪽에서 레이어를 다시 압축하면 digest 가 바뀌므로 실패하게 한다
                skopeo(
                    "copy", "--preserve-digests", "--digestfile", str(digestfile),
                    f"oci:{layout}:{LAYOUT_TAG}", f"docker://{image}",
                )  # fmt: skip
                digest = digestfile.read_text().strip()
            if digest != manifest_digest:
                log.error(
                    "push 된 digest 가 검증한 manifest 와 다릅니다: %s != %s",
                    digest,
                    manifest_digest,
                )
                return 2
            repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            log.info(
                "push 완료. InferenceService storageUri: oci://%s@%s", repo, digest
            )
        ok = True
    except subprocess.CalledProcessError as e:
        log.error("skopeo 명령 실패 (exit %d)", e.returncode)
        return 2
    finally:
        if layout.exists() and not (ok and keep):
            shutil.rmtree(layout)

    log.info("완료: %s", image)
    if keep:
        log.info(
            "OCI 레이아웃: %s (push: skopeo copy --preserve-digests oci:%s:%s docker://<image>)",
            layout,
            layout,
            LAYOUT_TAG,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
