# UiForgeMax - Install (or upgrade) into the local venv
# Usage:
#   .\scripts\install.ps1                # build + install latest wheel
#   .\scripts\install.ps1 -SkipBuild     # install whatever wheel is already in dist\
#   .\scripts\install.ps1 -Editable      # pip install -e . (dev loop, no wheel)

param(
    [switch]$SkipBuild,
    [switch]$Editable,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "=== UiForgeMax Install ===" -ForegroundColor Cyan

    $venvPython = "$root\.venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "No .venv found - creating one..." -ForegroundColor Yellow
        python -m venv "$root\.venv"
    }

    if ($Editable) {
        Write-Host "Installing in editable mode (pip install -e .)..." -ForegroundColor Yellow
        & $venvPython -m pip install --quiet -e .
        Write-Host "Editable install complete. Source at src\uiforgemax is live - no rebuild needed for code changes." -ForegroundColor Green
        return
    }

    if (-not $SkipBuild) {
        Write-Host "Building wheel first..." -ForegroundColor Yellow
        $buildArgs = @()
        if ($SkipTests) { $buildArgs += "-SkipTests" }
        & "$root\scripts\build.ps1" @buildArgs
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }

    $whl = Get-ChildItem "$root\dist\*.whl" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $whl) {
        Write-Host "No wheel found in dist\. Run without -SkipBuild first." -ForegroundColor Red
        exit 1
    }

    Write-Host "Installing $($whl.Name) into venv..." -ForegroundColor Yellow
    & $venvPython -m pip install --force-reinstall --no-deps "$($whl.FullName)"
    & $venvPython -m pip install --quiet "$($whl.FullName)"

    Write-Host ""
    Write-Host "Installed successfully." -ForegroundColor Green
    & $venvPython -c "import uiforgemax; print('uiforgemax location:', uiforgemax.__file__)"
    Write-Host ""
    Write-Host "Reload your IDE / restart the MCP server for changes to take effect." -ForegroundColor Cyan
} finally {
    Pop-Location
}
