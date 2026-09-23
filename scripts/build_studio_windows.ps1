param(
  [switch]$SkipTests,
  [switch]$SkipPrerequisiteInstall
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) {
  Write-Host "`n==> $Text" -ForegroundColor Cyan
}

function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machine;$user"
}

function Ensure-Command([string]$Name, [string]$WingetId) {
  if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
  if ($SkipPrerequisiteInstall) { throw "$Name is required but was not found." }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "$Name is required. Install it, or install Microsoft App Installer so winget is available."
  }
  Write-Host "Installing $Name with winget..." -ForegroundColor Yellow
  winget install --id $WingetId --exact --accept-package-agreements --accept-source-agreements --silent
  Refresh-Path
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name installation finished but it is not available on PATH yet. Restart this script." }
}

if ($env:OS -ne "Windows_NT") { throw "This installer build must run on Windows." }

Write-Step "Checking build prerequisites"
Ensure-Command "node" "OpenJS.NodeJS.LTS"
Ensure-Command "npm" "OpenJS.NodeJS.LTS"

$Python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
  try { & py -3.11 -c "import sys; print(sys.executable)" *> $null; if ($LASTEXITCODE -eq 0) { $Python = @('py','-3.11') } } catch {}
}
if (-not $Python -and (Get-Command python -ErrorAction SilentlyContinue)) {
  $v = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
  if ($v -eq '3.11') { $Python = @('python') }
}
if (-not $Python) {
  if ($SkipPrerequisiteInstall) { throw "Python 3.11 is required." }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Python 3.11 is required and winget is unavailable." }
  Write-Host "Installing Python 3.11 with winget..." -ForegroundColor Yellow
  winget install --id Python.Python.3.11 --exact --accept-package-agreements --accept-source-agreements --silent
  Refresh-Path
  if (Get-Command py -ErrorAction SilentlyContinue) { $Python = @('py','-3.11') } else { $Python = @('python') }
}

function Invoke-Python {
  param(
    [Parameter(Mandatory=$true)]
    [string[]]$PythonArgs
  )
  if ($Python.Count -eq 2) { & $Python[0] $Python[1] @PythonArgs } else { & $Python[0] @PythonArgs }
  if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($PythonArgs -join ' ')" }
}

# Prepare bundled FFmpeg before tests so the test suite works even on a clean Windows PC.
function Ensure-FFmpegTools {
  $ToolsDir = Join-Path $Root "studio/resources/tools"
  New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
  $ffmpegTarget = Join-Path $ToolsDir "ffmpeg.exe"
  $ffprobeTarget = Join-Path $ToolsDir "ffprobe.exe"
  if (-not (Test-Path $ffmpegTarget) -or -not (Test-Path $ffprobeTarget)) {
    Write-Step "Downloading FFmpeg and ffprobe"
    $CacheDir = Join-Path $Root "build/windows-cache"
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $FfmpegZip = Join-Path $CacheDir "ffmpeg-release-essentials.zip"
    $FfmpegExtract = Join-Path $CacheDir "ffmpeg"
    if (-not (Test-Path $FfmpegZip)) {
      Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $FfmpegZip
    }
    if (Test-Path $FfmpegExtract) { Remove-Item -Recurse -Force $FfmpegExtract }
    Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegExtract -Force
    $FfmpegExe = Get-ChildItem $FfmpegExtract -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    $FfprobeExe = Get-ChildItem $FfmpegExtract -Recurse -Filter ffprobe.exe | Select-Object -First 1
    if (-not $FfmpegExe -or -not $FfprobeExe) { throw "Could not find ffmpeg.exe/ffprobe.exe in downloaded archive." }
    Copy-Item -Force $FfmpegExe.FullName $ffmpegTarget
    Copy-Item -Force $FfprobeExe.FullName $ffprobeTarget
  }
  $env:AUTOEDITOR_FFMPEG = $ffmpegTarget
  $env:AUTOEDITOR_FFPROBE = $ffprobeTarget
  $env:Path = "$ToolsDir;$env:Path"
}

