from pathlib import Path

from model_lifecycle.common import hash_file, iter_files, read_checksums


def test_hash_file_sha256_and_git_blob(tmp_path: Path):
    f = tmp_path / "hello"
    f.write_bytes(b"hello\n")
    sha256, git_sha1 = hash_file(f)
    assert sha256 == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
    # git hash-object 와 같은 값
    assert git_sha1 == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_read_checksums_formats(tmp_path: Path):
    f = tmp_path / "SHA256SUMS"
    f.write_text(
        "aaa  a.json\n\nbbb *b.bin\nccc  dir/name with space.txt\n", encoding="utf-8"
    )
    assert read_checksums(f) == {
        "a.json": "aaa",
        "b.bin": "bbb",
        "dir/name with space.txt": "ccc",
    }


def test_iter_files_skips_hf_cache_and_exclude(tmp_path: Path):
    for rel in ["b.txt", "a/c.txt", ".cache/huggingface/x.lock", "SHA256SUMS"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    files = iter_files(tmp_path, exclude={"SHA256SUMS"})
    assert [p.as_posix() for p in files] == ["a/c.txt", "b.txt"]
