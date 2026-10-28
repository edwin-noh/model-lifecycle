# hf-scan 정적 검사 이미지 (ModelAudit / ModelScan / Bandit)
#
# 빌드: podman build -t localhost/model-lifecycle-scan:0.1.0 .
# 실행: scripts/podman-scan.sh <model_dir>

ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-312-minimal
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.15

FROM ${UV_IMAGE} AS uv

FROM ${BASE_IMAGE} AS builder
USER 0
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /build

# 의존성 레이어를 먼저 만들어 소스 변경 시 재설치를 피한다
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --extra scan --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --extra scan --no-dev --no-editable

FROM ${BASE_IMAGE}
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="model-lifecycle-scan" \
      org.opencontainers.image.description="Static security scan for downloaded Hugging Face models" \
      org.opencontainers.image.version="${VERSION}"

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMPTFOO_DISABLE_TELEMETRY=1 \
    NO_ANALYTICS=1 \
    HF_HUB_OFFLINE=1 \
    HF_SCAN_MODEL_DIR=/models

USER 1001
WORKDIR /tmp
ENTRYPOINT ["hf-scan"]
