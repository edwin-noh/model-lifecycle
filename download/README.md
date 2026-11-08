# 모델 다운로드 (인터넷 연결 PC, Podman)

`hf-download` 를 컨테이너로 실행해 Hugging Face 모델을 운영자 로컬 디스크에 받습니다. PC 에는 Podman 만 있으면 됩니다.

| 파일 | 용도 |
|---|---|
| `Containerfile` | hf-download 이미지 (UBI9 + huggingface-hub, 비root) |
| `podman-download.ps1` | 실행 스크립트 (Windows) |
| `podman-download.sh` | 실행 스크립트 (Linux / macOS) |

## 1. 이미지 빌드 (한 번)

저장소 루트에서 실행합니다 (`ghcr.io`, `registry.access.redhat.com`, `pypi.org` 접근 필요).

```bash
podman build -f download/Containerfile -t localhost/model-lifecycle-download:latest .
```

코드를 바꾸면 같은 명령으로 다시 빌드합니다. Windows 는 Podman Desktop 설치 후 podman machine 을 시작해 둡니다.

## 2. 다운로드

```powershell
# Windows
.\download\podman-download.ps1 -OutputDir D:\models Qwen/Qwen3-4B-Instruct-2507

# gated 모델: 토큰은 명령행에 쓰지 않고 환경변수로
$env:HF_TOKEN = "hf_..."
.\download\podman-download.ps1 -OutputDir D:\models google/gemma-4-26b-it --ignore-patterns "original/*"
```

```bash
# Linux / macOS
download/podman-download.sh -o /data/models Qwen/Qwen3-4B-Instruct-2507
HF_TOKEN=hf_... download/podman-download.sh -o /data/models google/gemma-4-26b-it --ignore-patterns "original/*"
```

PowerShell 스크립트 실행이 막혀 있으면 `powershell -ExecutionPolicy Bypass -File .\download\podman-download.ps1 ...` 로 실행합니다.

| 인자 | 설명 |
|---|---|
| `-OutputDir` / `-o` | 저장할 로컬 디렉터리 (기본 `./models`). 모델은 `<디렉터리>/<owner>__<name>/` 에 저장 |
| `repo_id` | 모델 repo (예: `Qwen/Qwen3-4B-Instruct-2507`) |
| `--revision` | 브랜치/태그/커밋 (기본 `main`). 실제 다운로드는 해석된 커밋 sha 로 고정 |
| `--allow-patterns` | 받을 파일 glob (예: `"*.safetensors" "*.json" LICENSE`) |
| `--ignore-patterns` | 제외할 파일 glob (예: `"original/*" "*.pth"`) |

| 환경변수 | 설명 |
|---|---|
| `HF_TOKEN` | gated/private 모델 토큰. 값은 컨테이너에 환경변수로만 전달 |
| `HF_DOWNLOAD_IMAGE` | 다른 이름으로 빌드했거나 레지스트리에서 받은 이미지 (기본 `localhost/model-lifecycle-download:latest`) |

- `MODEL_INFO.json` 의 `downloaded_by` 에는 운영자 계정(Windows `USERNAME`, Linux/macOS `USER`)이 기록됩니다
- 컨테이너는 비root(uid 1001), `--cap-drop all`, `--security-opt no-new-privileges` 로 실행합니다.
  Linux 에서는 `--userns keep-id` 로 받은 파일이 운영자 소유가 되고 SELinux 라벨(`:Z`)을 붙입니다
- 프록시: 호스트의 `HTTP(S)_PROXY` 환경변수는 podman 이 컨테이너에 자동으로 전달합니다 (Windows 는 podman machine 설정)

### 사내 프록시가 TLS 를 검사하는 경우

`SSL certificate verify failed` 가 나면 사내 CA 인증서(PEM)를 마운트해 직접 실행합니다.

```bash
podman run --rm -v /data/models:/work/models -v /path/corp-ca.pem:/tmp/ca.pem:ro \
  -e SSL_CERT_FILE=/tmp/ca.pem -e REQUESTS_CA_BUNDLE=/tmp/ca.pem -e HF_TOKEN \
  localhost/model-lifecycle-download:latest Qwen/Qwen3-4B-Instruct-2507
```

## 3. 반입 전 확인

생성 파일(`SHA256SUMS`, `MODEL_INFO.json`)과 경고 내용은 [../README.md](../README.md#생성-파일) 를 참고하세요.
`.cache/` 는 다운로드 메타데이터라 반입하지 않아도 됩니다.

```powershell
# Windows: 출력이 없으면 모두 일치
cd D:\models\<owner>__<name>
Get-Content SHA256SUMS | ForEach-Object {
  $hash, $file = $_ -split '  ', 2
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower() -ne $hash) { "FAIL $file" }
}
```

```bash
cd /data/models/<owner>__<name> && sha256sum -c SHA256SUMS   # macOS: shasum -a 256 -c SHA256SUMS
```
