#!/usr/bin/env bash
# Podman 컨테이너에서 hf-scan 을 실행한다.
#
# 사용법: scripts/podman-scan.sh [--build] <model_dir> [hf-scan 옵션...]
#   --build              이미지를 강제로 다시 빌드
#   HF_SCAN_IMAGE        사용할 이미지 (기본: localhost/model-lifecycle-scan:<version>)
#   HF_SCAN_REPORT_DIR   리포트 저장 디렉터리 (기본: 모델 디렉터리의 부모)
#
# 모델 디렉터리는 읽기 전용으로 마운트하고, 리포트는 <REPORT_DIR>/<모델명>.SCAN_REPORT.json 에 쓴다.
# 종료 코드는 hf-scan 과 같다: 0 통과, 1 무결성 실패/위험 발견, 2 검사 미완료 (125 는 podman 오류)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${PROJECT_DIR}/pyproject.toml" | head -1)"
IMAGE="${HF_SCAN_IMAGE:-localhost/model-lifecycle-scan:${VERSION}}"

build=false
model_dir=""
scan_args=()
for arg in "$@"; do
    if [[ "${arg}" == "--build" ]]; then
        build=true
    elif [[ -z "${model_dir}" && "${arg}" != -* ]]; then
        model_dir="${arg}"
    else
        scan_args+=("${arg}")
    fi
done

if [[ -z "${model_dir}" ]]; then
    sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}" >&2
    exit 2
fi
if [[ ! -d "${model_dir}" ]]; then
    echo "디렉터리가 아닙니다: ${model_dir}" >&2
    exit 2
fi

model_dir="$(cd "${model_dir}" && pwd)"
model_name="$(basename "${model_dir}")"
report_dir="$(mkdir -p "${HF_SCAN_REPORT_DIR:-$(dirname "${model_dir}")}" && cd "${HF_SCAN_REPORT_DIR:-$(dirname "${model_dir}")}" && pwd)"

if [[ "${build}" == true ]] || ! podman image exists "${IMAGE}"; then
    echo "이미지 빌드: ${IMAGE}" >&2
    podman build --build-arg "VERSION=${VERSION}" -t "${IMAGE}" -f "${PROJECT_DIR}/Containerfile" "${PROJECT_DIR}" >&2
fi

echo "검사: ${model_dir} → ${report_dir}/${model_name}.SCAN_REPORT.json" >&2
# --network none: 검사에 네트워크가 필요 없고, 외부 전송을 원천 차단한다 (폐쇄망 대응)
# :ro 모델은 읽기 전용, :Z 는 SELinux(RHEL) 라벨 지정
# --userns keep-id: 리포트 파일이 호스트 사용자 소유로 생성되도록 한다 (rootless)
status=0
podman run --rm \
    --network none \
    --cap-drop all \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,size=1g \
    --userns keep-id \
    -v "${model_dir}:/models:ro,Z" \
    -v "${report_dir}:/reports:Z" \
    -e "HF_SCAN_REPORT_FILE=/reports/${model_name}.SCAN_REPORT.json" \
    "${IMAGE}" ${scan_args[@]+"${scan_args[@]}"} || status=$?

if [[ ${status} -eq 125 ]]; then
    echo "podman 실행 오류 (macOS 라면 podman machine 에 공유된 경로(/Users 등) 아래의 모델인지 확인하세요)" >&2
fi
exit "${status}"
