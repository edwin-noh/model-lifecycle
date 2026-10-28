"""다운로드한 모델 디렉터리를 오픈소스 도구로 정적 검사하고 SCAN_REPORT.json 을 생성한다.

- ModelAudit: 다양한 모델 형식(pickle, PyTorch, safetensors, GGUF, ONNX, Keras 등)
- ModelScan: pickle/H5/SavedModel 위험 연산 교차 확인
- Bandit: 커스텀 코드(*.py, trust_remote_code 대상)

검사 전에 SHA256SUMS 로 무결성을 먼저 확인한다. 모델 파일은 로드하지 않는다.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from model_lifecycle.download import MODEL_INFO_FILE, hash_file, iter_files

log = logging.getLogger("model_lifecycle")

ENV_PREFIX = "HF_SCAN_"
SCHEMA_VERSION = 1
SEVERITIES = ["low", "medium", "high", "critical"]
# 도구별 severity 를 공통 등급으로 변환
SEVERITY_MAP = {
    "modelaudit": {"info": "low", "warning": "medium", "critical": "critical"},
    "modelscan": {s.upper(): s for s in SEVERITIES},
    "bandit": {s.upper(): s for s in SEVERITIES},
}
# modelaudit 는 기본적으로 사용 통계를 외부로 전송하므로 끈다 (폐쇄망 대응)
TOOL_ENV = {"PROMPTFOO_DISABLE_TELEMETRY": "1", "NO_ANALYTICS": "1"}


def verify_checksums(
    model_dir: Path, checksum_file: str, exclude: set[str]
) -> dict[str, Any]:
    """SHA256SUMS 와 실제 파일을 비교한다. 목록에 없는 파일이 추가된 경우도 실패로 본다."""
    path = model_dir / checksum_file
    if not path.is_file():
        return {"ok": False, "error": f"{checksum_file} 파일이 없습니다"}

    expected: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.lstrip("*")] = digest

    mismatched, missing = [], []
    for name, digest in sorted(expected.items()):
        file = model_dir / name
        if not file.is_file():
            missing.append(name)
        elif hash_file(file)[0] != digest:
            mismatched.append(name)

    present = {
        p.as_posix() for p in iter_files(model_dir, exclude={checksum_file, *exclude})
    }
    unlisted = sorted(present - expected.keys())
    return {
        "ok": not (mismatched or missing or unlisted),
        "checksum_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_count": len(expected),
        "mismatched": mismatched,
        "missing": missing,
        "unlisted": unlisted,
    }


def _tool_path(name: str) -> str | None:
    # uv 가상환경의 bin 을 우선 사용한다
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.is_file() else shutil.which(name)


def _tool_version(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def _relative(location: str, model_dir: Path) -> str:
    """도구가 출력한 절대경로를 모델 디렉터리 기준 상대경로로 바꾼다 (/tmp → /private/tmp 대응)."""
    for root in (str(model_dir.resolve()), str(model_dir.absolute())):
        if location.startswith(root):
            return location[len(root) :].lstrip("/")
    return location


def run_tool(
    name: str, args: list[str], output: Path | None, workdir: Path
) -> dict[str, Any]:
    """도구를 실행하고 JSON 결과(output 파일, None 이면 stdout)를 읽는다.

    종료 코드는 도구마다 의미가 달라 결과 판정에 쓰지 않는다.
    """
    exe = _tool_path(name)
    status: dict[str, Any] = {"version": _tool_version(name), "errors": []}
    if exe is None:
        status["errors"].append(
            f"{name} 이(가) 설치되어 있지 않습니다 (uv sync --extra scan)"
        )
        return status
    log.info("%s 실행", name)
    # cwd 를 임시 디렉터리로 두어 현재 디렉터리의 설정 파일(modelscan-settings.toml 등)을 읽지 않게 한다
    proc = subprocess.run(
        [exe, *args],
        cwd=workdir,
        env={**os.environ, **TOOL_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    status["exit_code"] = proc.returncode
    try:
        text = output.read_text(encoding="utf-8") if output else proc.stdout
        status["result"] = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError):
        status["errors"].append(
            f"JSON 결과를 읽을 수 없습니다: {proc.stderr.strip()[-500:]}"
        )
    return status


def scan_modelaudit(model_dir: Path, workdir: Path) -> tuple[dict, list[dict]]:
    # --output 은 macOS 일부 버전에서 ACL 확인 중 오류가 나므로 stdout 으로 받는다
    # HF 유명 모델 severity 하향(whitelist)과 결과 캐시를 끈다
    args = ["scan", str(model_dir), "--format", "json", "--no-whitelist", "--no-cache"]
    status = run_tool("modelaudit", [*args, "-q"], None, workdir)
    result = status.pop("result", None) or {}
    if result and not result.get("files_scanned"):
        status["errors"].append("modelaudit 가 검사한 파일이 없습니다")
    findings = []
    incomplete = []
    for issue in result.get("issues", []):
        details = issue.get("details") or {}
        # 검사하지 못한 파일은 발견 항목이 아니라 도구 오류로 분류한다
        if details.get("analysis_incomplete"):
            incomplete.append(issue)
            continue
        severity = SEVERITY_MAP["modelaudit"].get(issue.get("severity"))
        if severity is None:  # debug
            continue
        findings.append(
            {
                "tool": "modelaudit",
                "severity": severity,
                "rule": issue.get("rule_code") or issue.get("type"),
                "file": _relative(issue.get("location", ""), model_dir),
                "message": issue.get("message"),
            }
        )
    for issue in incomplete:
        message = (
            f"{_relative(issue.get('location', ''), model_dir)}: {issue.get('message')}"
        )
        if issue["details"].get("scan_outcome_reason") == "shard_pin_unavailable":
            # modelaudit 는 분할 파일을 /proc/self/fd 로 고정해서 검사하는데 macOS 에는 없다
            message += " (macOS 미지원, Linux 에서 실행하세요)"
        status["errors"].append(message)
    if result.get("has_errors") and not incomplete:
        status["errors"].append("modelaudit 가 일부 파일 검사 중 오류를 보고했습니다")
    return status, findings


def scan_modelscan(model_dir: Path, workdir: Path) -> tuple[dict, list[dict]]:
    out = workdir / "modelscan.json"
    args = ["scan", "-p", str(model_dir), "-r", "json", "-o", str(out)]
    status = run_tool("modelscan", args, out, workdir)
    result = status.pop("result", None) or {}
    status["errors"] += [
        f"{e.get('source')}: {e.get('description')}" for e in result.get("errors", [])
    ]
    findings = [
        {
            "tool": "modelscan",
            "severity": SEVERITY_MAP["modelscan"].get(issue.get("severity"), "medium"),
            "rule": issue.get("scanner"),
            "file": issue.get("source"),
            "message": issue.get("description"),
        }
        for issue in result.get("issues", [])
    ]
    return status, findings


def scan_bandit(model_dir: Path, workdir: Path) -> tuple[dict, list[dict]]:
    if not any(p.suffix == ".py" for p in iter_files(model_dir, exclude=set())):
        return {
            "version": _tool_version("bandit"),
            "errors": [],
            "skipped": "*.py 없음",
        }, []
    out = workdir / "bandit.json"
    args = ["-r", str(model_dir), "-f", "json", "-o", str(out), "-q"]
    status = run_tool("bandit", [*args, "-x", str(model_dir / ".cache")], out, workdir)
    result = status.pop("result", None) or {}
    status["errors"] += [
        f"{e.get('filename')}: {e.get('reason')}" for e in result.get("errors", [])
    ]
    findings = [
        {
            "tool": "bandit",
            "severity": SEVERITY_MAP["bandit"].get(r.get("issue_severity"), "medium"),
            "rule": r.get("test_id"),
            "file": f"{_relative(r.get('filename', ''), model_dir)}:{r.get('line_number')}",
            "message": f"{r.get('issue_text')} (confidence={r.get('issue_confidence')})",
        }
        for r in result.get("results", [])
    ]
    return status, findings


def _commit_sha(model_dir: Path) -> str | None:
    try:
        info = json.loads((model_dir / MODEL_INFO_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return info.get("source", {}).get("commit_sha") or info.get("commit_sha")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """우선순위: 명령행 인자 > 환경변수 > 기본값."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="다운로드한 모델을 정적 검사하고 SCAN_REPORT.json 을 생성합니다.",
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
        "--report-file",
        default=env(f"{ENV_PREFIX}REPORT_FILE", "SCAN_REPORT.json"),
        help=f"리포트 파일명 (기본: SCAN_REPORT.json, env: {ENV_PREFIX}REPORT_FILE)",
    )
    parser.add_argument(
        "--checksum-file",
        default=env(f"{ENV_PREFIX}CHECKSUM_FILE", "SHA256SUMS"),
        help=f"체크섬 파일명 (기본: SHA256SUMS, env: {ENV_PREFIX}CHECKSUM_FILE)",
    )
    parser.add_argument(
        "--fail-on",
        choices=SEVERITIES,
        default=env(f"{ENV_PREFIX}FAIL_ON", "high"),
        help=f"이 등급 이상 발견 시 실패 (기본: high, env: {ENV_PREFIX}FAIL_ON)",
    )
    args = parser.parse_args(argv)
    if not args.model_dir:
        parser.error(
            f"model_dir 를 인자 또는 {ENV_PREFIX}MODEL_DIR 환경변수로 지정하세요"
        )
    if not args.model_dir.is_dir():
        parser.error(f"디렉터리가 아닙니다: {args.model_dir}")
    return args


