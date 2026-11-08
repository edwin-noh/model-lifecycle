import hashlib
from pathlib import Path

import pytest


def write_model(
    root: Path, files: dict[str, bytes], checksum_file: str = "SHA256SUMS"
) -> Path:
    """files 를 만들고 그 sha256 으로 체크섬 파일을 쓴다."""
    root.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, data in sorted(files.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    (root / checksum_file).write_text("".join(lines), encoding="utf-8")
    return root


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    return write_model(
        tmp_path / "model",
        {
            "config.json": b"{}",
            "model.safetensors": b"weights",
            "README.md": b"# card\n",
            "sub/tokenizer.json": b"tok",
        },
    )
