# RHOAI 3.5 배포 (KServe RawDeployment + ModelCar)

`hf-modelcar` 로 만든 ModelCar 이미지를 RHOAI 3.5 에서 KServe RawDeployment 모드로 서빙합니다.
OpenShift GitOps(Argo CD)로 배포하고, Ops팀과 AI팀이 폴더를 나눠 관리합니다.
나중에 리포를 나눌 때는 `ops/`, `ai/` 을 각각 리포로 옮기고 Argo CD 의 `repoURL`/`path` 만 바꿉니다.

| 폴더 | 담당 | 내용 | 문서 |
|---|---|---|---|
| [`ops/`](ops/) | Ops팀 | 플랫폼 설정, 모델 네임스페이스, 네트워크 격리, RBAC, vLLM 런타임, Argo CD 설정 | [ops/README.md](ops/README.md) |
| [`ai/`](ai/) | AI팀 | InferenceService | [ai/README.md](ai/README.md) |

## 등급과 출처

모델은 네임스페이스 하나(`kbl-models`)에 두고, 등급과 출처는 **라벨과 Nexus repo** 로 구분합니다.
등급을 바꿔도 네임스페이스가 그대로라 엔드포인트(`<모델>-predictor.kbl-models.svc`)가 바뀌지 않습니다.
네임스페이스를 나눈다면 등급이 아니라 소비 주체·업무 기준으로 나눕니다.

| 구분 | 표시 | 실제 근거 | 함께 맞출 값 |
|---|---|---|---|
| 런타임 등급 | ServingRuntime·ISVC 라벨 `kbl.io/runtime-tier: ga\|fast\|upstream` | Nexus `vllm-ga` / `vllm-fast` / `vllm-upstream` repo (push 권한) | 이미지 repo, 런타임 이름 `vllm-<등급>-<버전>`, `kbl.io/support-status` |
| 모델 출처 | ISVC 라벨 `kbl.io/model-source: validated\|community` | Nexus `models-validated` / `models-community` repo (push 권한) | `storageUri` repo |

- 라벨은 누구나 붙일 수 있으므로 "GA = 승인된 이미지" 는 **Nexus repo 의 push 권한**으로 보장합니다.
  라벨과 실제 이미지/모델 경로가 맞는지는 리뷰(PR)에서 확인합니다
- 이미지와 모델은 모두 digest(`@sha256:`)로만 참조합니다 (태그는 바뀔 수 있음)
- Model Registry 는 하나만 두고 custom property(출처, 검증 상태, 런타임 등급)로 구분합니다.
  출처·라이선스 값은 `MODEL_INFO.json` 에서 가져옵니다

## 역할 분리

| | Ops팀 | AI팀 |
|---|---|---|
| Git | `ops/` | `ai/` |
| Argo CD | AppProject `kbl-ops` (클러스터 전체) | AppProject `kbl-ai` (kbl-models 의 InferenceService 만) |
| 클러스터 권한 | cluster-admin | Role `kbl-ai-developer`: ISVC 생성·수정·삭제, 나머지 조회, port-forward |
| Nexus | `vllm-*` push, `models-validated` push(승인자) | `models-community` push |
| 요청 | ← 런타임 추가, 다른 네임스페이스 접근, GPU quota | |
| 에이전트 팀 | 소비 네임스페이스 라벨(`kbl.io/model-consumer=true`) 부여 | [모델 카탈로그](ai/CATALOG.md) 관리 |

AI팀이 ISVC 를 직접 수정하므로, ISVC 에서 쓰지 않을 필드(`model.image`, 커스텀 컨테이너, SA 변경, 외부 Route 등)는
리뷰에서 확인합니다. 목록은 [ai/README.md](ai/README.md#규칙) 에 있습니다.

## 에이전트 개발

에이전트 개발자는 [모델 카탈로그](ai/CATALOG.md)에서 모델 이름과 OpenAI 호환 엔드포인트를 확인합니다.
카탈로그는 AI팀 ISVC 파일에서 `scripts/model-catalog.py` 로 생성하므로 실제 배포와 같습니다.
호출하려면 에이전트 네임스페이스에 Ops팀이 `kbl.io/model-consumer=true` 라벨을 붙여야 합니다.

```python
from openai import OpenAI
client = OpenAI(base_url="http://qwen3-4b-instruct-predictor.kbl-models.svc:8080/v1", api_key="unused")
client.chat.completions.create(model="qwen3-4b-instruct", messages=[...], tools=[...])
```

## 다음 단계

- **리포 분리**: `ops/`, `ai/` 을 각 리포로 옮기고 `ops/gitops/` 의 `sourceRepos`, `repoURL`, `path` 변경
- **Gateway / MaaS**: 앞단에서 모델 이름 기준으로 라우팅하면 뒤쪽 배치(네임스페이스, 등급)가 바뀌어도
  호출하는 에이전트는 영향을 받지 않습니다. 인증도 이 앞단에서 처리합니다
- **이미지 서명**: Nexus `vllm-ga` / `models-validated` 이미지에 cosign 서명 + `ClusterImagePolicy`

## RHOAI 3.5 문서 대조

| 항목 | 상태 | 근거 (RHOAI 3.5 문서) |
|---|---|---|
| ISVC `serving.kserve.io/deploymentMode: RawDeployment` | 확인 | *Deploying models* — 2.1 About KServe deployment modes, ISVC 예시 |
| DSC `datasciencecluster.opendatahub.io/v2`, `kserve.rawDeploymentServiceConfig` (Headless/Headed) | 확인 | *Installing and uninstalling … in a disconnected environment*, *Enabling AI safety with guardrails* 의 DSC 예시 |
| ModelCar(`storageUri: oci://`) | 확인 | *Deploying models* — 1.1 OCI-based model storage |
| 토큰 인증 ISVC 에 OAuth proxy 사이드카 추가 | 확인 | *Installing … disconnected environment* — 3.5.1.1 Configure OAuth proxy sidecar resources for KServe |
| ISVC `security.opendatahub.io/enable-auth` | **3.5 미확인** | 3.5 문서에는 LLMInferenceService/Guardrails 예시만 있음 |
| HardwareProfile `infrastructure.opendatahub.io/v1`, ISVC `opendatahub.io/hardware-profile-name/-namespace` | **3.5 미확인** | 3.5 문서에 YAML 예시 없음 |
| `inferenceservice-config` 의 `storageInitializer.enableModelcar` | **3.5 미확인** | 3.5 문서에 없음 |

미확인 항목은 처음 적용할 때 `oc explain` 과 `oc apply --dry-run=server` 로 확인하세요.
