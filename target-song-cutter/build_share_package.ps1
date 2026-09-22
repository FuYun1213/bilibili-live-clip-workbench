param(
    [string]$OutputRoot = '',
    [switch]$KeepExpanded
)
$ErrorActionPreference = 'Stop'
$SkillRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = Split-Path -Parent $SkillRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $WorkspaceRoot 'releases' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$PackageName = "直播切片工作台-$Stamp"
$PackageRoot = Join-Path $OutputRoot $PackageName
if (Test-Path -LiteralPath $PackageRoot) { throw "输出目录已存在：$PackageRoot" }
New-Item -ItemType Directory -Path $PackageRoot | Out-Null

function Copy-FilteredDirectory([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($File in Get-ChildItem -LiteralPath $Source -Recurse -File) {
        $Relative = $File.FullName.Substring($Source.Length).TrimStart('\', '/')
        $Parts = $Relative -split '[\\/]'
        if ($Parts -contains '__pycache__' -or $Parts -contains 'legacy') { continue }
        if ($File.Name -like 'test_*.py' -or $File.Extension -in @('.pyc', '.pyo')) { continue }
        $Target = Join-Path $Destination $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target
    }
}

$TargetSkill = Join-Path $PackageRoot 'target-song-cutter'
New-Item -ItemType Directory -Path $TargetSkill | Out-Null
foreach ($DirectoryName in @('scripts', 'assets', 'references')) {
    Copy-FilteredDirectory (Join-Path $SkillRoot $DirectoryName) (Join-Path $TargetSkill $DirectoryName)
}
Copy-Item -LiteralPath (Join-Path $SkillRoot 'requirements.txt') -Destination (Join-Path $TargetSkill 'requirements.txt')

function Remove-CollectionAccountIds($Value) {
    if ($null -eq $Value) { return }
    if ($Value -is [string]) { return }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($Key in @($Value.Keys)) {
            if ($Key -in @('season_id', 'section_id')) { $Value.Remove($Key); continue }
            Remove-CollectionAccountIds $Value[$Key]
        }
        return
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [pscustomobject]) {
        foreach ($Item in $Value) { Remove-CollectionAccountIds $Item }
        return
    }
    if ($Value -isnot [pscustomobject]) { return }
    foreach ($Property in @($Value.PSObject.Properties)) {
        if ($Property.Name -in @('season_id', 'section_id')) {
            $Value.PSObject.Properties.Remove($Property.Name)
        } else {
            Remove-CollectionAccountIds $Property.Value
        }
    }
}
$ProfilePath = Join-Path $TargetSkill 'assets\creator-profiles.json'
$Profiles = Get-Content -LiteralPath $ProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
Remove-CollectionAccountIds $Profiles
$ProfilesJson = $Profiles | ConvertTo-Json -Depth 100
[IO.File]::WriteAllText($ProfilePath, $ProfilesJson + "`n", [Text.UTF8Encoding]::new($false))

$ToolDirectory = Join-Path $PackageRoot 'tools\ffmpeg'
New-Item -ItemType Directory -Path $ToolDirectory -Force | Out-Null
$BundledFfmpeg = Join-Path $WorkspaceRoot 'tools\ffmpeg\ffmpeg.exe'
$BundledFfprobe = Join-Path $WorkspaceRoot 'tools\ffmpeg\ffprobe.exe'
$FfmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
$FfprobeCommand = Get-Command ffprobe -ErrorAction SilentlyContinue
$FfmpegSource = if (Test-Path -LiteralPath $BundledFfmpeg) { $BundledFfmpeg } elseif ($FfmpegCommand) { $FfmpegCommand.Source } else { $BundledFfmpeg }
$FfprobeSource = if (Test-Path -LiteralPath $BundledFfprobe) { $BundledFfprobe } elseif ($FfprobeCommand) { $FfprobeCommand.Source } else { $BundledFfprobe }
if (-not (Test-Path -LiteralPath $FfmpegSource)) { throw '找不到 ffmpeg.exe' }
if (-not (Test-Path -LiteralPath $FfprobeSource)) { throw '找不到 ffprobe.exe' }
Copy-Item -LiteralPath $FfmpegSource -Destination (Join-Path $ToolDirectory 'ffmpeg.exe')
Copy-Item -LiteralPath $FfprobeSource -Destination (Join-Path $ToolDirectory 'ffprobe.exe')

$Packaging = Join-Path $SkillRoot 'packaging'
foreach ($Name in @('启动工作台.cmd', '首次安装.cmd', '首次安装.ps1', '使用说明.txt')) {
    Copy-Item -LiteralPath (Join-Path $Packaging $Name) -Destination (Join-Path $PackageRoot $Name)
}
foreach ($DirectoryName in @('录播', 'workflow-projects', 'models')) {
    $EmptyDirectory = Join-Path $PackageRoot $DirectoryName
    New-Item -ItemType Directory -Path $EmptyDirectory -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $EmptyDirectory '.keep'), '', [Text.UTF8Encoding]::new($false))
}

$FileRows = @()
foreach ($File in Get-ChildItem -LiteralPath $PackageRoot -Recurse -File | Sort-Object FullName) {
    $Relative = $File.FullName.Substring($PackageRoot.Length).TrimStart('\', '/') -replace '\\', '/'
    $FileRows += [ordered]@{
        path = $Relative
        bytes = $File.Length
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$Manifest = [ordered]@{
    product = '直播切片工作台'
    package = $PackageName
    generated_at = (Get-Date).ToUniversalTime().ToString('o')
    portable_runtime = '首次安装时创建 Python 3.12 虚拟环境'
    privacy = [ordered]@{
        credentials_included = $false
        recordings_included = $false
        existing_projects_included = $false
        whisper_models_included = $false
        collection_account_ids_removed = $true
    }
    file_count = $FileRows.Count
    files = $FileRows
}
$ManifestPath = Join-Path $PackageRoot 'package-manifest.json'
$ManifestJson = $Manifest | ConvertTo-Json -Depth 10
[IO.File]::WriteAllText($ManifestPath, $ManifestJson + "`n", [Text.UTF8Encoding]::new($false))

$ZipPath = Join-Path $OutputRoot ($PackageName + '.zip')
Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
$ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$HashPath = $ZipPath + '.sha256.txt'
[IO.File]::WriteAllText($HashPath, "$ZipHash  $([IO.Path]::GetFileName($ZipPath))`r`n", [Text.UTF8Encoding]::new($false))
$ExpandedPath = $PackageRoot
if (-not $KeepExpanded) {
    $ResolvedOutput = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $ResolvedPackage = [IO.Path]::GetFullPath($PackageRoot)
    if (-not $ResolvedPackage.StartsWith($ResolvedOutput, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理输出目录之外的展开包：$ResolvedPackage"
    }
    Remove-Item -LiteralPath $ResolvedPackage -Recurse -Force
    $ExpandedPath = $null
}
[ordered]@{
    package_root = $ExpandedPath
    zip = $ZipPath
    sha256_file = $HashPath
    zip_bytes = (Get-Item -LiteralPath $ZipPath).Length
    sha256 = $ZipHash
} | ConvertTo-Json
