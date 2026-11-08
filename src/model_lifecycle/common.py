"""hf-download / hf-scan / hf-modelcar 가 함께 쓰는 상수와 파일/체크섬 유틸리티.

huggingface_hub 를 import 하지 않는다 (hf-scan 컨테이너, hf-modelcar 는 Hub 에 접근하지 않음).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_CHECKSUM_FILE = "SHA256SUMS"
MODEL_INFO_FILE = "MODEL_INFO.json"
# snapshot_download(local_dir=...) 가 남기는 메타데이터 디렉터리
HF_LOCAL_CACHE_DIR = ".cache"
HUB_URL = "https://huggingface.co"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )


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


def read_checksums(path: Path) -> dict[str, str]:
    """sha256sum 형식(`<hash>  <path>`, 바이너리 모드 `*` 접두 허용) 파일을 {상대경로: sha256} 으로 읽는다."""
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            checksums[name.lstrip("*")] = digest
    return checksums
