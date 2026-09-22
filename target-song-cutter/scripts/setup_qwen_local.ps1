[CmdletBinding()]
param(
    [switch]$SkipModels,
    [switch]$Include17B
)

$ErrorActionPreference = "Stop"
$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BootstrapPython = Join-Path $WorkspaceRoot ".python312\python.exe"
$RuntimeRoot = Join-Path $env:LOCALAPPDATA "target-song-cutter\qwen-asr-runtime"
$RuntimePython = Join-Path $RuntimeRoot "Scripts\python.exe"
$Requirements = Join-Path $WorkspaceRoot "target-song-cutter\requirements-qwen-local.txt"
$ModelRoot = Join-Path $WorkspaceRoot "models\qwen3-asr"

if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) {
    throw "Missing workspace Python 3.12 runtime: $BootstrapPython"
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not available; cannot create the ASCII-only Qwen runtime path."
}

Write-Host "Creating isolated Qwen runtime at $RuntimeRoot"
& $BootstrapPython -m venv --system-site-packages $RuntimeRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to create the Qwen runtime." }

& $RuntimePython -X utf8 -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to update pip in the Qwen runtime." }
& $RuntimePython -X utf8 -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install the Qwen runtime dependencies." }

if (-not $SkipModels) {
    $Models = @(
        @{ Id = "Qwen/Qwen3-ASR-0.6B"; Directory = "Qwen3-ASR-0.6B" },
        @{ Id = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"; Directory = "fsmn-vad" },
        @{ Id = "iic/speech_campplus_sv_zh-cn_16k-common"; Directory = "cam++" }
    )
    if ($Include17B) {
        $Models += @{ Id = "Qwen/Qwen3-ASR-1.7B"; Directory = "Qwen3-ASR-1.7B" }
    }
    New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
    foreach ($Model in $Models) {
        $Destination = Join-Path $ModelRoot $Model.Directory
        Write-Host "Downloading $($Model.Id) to $Destination"
        & $BootstrapPython -X utf8 -m modelscope.cli.cli download $Model.Id --local-dir $Destination --max-workers 4
        if ($LASTEXITCODE -ne 0) { throw "Failed to download $($Model.Id)." }
    }
}

& $RuntimePython -I -X utf8 -c "from qwen_asr import Qwen3ASRModel; import funasr, torch, transformers; print('Qwen local runtime OK', transformers.__version__, torch.cuda.is_available(), Qwen3ASRModel.__name__)"
if ($LASTEXITCODE -ne 0) { throw "The Qwen runtime import check failed." }

Write-Host "Local Qwen3-ASR setup completed. No API key is required."