Ensure-FFmpegTools

Write-Step "Installing Python engine dependencies"
Invoke-Python -PythonArgs @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Python -PythonArgs @("-m", "pip", "install", "-e", ".[dev]", "pyinstaller")

if (-not $SkipTests) {
  Write-Step "Running Python tests"
  Invoke-Python -PythonArgs @("-m", "pytest", "-q", "--ignore=tests/test_cli_and_e2e.py")
}

Write-Step "Building Python engine sidecar"
Invoke-Python -PythonArgs @("-m", "PyInstaller", "--noconfirm", "--clean", "packaging/autoeditor_engine.spec")
New-Item -ItemType Directory -Force -Path studio/resources/engine | Out-Null
Copy-Item -Force dist/autoeditor-engine.exe studio/resources/engine/autoeditor-engine.exe
# Windows PowerShell 5.1 pipes text to programs with a UTF-8 byte-order mark when the console input encoding is UTF-8
# (as on GitHub's Windows runners); the engine expects plain JSON lines, so use UTF-8 without the mark.
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
$ping = '{"id":1,"method":"ping","params":{}}' | & studio/resources/engine/autoeditor-engine.exe | Select-Object -First 1
if (-not ($ping -match '"ok"\s*:\s*true')) { throw "Packaged engine failed its ping check: $ping" }

Write-Step "Installing Remotion dependencies"
Push-Location remotion
npm ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "npm ci failed in remotion." }
npm run typecheck
if ($LASTEXITCODE -ne 0) { throw "Remotion typecheck failed." }
# Pre-download the exact Chrome Headless Shell Remotion expects so the installed app does not download it at first render.
npx remotion browser ensure
if ($LASTEXITCODE -ne 0) { throw "Remotion browser download failed." }
Pop-Location

Write-Step "Verifying bundled FFmpeg and ffprobe"
Ensure-FFmpegTools

Write-Step "Bundling a private Node runtime for Remotion"
$NodeSource = Split-Path -Parent (Get-Command node).Source
$NodeTarget = Join-Path $Root "studio/resources/node"
if (Test-Path $NodeTarget) { Remove-Item -Recurse -Force $NodeTarget }
New-Item -ItemType Directory -Force -Path $NodeTarget | Out-Null
Copy-Item -Force (Join-Path $NodeSource "node.exe") $NodeTarget
foreach ($f in @('npm','npm.cmd','npx','npx.cmd')) {
  $src = Join-Path $NodeSource $f
  if (Test-Path $src) { Copy-Item -Force $src $NodeTarget }
}
$npmModules = Join-Path $NodeSource "node_modules/npm"
if (-not (Test-Path $npmModules)) { throw "Could not locate npm runtime beside node.exe." }
New-Item -ItemType Directory -Force -Path (Join-Path $NodeTarget "node_modules") | Out-Null
Copy-Item -Recurse -Force $npmModules (Join-Path $NodeTarget "node_modules/npm")

Write-Step "Installing Studio dependencies"
Push-Location studio
npm install --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "Studio npm install failed." }
npm run typecheck
if ($LASTEXITCODE -ne 0) { throw "Studio typecheck failed." }

Write-Step "Building Windows installer"
npm run dist:win
if ($LASTEXITCODE -ne 0) { throw "electron-builder failed." }
Pop-Location

$Installer = Get-ChildItem (Join-Path $Root "studio/builder-out") -Filter "Auto-Editor-PRO-Studio-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) { throw "Build finished but no installer was found." }
$ReleaseDir = Join-Path $Root "release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
$Final = Join-Path $ReleaseDir "Auto-Editor-PRO-Studio-Setup.exe"
Copy-Item -Force $Installer.FullName $Final

Write-Host "`nBUILD COMPLETE" -ForegroundColor Green
Write-Host "Installer: $Final" -ForegroundColor Green
Write-Host "Double-click that file to install Auto-Editor PRO Studio." -ForegroundColor Green
