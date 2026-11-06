# model-lifecycle

Hugging Face Hub 모델을 다운로드하고, 추후 `sha256sum -c` 로 무결성을 검증할 수 있는
`SHA256SUMS` 파일을 생성합니다.

## 설치

```bash
uv sync
```

## 사용법

```bash
# 기본: ./models/<owner>__<name> 에 저장
uv run hf-download sshleifer/tiny-gpt2

# 특정 revision, safetensors/json 만 다운로드
uv run hf-download meta-llama/Llama-3.1-8B-Instruct \
  --revision main \
  --allow-patterns "*.safetensors" "*.json" \
  --output-dir ./models/llama-3.1-8b-instruct
```

gated/private 모델은 `--token` 또는 `HF_TOKEN` 환경변수(`hf auth login` 캐시도 가능)를 사용합니다.

값의 우선순위는 **명령행 인자 > 환경변수 > 기본값** 입니다.

| 옵션 | 환경변수 | 설명 |
|---|---|---|
| `repo_id` (위치 인자) | `HF_DOWNLOAD_REPO_ID` | 모델 repo (필수: 둘 중 하나) |
| `--revision` | `HF_DOWNLOAD_REVISION` | 브랜치/태그/커밋 (기본 `main`). 실제 다운로드는 해석된 커밋 sha 로 고정 |
| `--output-dir` | `HF_DOWNLOAD_OUTPUT_DIR` | 저장 경로 (기본 `./models/<owner>__<name>`) |
| `--token` | `HF_TOKEN` | HF 토큰 (둘 다 없으면 로그인 캐시) |
| `--allow-patterns` | `HF_DOWNLOAD_ALLOW_PATTERNS` | 다운로드할 파일 glob (환경변수는 콤마 구분) |
| `--ignore-patterns` | `HF_DOWNLOAD_IGNORE_PATTERNS` | 제외할 파일 glob (환경변수는 콤마 구분) |
| `--checksum-file` | `HF_DOWNLOAD_CHECKSUM_FILE` | 체크섬 파일명 (기본 `SHA256SUMS`) |
| `--max-workers` | `HF_DOWNLOAD_MAX_WORKERS` | 동시 다운로드 수 (기본 8) |

```bash
# 환경변수만으로 실행 (컨테이너/파이프라인용)
export HF_DOWNLOAD_REPO_ID=Qwen/Qwen2.5-0.5B-Instruct
export HF_DOWNLOAD_ALLOW_PATTERNS="*.safetensors,*.json,LICENSE"
uv run hf-download
```

## 생성 파일

- `SHA256SUMS` — `sha256sum` 호환 형식(`<hash>  <path>`), 모델 디렉터리 기준 상대경로
- `MODEL_INFO.json` — 사내 반입 심사용 메타데이터 (SHA256SUMS 에 포함됨)

### MODEL_INFO.json 구성

| 섹션 | 내용 |
|---|---|
| `source` | repo_id, URL(커밋 고정), 작성자, revision, commit sha, 생성/수정일, 다운로드 일시·사용자 |
| `license` | model card 의 license/name/link, gated 여부, LICENSE·NOTICE·USE_POLICY 파일 sha256, 상위(base) 모델 라이선스 |
| `lineage` | 상위 모델과 관계(finetune/quantized/adapter/merge), 학습 데이터셋, 언어, arxiv 논문 |
| `technical` | 아키텍처, 파라미터 수(dtype별), torch_dtype, context length, 용량, 파일 형식, chat template 유무 |
| `security` | HF 보안 스캔 결과, pickle 기반 파일, 커스텀 코드(`trust_remote_code` 필요 여부) |
| `download` | allow/ignore 패턴, 파일 수, 체크섬 파일명 |
| `approval` | **수동 기입란** — 요청자, 사용 목적, 검토자, 라이선스 검토 결과, 상업적 이용 가능 여부, 제한 사항, 승인일 |

다음과 같은 경우 경고를 출력합니다 (다운로드는 계속 진행):
라이선스 미기재/`other`, LICENSE 파일 미포함, gated 모델, 상위 모델과 라이선스 상이,
pickle 파일 포함, 커스텀 코드 포함, HF 보안 스캔 미완료/이슈.

> model card 의 license 값은 업로더가 기재한 값입니다. 반드시 LICENSE 원문을 함께 검토하세요.
> `approval` 을 채우면 MODEL_INFO.json 해시가 바뀌므로, 승인 기록은 별도 파일로 관리하거나
> 기입 후 `sha256sum MODEL_INFO.json` 으로 SHA256SUMS 를 갱신하세요.

