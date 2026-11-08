from pathlib import Path

from model_lifecycle.scan import verify_checksums


def test_ok(model_dir: Path):
    result = verify_checksums(model_dir, "SHA256SUMS", exclude=set())
    assert result["ok"]
    assert result["file_count"] == 4
    assert len(result["checksum_file_sha256"]) == 64


def test_mismatched(model_dir: Path):
    (model_dir / "model.safetensors").write_bytes(b"tampered")
    result = verify_checksums(model_dir, "SHA256SUMS", exclude=set())
    assert not result["ok"]
    assert result["mismatched"] == ["model.safetensors"]


def test_missing(model_dir: Path):
    (model_dir / "sub/tokenizer.json").unlink()
    result = verify_checksums(model_dir, "SHA256SUMS", exclude=set())
    assert not result["ok"]
    assert result["missing"] == ["sub/tokenizer.json"]


def test_unlisted_and_exclude(model_dir: Path):
    (model_dir / "extra.py").write_text("print(1)")
    (model_dir / "SCAN_REPORT.json").write_text("{}")
    result = verify_checksums(model_dir, "SHA256SUMS", exclude={"SCAN_REPORT.json"})
    assert not result["ok"]
    assert result["unlisted"] == ["extra.py"]


def test_no_checksum_file(tmp_path: Path):
    result = verify_checksums(tmp_path, "SHA256SUMS", exclude=set())
    assert not result["ok"]
    assert "error" in result
