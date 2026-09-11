$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$python = Join-Path (Get-Location) '.venv-ocr\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先使用 Python 3.9-3.13 创建 .venv-ocr 并安装 requirements.txt、requirements-ocr.txt 和 PyInstaller' }
$modelSource = Join-Path $env:USERPROFILE '.paddlex\official_models'
$modelTarget = Join-Path $PSScriptRoot 'models'
New-Item -ItemType Directory -Force -Path $modelTarget | Out-Null
foreach ($name in 'PP-OCRv6_small_det','PP-OCRv6_small_rec') {
    $source = Join-Path $modelSource $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $modelTarget -Recurse -Force }
}
& $python -m PyInstaller --noconfirm --distpath dist --workpath packaging\build-1.2.5 packaging\LieBiao.spec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
$compiler = Join-Path $PSScriptRoot 'tools\InnoSetup\ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) { $compiler = (Get-Command ISCC.exe -ErrorAction Stop).Source }
& $compiler /Qp (Join-Path $PSScriptRoot 'LieBiao.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
$installer = Resolve-Path release\LieBiao-Setup-1.2.5-win-x64.exe
$stream = [IO.File]::OpenRead($installer)
try { $hash = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($stream) | ForEach-Object { $_.ToString('X2') }) }
finally { $stream.Dispose() }
Write-Output "SHA256 $hash  $installer"
