param()
$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $PackageRoot

function Test-Python312([string]$Executable, [string[]]$PrefixArgs) {
    try {
        & $Executable @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python312 {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher -and (Test-Python312 $PyLauncher.Source @('-3.12'))) {
        return @{ Executable = $PyLauncher.Source; Prefix = @('-3.12') }
    }
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        'C:\Python312\python.exe'
    )
    foreach ($Candidate in $Candidates) {
        if ((Test-Path -LiteralPath $Candidate) -and (Test-Python312 $Candidate @())) {
            return @{ Executable = $Candidate; Prefix = @() }
        }
    }
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand -and (Test-Python312 $PythonCommand.Source @())) {
        return @{ Executable = $PythonCommand.Source; Prefix = @() }
    }
    return $null
}

Write-Host '=== 直播切片工作台：首次安装 ===' -ForegroundColor Cyan
$Python = Find-Python312
if (-not $Python) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw '未找到 Python 3.12，也没有 winget。请先从 python.org 安装 64 位 Python 3.12。'
    }
    Write-Host '未找到 Python 3.12，正在通过 winget 安装……' -ForegroundColor Yellow
    & $Winget.Source install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 安装失败（退出码 $LASTEXITCODE）" }
    $Python = Find-Python312
    if (-not $Python) { throw 'Python 已安装，但当前窗口仍无法定位它。请关闭窗口后重新运行“首次安装.cmd”。' }
}

$Venv = Join-Path $PackageRoot '.python312'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host '正在创建独立 Python 环境……' -ForegroundColor Cyan
    & $Python.Executable @($Python.Prefix) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "创建 Python 环境失败（退出码 $LASTEXITCODE）" }
}

Write-Host '正在安装运行依赖；PyTorch 等组件较大，请耐心等待……' -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "更新 pip 失败（退出码 $LASTEXITCODE）" }
& $VenvPython -m pip install -r (Join-Path $PackageRoot 'target-song-cutter\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败（退出码 $LASTEXITCODE）" }

& $VenvPython -X utf8 -c "import tkinter, PIL, qrcode, torch, faster_whisper; print('Python 依赖检查通过')"
if ($LASTEXITCODE -ne 0) { throw '依赖检查失败' }

$VlcPaths = @(
    'C:\Program Files\VideoLAN\VLC\libvlc.dll',
    'C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll'
)
$HasVlc = $false
foreach ($VlcPath in $VlcPaths) { if (Test-Path -LiteralPath $VlcPath) { $HasVlc = $true } }
if (-not $HasVlc) {
    Write-Warning '未检测到 VLC。工作台仍可打开外部播放器，但内嵌视频预览需要 64 位 VLC。'
    Write-Host '可在 https://www.videolan.org/vlc/ 下载 VLC，安装后无需重新配置。'
}

Write-Host ''
Write-Host '安装完成。现在可以双击“启动工作台.cmd”。' -ForegroundColor Green
Write-Host '提示：首次转写会联网下载所选 Whisper 模型；自动选片还需要安装并登录 Codex CLI。'
