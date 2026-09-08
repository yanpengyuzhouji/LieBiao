$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m PyInstaller --noconfirm --distpath dist --workpath packaging\build packaging\LieBiao.spec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
$compiler = Join-Path $PSScriptRoot 'tools\InnoSetup\ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) { $compiler = (Get-Command ISCC.exe -ErrorAction Stop).Source }
& $compiler /Qp (Join-Path $PSScriptRoot 'LieBiao.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
Get-FileHash -Algorithm SHA256 release\LieBiao-Setup-1.0.0-win-x64.exe