def main(argv: list[str] | None = None) -> int:
    """종료 코드: 0 통과, 1 무결성 실패 또는 fail-on 이상 발견, 2 검사 미완료(도구 오류)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    # 도구는 임시 디렉터리를 cwd 로 실행되므로 절대경로로 넘겨야 한다
    model_dir: Path = args.model_dir.resolve()

    log.info("무결성 검증: %s", model_dir / args.checksum_file)
    integrity = verify_checksums(
        model_dir, args.checksum_file, exclude={args.report_file}
    )
    tools: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    if integrity["ok"]:
        with tempfile.TemporaryDirectory() as tmp:
            for name, scanner in (
                ("modelaudit", scan_modelaudit),
                ("modelscan", scan_modelscan),
                ("bandit", scan_bandit),
            ):
                tools[name], found = scanner(model_dir, Path(tmp))
                findings += found
    else:
        log.error("무결성 검증 실패, 검사를 중단합니다: %s", integrity)

    findings.sort(
        key=lambda f: (-SEVERITIES.index(f["severity"]), f["tool"], f["file"])
    )
    threshold = SEVERITIES.index(args.fail_on)
    blocking = [f for f in findings if SEVERITIES.index(f["severity"]) >= threshold]
    tool_errors = {name: t["errors"] for name, t in tools.items() if t["errors"]}
    passed = integrity["ok"] and not blocking and not tool_errors

    report = {
        "schema_version": SCHEMA_VERSION,
        "model_dir": str(model_dir.resolve()),
        "commit_sha": _commit_sha(model_dir),
        "scanned_at": datetime.now(UTC).isoformat(),
        "scanned_by": getpass.getuser(),
        "integrity": integrity,
        "tools": tools,
        "summary": {
            "passed": passed,
            "fail_on": args.fail_on,
            "by_severity": {
                s: c for s, c in Counter(f["severity"] for f in findings).items()
            },
            "blocking_count": len(blocking),
            "tool_errors": tool_errors,
        },
        "findings": findings,
    }
    report_path = model_dir / args.report_file
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    for f in blocking:
        log.error(
            "[%s] %s %s: %s", f["tool"], f["severity"].upper(), f["file"], f["message"]
        )
    for name, errors in tool_errors.items():
        log.warning("%s 검사 미완료: %s", name, errors)
    log.info("발견 항목: %s", report["summary"]["by_severity"] or "없음")
    log.info("리포트: %s", report_path)

    if not integrity["ok"] or blocking:
        log.error("검사 실패")
        return 1
    if tool_errors:
        log.error("일부 도구 검사가 완료되지 않았습니다")
        return 2
    log.info("검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
