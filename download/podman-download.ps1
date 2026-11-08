<#
.SYNOPSIS
  hf-download 이미지로 Hugging Face 모델을 운영자 로컬 디스크에 받는다 (Windows).

.DESCRIPTION
  모델은 <OutputDir>\<owner>__<name>\ 에 저장되고 SHA256SUMS, MODEL_INFO.json 이 함께 생성된다.
  나머지 인자는 repo_id 와 hf-download 옵션(--revision, --allow-patterns, --ignore-patterns)으로 그대로 전달한다.
    HF_TOKEN           gated/private 모델용 토큰 (환경변수로만 전달)
    HF_DOWNLOAD_IMAGE  이미지 (기본: localhost/model-lifecycle-download:latest)

.EXAMPLE
  .\download\podman-download.ps1 -OutputDir D:\models Qwen/Qwen3-4B-Instruct-2507 --ignore-patterns "original/*"
#>
# [Parameter] 속성을 쓰면 --revision 같은 인자를 PowerShell 이 매개변수로 해석하므로 단순 param + $args 를 쓴다
param([string]$OutputDir = ".\models")

if ($args.Count -eq 0) { Get-Help $PSCommandPath -Detailed; exit 2 }
$Image = if ($env:HF_DOWNLOAD_IMAGE) { $env:HF_DOWNLOAD_IMAGE } else { "localhost/model-lifecycle-download:latest" }

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

$Opts = @("--rm", "--cap-drop", "all", "--security-opt", "no-new-privileges",
          "-e", "USER=$env:USERNAME", "-v", "${OutputDir}:/work/models")
if ($env:HF_TOKEN) { $Opts += @("-e", "HF_TOKEN") }

podman run @Opts $Image @args
exit $LASTEXITCODE
