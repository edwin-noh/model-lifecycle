import hashlib
from pathlib import Path

from model_lifecycle.common import read_checksums
from model_lifecycle.modelcar import (
    ANN_CONTENT_DIGEST,
    ANN_INLAYERPATH,
    default_image,
    layer_files,
    verify_layers,
)


def _manifest(entries: dict[str, str]) -> dict:
    """olot 이 만드는 형식의 manifest (베이스 레이어 하나 + 파일별 레이어)."""
    layers = [{"digest": "sha256:base"}]  # 베이스 이미지 레이어 (annotation 없음)
    for name, digest in entries.items():
        layers.append(
            {
                "annotations": {
                    ANN_INLAYERPATH: f"/models/{name}",
                    ANN_CONTENT_DIGEST: f"sha256:{digest}",
                }
            }
        )
    return {"layers": layers}


def _expected(model_dir: Path) -> dict[str, str]:
    expected = read_checksums(model_dir / "SHA256SUMS")
    expected["SHA256SUMS"] = hashlib.sha256(
        (model_dir / "SHA256SUMS").read_bytes()
    ).hexdigest()
    return expected


def test_layer_files_separates_modelcard(model_dir: Path):
    files, modelcard = layer_files(model_dir, "SHA256SUMS")
    assert modelcard == model_dir / "README.md"
    assert sorted(p.relative_to(model_dir).as_posix() for p in files) == [
        "SHA256SUMS",
        "config.json",
        "model.safetensors",
        "sub/tokenizer.json",
    ]


def test_verify_layers_ok(model_dir: Path):
    result = verify_layers(_manifest(_expected(model_dir)), model_dir, "SHA256SUMS")
    assert result["ok"]
    assert result["layers"] == 5  # 파일 3 + README.md(ModelCarD) + SHA256SUMS


def test_verify_layers_missing_extra_mismatched(model_dir: Path):
    entries = _expected(model_dir)
    del entries["config.json"]
    entries["model.safetensors"] = "0" * 64
    entries["unexpected.bin"] = "1" * 64
    result = verify_layers(_manifest(entries), model_dir, "SHA256SUMS")
    assert not result["ok"]
    assert result["missing"] == ["config.json"]
    assert result["extra"] == ["unexpected.bin"]
    assert result["mismatched"] == ["model.safetensors"]


def test_default_image():
    info = {"source": {"repo_id": "Qwen/Qwen3-4B", "commit_sha": "abcdef0123456789"}}
    assert default_image(info, None) == "localhost/qwen/qwen3-4b:abcdef012345"
    assert (
        default_image(info, "quay.internal/ai/")
        == "quay.internal/ai/qwen/qwen3-4b:abcdef012345"
    )
