$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$envPython = "<USER_HOME>\.conda\envs\review\python.exe"
if (-not (Test-Path $envPython)) {
    throw "Python interpreter not found: $envPython"
}

Write-Host "Using Python:" $envPython

# Ensure PyInstaller is available in the selected environment.
& $envPython -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found. Installing..."
    & $envPython -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller."
    }
}

if (Test-Path "review_app.spec") {
    Write-Host "Building from spec: review_app.spec"
    & $envPython -m PyInstaller --noconfirm --clean "review_app.spec"
} else {
    Write-Host "Spec file not found. Building with default options from review.py"
    & $envPython -m PyInstaller --noconfirm --clean --name "review_app" "review.py"
}

if ($LASTEXITCODE -ne 0) {
    throw "Build failed."
}

Write-Host "Build complete. EXE should be in dist\review_app\"
