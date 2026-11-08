#!/usr/bin/env bash
# hf-download 이미지로 Hugging Face 모델을 운영자 로컬 디스크에 받는다 (Linux / macOS).
#
# 사용법: download/podman-download.sh [-o <저장 디렉터리>] <repo_id> [--revision ..] [--allow-patterns ..] [--ignore-patterns ..]
#   -o <dir>           저장 디렉터리 (기본: ./models). 모델은 <dir>/<owner>__<name>/ 에 저장
#   HF_TOKEN           gated/private 모델용 토큰 (환경변수로만 전달)
#   HF_DOWNLOAD_IMAGE  이미지 (기본: localhost/model-lifecycle-download:latest)
set -euo pipefail

IMAGE="${HF_DOWNLOAD_IMAGE:-localhost/model-lifecycle-download:latest}"
output_dir="./models"
if [[ "${1:-}" == "-o" ]]; then
    output_dir="$2"
    shift 2
fi
if [[ $# -eq 0 ]]; then
    sed -n '2,7s/^# \{0,1\}//p' "${BASH_SOURCE[0]}" >&2
    exit 2
fi
mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd)"

opts=(--rm --cap-drop all --security-opt no-new-privileges -e "USER=${USER:-$(id -un)}")
if [[ "$(uname -s)" == "Linux" ]]; then
    # rootless 에서 파일이 운영자 소유가 되게 하고 SELinux 라벨을 붙인다
    opts+=(--userns keep-id -v "${output_dir}:/work/models:Z")
else
    opts+=(-v "${output_dir}:/work/models")
fi
if [[ -n "${HF_TOKEN:-}" ]]; then
    opts+=(-e HF_TOKEN)
fi

podman run "${opts[@]}" "${IMAGE}" "$@"
