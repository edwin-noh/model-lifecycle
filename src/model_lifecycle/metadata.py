"""사내 반입 심사용 모델 메타데이터(MODEL_INFO.json)를 수집한다.

Hub API(model card, 계보, 보안 스캔 등)와 다운로드된 로컬 파일을 함께 조사한다.
위험 신호는 경고로만 남기고, 최종 판단은 사내 검토자가 `approval` 항목을 채워서 한다.
"""

from __future__ import annotations

import getpass
import json
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import huggingface_hub
from huggingface_hub import HfApi
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.hf_api import ModelInfo

log = logging.getLogger("model_lifecycle")

SCHEMA_VERSION = 1
HUB_URL = "https://huggingface.co"

EXPAND_FIELDS = [
    "author",
    "baseModels",
    "cardData",
    "config",
    "createdAt",
    "gated",
    "lastModified",
    "library_name",
    "pipeline_tag",
    "safetensors",
    "sha",
    "tags",
]
LICENSE_FILE_PREFIXES = ("LICENSE", "LICENCE", "NOTICE", "USE_POLICY")
# 로드 시 임의 코드가 실행될 수 있는 pickle 기반 포맷
PICKLE_SUFFIXES = {".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt"}


def fetch_hub_metadata(
    api: HfApi, repo_id: str, commit: str
) -> tuple[ModelInfo, dict[str, Any] | None]:
    """model card/계보/기술 정보와 보안 스캔 결과를 조회한다 (둘은 함께 요청할 수 없음)."""
    info = api.model_info(repo_id, revision=commit, expand=EXPAND_FIELDS)
    try:
        scan = api.model_info(
            repo_id, revision=commit, securityStatus=True
        ).security_repo_status
    except HfHubHTTPError as e:  # 보안 스캔 조회 실패가 다운로드를 막지 않도록 한다
        log.warning("HF 보안 스캔 결과 조회 실패: %s", e)
        scan = None
    return info, scan


def fetch_base_model_licenses(api: HfApi, base_ids: list[str]) -> list[dict[str, Any]]:
    """상위 모델의 라이선스를 조회한다. 파생 모델은 상위 라이선스 조건을 상속할 수 있다."""
    result = []
    for base_id in base_ids:
        try:
            card = api.model_info(base_id, expand=["cardData"]).card_data
            card = card.to_dict() if card else {}
            result.append(
                {
                    "repo_id": base_id,
                    "license": card.get("license"),
                    "license_name": card.get("license_name"),
                    "license_link": card.get("license_link"),
                }
            )
        except HfHubHTTPError as e:
            log.warning("상위 모델 라이선스 조회 실패: %s (%s)", base_id, e)
            result.append({"repo_id": base_id, "license": None, "error": str(e)})
    return result


