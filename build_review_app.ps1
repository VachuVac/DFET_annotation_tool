param(
    # Python to build with. Defaults to the active conda env, then python on PATH.
    [string]$Python
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if ($Python) {
    $envPython = $Python
} elseif ($env:CONDA_PREFIX) {
    $envPython = Join-Path $env:CONDA_PREFIX "python.exe"
} else {
    $envPython = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path $envPython)) {
    throw "Python interpreter not found: $envPython"
}

Write-Host "Using Python:" $envPython

# Run a native command without "Stop" turning its stderr into a fatal error: Windows
# PowerShell 5.1 does that when output is redirected, and PyInstaller logs to stderr.
# Failures are detected via $LASTEXITCODE instead.
function Invoke-Native([scriptblock]$Command) {
    $ErrorActionPreference = "Continue"
    & $Command
}

# Derive the build name (incl. version) from the single source of truth in constants.
$constantsSrc = Get-Content "src/constants.py" -Raw
$appName = [regex]::Match($constantsSrc, 'APP_NAME\s*=\s*["'']([^"'']+)["'']').Groups[1].Value
$appVersion = [regex]::Match($constantsSrc, 'APP_VERSION\s*=\s*["'']([^"'']+)["'']').Groups[1].Value
$buildName = "$appName $appVersion"

# Ensure PyInstaller is available in the selected environment.
Invoke-Native { & $envPython -m pip show pyinstaller *> $null }
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found. Installing..."
    Invoke-Native { & $envPython -m pip install pyinstaller }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller."
    }
}

if (Test-Path "review_app.spec") {
    Write-Host "Building from spec: review_app.spec"
    Invoke-Native { & $envPython -m PyInstaller --noconfirm --clean "review_app.spec" }
} else {
    Write-Host "Spec file not found. Building with default options from review.py"
    Invoke-Native { & $envPython -m PyInstaller --noconfirm --clean --name "$buildName" --icon "src/assets/app_icon.ico" "review.py" }
}

if ($LASTEXITCODE -ne 0) {
    throw "Build failed."
}

# One-file build: the whole app is the single exe. Share just this file; the 'build\'
# folder is throwaway intermediate work and does NOT need to be shared.
Write-Host ""
Write-Host "Build complete."
Write-Host "Share THIS file: dist\$buildName.exe"