체크섬 파일을 만들기 전에 다운로드한 각 파일을 Hub 메타데이터와 교차검증합니다
(LFS 파일은 sha256, 일반 파일은 git blob id). 하나라도 일치하지 않으면 종료 코드 1로 실패합니다.
`local_dir` 메타데이터 디렉터리(`.cache/`)는 체크섬 대상에서 제외됩니다.

## 무결성 검증

```bash
cd models/<name>
sha256sum -c SHA256SUMS          # Linux
shasum -a 256 -c SHA256SUMS      # macOS
```

## 정적 검사 (`hf-scan`)

다운로드한 모델 디렉터리를 오픈소스 도구로 검사하고 리포트(JSON)를 생성합니다.
모델 파일은 로드하지 않고 정적으로만 분석합니다. **검사는 Podman 컨테이너(Linux)에서 실행합니다.**

```bash
scripts/podman-scan.sh models/<name>                  # 이미지가 없으면 자동 빌드
scripts/podman-scan.sh --build models/<name>          # 코드 변경 후 이미지 재빌드
scripts/podman-scan.sh models/<name> --fail-on medium # 뒤의 옵션은 hf-scan 에 그대로 전달
```

결과: `models/<name>.SCAN_REPORT.json` (모델 디렉터리 옆). 종료 코드는 hf-scan 과 같습니다.