def _load_local_config(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "config.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("config.json 파싱 실패")
        return {}


def _context_length(config: dict[str, Any]) -> int | None:
    # 멀티모달 모델은 text_config 아래에 있는 경우가 있다
    for cfg in (config, config.get("text_config") or {}):
        for key in (
            "max_position_embeddings",
            "max_sequence_length",
            "n_positions",
            "seq_length",
        ):
            if isinstance(cfg.get(key), int):
                return cfg[key]
    return None


def build_model_info(
    *,
    api: HfApi,
    repo_id: str,
    revision: str,
    commit: str,
    output_dir: Path,
    checksums: dict[str, str],
    allow_patterns: list[str] | None,
    ignore_patterns: list[str] | None,
    checksum_file: str,
) -> dict[str, Any]:
    """MODEL_INFO.json 내용을 만든다. checksums 는 {상대경로: sha256} (다운로드된 파일 전체)."""
    info, scan = fetch_hub_metadata(api, repo_id, commit)
    card = info.card_data.to_dict() if info.card_data else {}
    tags = info.tags or []
    hub_config = info.config or {}
    local_config = _load_local_config(output_dir)
    paths = [PurePosixPath(p) for p in checksums]

    base = info.base_models or {}
    base_ids = [m["id"] for m in base.get("models", []) if "id" in m]

    license_files = [
        {"path": p.as_posix(), "sha256": checksums[p.as_posix()]}
        for p in paths
        if p.name.upper().startswith(LICENSE_FILE_PREFIXES)
    ]
    pickle_files = [p.as_posix() for p in paths if p.suffix.lower() in PICKLE_SUFFIXES]
    custom_code_files = [p.as_posix() for p in paths if p.suffix == ".py"]
    auto_map = local_config.get("auto_map") or hub_config.get("auto_map")
    tokenizer_config = hub_config.get("tokenizer_config") or {}

    model_info = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "repo_id": repo_id,
            "url": f"{HUB_URL}/{repo_id}/tree/{commit}",
            "author": info.author,
            "revision": revision,
            "commit_sha": commit,
            "created_at": info.created_at.isoformat() if info.created_at else None,
            "last_modified": info.last_modified.isoformat()
            if info.last_modified
            else None,
            "downloaded_at": datetime.now(UTC).isoformat(),
            "downloaded_by": getpass.getuser(),
            "huggingface_hub_version": huggingface_hub.__version__,
        },
        "license": {
            "id": card.get("license"),
            "name": card.get("license_name"),
            "link": card.get("license_link"),
            "gated": info.gated,
            "license_files": license_files,
            "base_model_licenses": fetch_base_model_licenses(api, base_ids),
        },
        "lineage": {
            "base_models": {"relation": base.get("relation"), "models": base_ids}
            if base
            else None,
            "datasets": card.get("datasets"),
            "languages": card.get("language"),
            "papers": [
                t.removeprefix("arxiv:") for t in tags if t.startswith("arxiv:")
            ],
        },
        "technical": {
            "architectures": local_config.get("architectures")
            or hub_config.get("architectures"),
            "model_type": local_config.get("model_type")
            or hub_config.get("model_type"),
            "pipeline_tag": info.pipeline_tag,
            "library_name": info.library_name,
            "parameters": info.safetensors.total if info.safetensors else None,
            "parameters_by_dtype": info.safetensors.parameters
            if info.safetensors
            else None,
            "torch_dtype": local_config.get("torch_dtype") or local_config.get("dtype"),
            "context_length": _context_length(local_config),
            "total_size_bytes": sum((output_dir / p).stat().st_size for p in checksums),
            "file_formats": sorted({p.suffix.lower() for p in paths if p.suffix}),
            "has_chat_template": bool(tokenizer_config.get("chat_template"))
            or any(p.name == "chat_template.jinja" for p in paths),
        },
        "security": {
            "hf_scan": scan,
            "pickle_files": pickle_files,
            "requires_trust_remote_code": bool(custom_code_files or auto_map),
            "custom_code_files": custom_code_files,
            "auto_map": auto_map,
        },
        "download": {
            "allow_patterns": allow_patterns,
            "ignore_patterns": ignore_patterns,
            "file_count": len(checksums),
            "checksum_file": checksum_file,
        },
        # 사내 검토자가 수동으로 기입
        "approval": {
            "requester": None,
            "purpose": None,
            "reviewer": None,
            "license_review": None,
            "commercial_use_allowed": None,
            "restrictions": None,
            "approved_at": None,
        },
    }
    warn_risks(model_info)
    return model_info


def warn_risks(model_info: dict[str, Any]) -> None:
    """반입 심사 시 주의가 필요한 항목을 경고로 출력한다."""
    lic = model_info["license"]
    sec = model_info["security"]
    if lic["id"] in (None, "other", "unknown"):
        log.warning(
            "라이선스 확인 필요: id=%s name=%s link=%s",
            lic["id"],
            lic["name"],
            lic["link"],
        )
    if not lic["license_files"]:
        log.warning("LICENSE 파일이 다운로드되지 않았습니다 (allow/ignore 패턴 확인)")
    if lic["gated"]:
        log.warning(
            "gated 모델입니다 (%s): 이용 약관 동의 조건을 검토하세요", lic["gated"]
        )
    for b in lic["base_model_licenses"]:
        if b.get("license") != lic["id"]:
            log.warning(
                "상위 모델 라이선스가 다릅니다: %s → %s", b["repo_id"], b.get("license")
            )
    if sec["pickle_files"]:
        log.warning(
            "pickle 기반 파일 포함 (로드 시 코드 실행 위험): %s", sec["pickle_files"]
        )
    if sec["requires_trust_remote_code"]:
        log.warning(
            "커스텀 코드 포함 (trust_remote_code 필요): %s",
            sec["custom_code_files"] or sec["auto_map"],
        )
    scan = sec["hf_scan"]
    if scan is None or not scan.get("scansDone"):
        log.warning("HF 보안 스캔 결과가 없거나 완료되지 않았습니다")
    elif scan.get("filesWithIssues"):
        log.warning("HF 보안 스캔 이슈: %s", scan["filesWithIssues"])
