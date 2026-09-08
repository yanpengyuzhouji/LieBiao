$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m PyInstaller --noconfirm --distpath dist --workpath packaging\build-1.1.0 packaging\LieBiao.spec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
$compiler = Join-Path $PSScriptRoot 'tools\InnoSetup\ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) { $compiler = (Get-Command ISCC.exe -ErrorAction Stop).Source }
& $compiler /Qp (Join-Path $PSScriptRoot 'LieBiao.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
$installer = Resolve-Path release\LieBiao-Setup-1.1.0-win-x64.exe
$stream = [IO.File]::OpenRead($installer)
try { $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::Create().ComputeHash($stream)) }
finally { $stream.Dispose() }
Write-Output "SHA256 $hash  $installer"