| 도구 | 라이선스 | 역할 |
|---|---|---|
| [ModelAudit](https://github.com/promptfoo/modelaudit) | MIT | 주 스캐너. pickle, PyTorch, safetensors, GGUF, ONNX, Keras 등 |
| [ModelScan](https://github.com/protectai/modelscan) | Apache-2.0 | pickle / H5 / SavedModel 위험 연산 교차 확인 (safetensors 는 대상 아님) |
| [Bandit](https://github.com/PyCQA/bandit) | Apache-2.0 | 커스텀 코드(`*.py`, `trust_remote_code` 대상) 검사. `*.py` 가 없으면 생략 |

동작 순서:
1. `SHA256SUMS` 로 무결성 검증. 해시 불일치, 누락 파일, **목록에 없는 추가 파일**이 있으면 검사하지 않고 실패
2. 세 도구를 실행하고, 도구별 severity 를 `low < medium < high < critical` 로 통일
3. 리포트 작성 (검사 대상인 SHA256SUMS 의 sha256 과 commit sha 를 기록해 결과를 특정 모델 내용에 연결)

종료 코드: `0` 통과 / `1` 무결성 실패 또는 `--fail-on` 이상 발견 / `2` 일부 도구 검사 미완료 / `125` podman 오류

### 컨테이너 구성

- `Containerfile`: Red Hat UBI 9 (`ubi9/python-312-minimal`) 기반 multi-stage 빌드.
  uv 로 `uv.lock` 그대로(`--frozen`) 설치한 가상환경만 최종 이미지에 복사합니다. 비root(uid 1001) 실행
- `scripts/podman-scan.sh` 실행 옵션

| 옵션 | 이유 |
|---|---|
| `-v <model_dir>:/models:ro,Z` | 모델은 **읽기 전용** 마운트 (검사가 모델을 바꿀 수 없음). `Z` 는 RHEL SELinux 라벨 |
| `-v <report_dir>:/reports:Z` | 리포트만 쓰기 가능. 기본은 모델 디렉터리의 부모, `HF_SCAN_REPORT_DIR` 로 변경 |
| `--network none` | 검사에 네트워크 불필요. 외부 전송(텔레메트리 등) 원천 차단 |
| `--read-only --tmpfs /tmp` | 컨테이너 파일시스템 변경 불가 |
| `--cap-drop all`, `--security-opt no-new-privileges` | 권한 최소화 |
| `--userns keep-id` | rootless 에서 리포트 파일이 호스트 사용자 소유로 생성 |

환경변수: `HF_SCAN_IMAGE` (기본 `localhost/model-lifecycle-scan:<version>`), `HF_SCAN_REPORT_DIR`

> **macOS:** `podman machine start` 가 필요하고, podman machine 에 공유된 경로(`/Users` 등) 아래의 모델만
> 마운트할 수 있습니다 (`/tmp` 등은 125 오류).
> 호스트에서 직접 `uv sync --extra scan && uv run hf-scan models/<name>` 도 가능하지만, ModelAudit(0.2.54)이
> macOS 에서는 분할 파일(`model-0000X-of-0000Y.safetensors`)을 검사하지 못합니다 (종료 코드 2, `macOS 미지원` 표시).

### hf-scan 옵션 (컨테이너 안에서 실행되는 CLI)

| 옵션 | 환경변수 | 설명 |
|---|---|---|
| `model_dir` (위치 인자) | `HF_SCAN_MODEL_DIR` | 검사할 모델 디렉터리 (컨테이너에서는 `/models`) |
| `--fail-on` | `HF_SCAN_FAIL_ON` | 이 등급 이상 발견 시 실패 (기본 `high`) |
| `--report-file` | `HF_SCAN_REPORT_FILE` | 리포트 경로. 상대경로는 모델 디렉터리 기준 (기본 `SCAN_REPORT.json`) |
| `--checksum-file` | `HF_SCAN_CHECKSUM_FILE` | 체크섬 파일명 (기본 `SHA256SUMS`) |

> ModelAudit 은 기본적으로 사용 통계를 외부로 전송합니다. 이미지와 `hf-scan` 모두
> `PROMPTFOO_DISABLE_TELEMETRY=1` 을 설정해 이를 끕니다.

## ModelCar 이미지 만들기 (`hf-modelcar`)

검사를 통과한 모델 디렉터리를 KServe ModelCar(OCI) 이미지로 만듭니다.
[olot](https://github.com/containers/olot) 으로 `ubi9/ubi-micro` 위에 **파일 하나를 무압축 레이어 하나**로 붙입니다.
Red Hat 공식 ModelCar(`registry.redhat.io/rhai/modelcar-*`)와 같은 구조입니다.
컨테이너 빌드를 하지 않으므로 podman 이 필요 없고, 빌드 디스크는 **모델의 약 2배**(원본 + 레이어 blob, 호스트 디스크)입니다.

**skopeo 가 필요합니다** (macOS: `brew install skopeo`, RHEL: `dnf install skopeo`). push 전에 `skopeo login <registry>`.

```bash
uv run hf-modelcar models/<name>                                   # OCI 레이아웃만 만듦 (models/<name>.oci-layout)
uv run hf-modelcar models/<name> --registry quay.internal/ai --push  # 빌드 + push, digest 출력
uv run hf-modelcar models/<name> --archive /path/<name>.oci.tar     # 폐쇄망 반입용 파일로 저장
```

1. **검사 게이트** — 하나라도 실패하면 이미지를 만들지 않습니다 (종료 코드 1)
   - `SHA256SUMS` 와 실제 파일이 일치 (추가/누락 파일 없음)
   - 스캔 리포트(`models/<name>.SCAN_REPORT.json`)가 `passed: true`
   - 리포트에 기록된 SHA256SUMS 의 sha256 이 지금과 같음 (검사한 내용 그대로인지)
   - 리포트와 `MODEL_INFO.json` 의 commit sha 가 같음
2. **베이스** — `skopeo` 로 `--base-image`(기본 `ubi9/ubi-micro`)를 `--platform`(기본 `linux/amd64`) 하나만 OCI 레이아웃으로 받습니다.
   서명은 떼고 받습니다 (레이어를 붙이면 새 이미지가 됨). Mac(arm64)에서도 에뮬레이션 없이 amd64 이미지가 만들어집니다
3. **레이어** — `SHA256SUMS` 에 있는 파일과 `SHA256SUMS` 자신만 `/models/<상대경로>` 에 하나씩 무압축 레이어로 붙입니다
   (`.cache/` 등은 제외, 파일 소유자 root·권한 0664 로 OpenShift 임의 UID 에서 읽기 가능).
   `README.md` 는 ModelCarD(gzip 레이어, `io.opendatahub.layers.modelcard`)가 됩니다.
   레이어마다 `olot.layer.content.digest`(파일 sha256) annotation 이 붙습니다.
   이미지 이름 기본값: `<registry|localhost>/models/<repo_id 소문자>:<commit 앞 12자>`
4. **라벨** — 출처, commit, 라이선스, 파라미터 수, SHA256SUMS digest, 스캔 결과/리포트 digest 를
   config 라벨과 manifest annotation 에 모두 남깁니다 (스캔 리포트 원본은 이미지에 넣지 않음)
   ```bash
   skopeo inspect --config docker://<image> | python3 -c \
     'import json,sys; [print(k, v) for k, v in json.load(sys.stdin)["config"]["Labels"].items() if k.startswith("io.kblife")]'
   ```
5. **레이어 검증** — 데이터를 다시 읽지 않고, manifest 의 레이어 annotation(파일 경로·해시)이 `SHA256SUMS` 와
   정확히 같은지 확인합니다 (추가/누락/해시 불일치면 종료 코드 2)
6. **push / archive** — `--preserve-digests` 로 복사하므로 레이어가 다시 압축되어 digest 가 바뀌면 실패합니다.
   `--push` 는 push 된 digest 가 검증한 manifest 와 같은지 확인한 뒤 InferenceService 에 넣을 `oci://<repo>@sha256:...` 를 출력합니다.
   폐쇄망 내부에서는 `skopeo copy --preserve-digests oci-archive:<name>.oci.tar:latest docker://<내부 레지스트리>/<repo>:<tag>` 로 올립니다
   (digest 가 그대로라 반입 전에 기록한 digest 를 그대로 씁니다)

작업 레이아웃(`models/<name>.oci-layout`)은 `--push`/`--archive` 후 지웁니다 (`--keep-layout` 으로 보존).
둘 다 없으면 레이아웃이 결과물이므로 남깁니다.

| 옵션 | 환경변수 | 설명 |
|---|---|---|
| `model_dir` | `HF_MODELCAR_MODEL_DIR` | 모델 디렉터리 |
| `--scan-report` | `HF_MODELCAR_SCAN_REPORT` | 기본 `<부모>/<모델명>.SCAN_REPORT.json` |
| `--registry` | `HF_MODELCAR_REGISTRY` | 이미지 이름 앞에 붙일 레지스트리 |
| `--image` | `HF_MODELCAR_IMAGE` | 이미지 전체 이름 (지정 시 registry 무시) |
| `--platform` | `HF_MODELCAR_PLATFORM` | `linux/<arch>`, 기본 `linux/amd64` |
| `--base-image` | `HF_MODELCAR_BASE_IMAGE` | `sh`/`ln`/`sleep` 이 있어야 함 (기본 `ubi9/ubi-micro`) |
| `--checksum-file` | `HF_MODELCAR_CHECKSUM_FILE` | 기본 `SHA256SUMS` |
| `--archive` | `HF_MODELCAR_ARCHIVE` | oci-archive 저장 경로 |
| `--push` | | 빌드 후 push (`--registry` 또는 `--image` 필요) |
| `--keep-layout` | | push/archive 후에도 OCI 레이아웃 보존 |

종료 코드: `0` 성공 / `1` 검사 게이트 실패 / `2` 빌드·검증·push 실패 (skopeo 없음 포함)

Red Hat `rhai/modelcar-*` 처럼 이미 빌드된 ModelCar 이미지를 폐쇄망으로 옮기는 절차는 [docs/modelcar-import.md](docs/modelcar-import.md) 를 참고하세요.

> 배포 시 InferenceService `storageUri` 는 태그가 아니라 **digest** (`oci://...@sha256:...`)로 지정하세요.
> 태그는 바뀔 수 있지만 digest 는 검사한 이미지와 정확히 일치합니다.
> RHOAI 에서 ModelCar 가 꺼져 있다면 `inferenceservice-config` ConfigMap 의 `storageInitializer.enableModelcar` 를 확인하세요.

### 다음 단계 (제안)

- **이미지 서명**: `cosign sign` 또는 `skopeo copy --sign-by-sigstore` 로 승인된 이미지에 서명하고,
  OpenShift `ClusterImagePolicy`(sigstore 검증)로 모델 네임스페이스에서 서명된 이미지만 실행되게 합니다.
  반입 승인 → 서명 → 클러스터 검증으로 "검사하지 않은 모델은 배포 불가"를 강제할 수 있습니다.
- **레지스트리 취약점 스캔**(Quay/Clair): 모델 이미지에서는 베이스(ubi-micro) 패키지만 대상이 됩니다.

## RHOAI 배포

검증한 모델을 RHOAI 3.5(KServe Standard)에 GitOps 로 배포하는 매니페스트는 [`deploy/`](deploy/README.md) 를 참고하세요.

- `deploy/ops/`: Ops팀. 플랫폼 설정, 모델 네임스페이스,
  네트워크 격리, RBAC, vLLM 런타임, Argo CD 설정
- `deploy/ai/`: AI팀. InferenceService
- 런타임 등급(ga/fast/upstream)과 모델 출처(validated/community)는 라벨 + Nexus repo 로 구분
- 에이전트 개발자용 모델 목록: [`deploy/ai/CATALOG.md`](deploy/ai/CATALOG.md) (`scripts/model-catalog.py` 로 생성)
