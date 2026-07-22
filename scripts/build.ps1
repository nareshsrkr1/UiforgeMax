# UiForgeMax - Build & Package
# Usage: .\scripts\build.ps1
# Output: dist\uiforgemax-<version>-py3-none-any.whl

param(
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "=== UiForgeMax Build ===" -ForegroundColor Cyan

    # 1. Clean previous build
    if ($Clean -or (Test-Path "dist")) {
        Write-Host "[1/4] Cleaning previous build..." -ForegroundColor Yellow
        if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
        if (Test-Path "src\uiforgemax.egg-info") { Remove-Item -Recurse -Force "src\uiforgemax.egg-info" }
    } else {
        Write-Host "[1/4] No previous build to clean" -ForegroundColor DarkGray
    }

    # 2. Install build dependencies
    Write-Host "[2/4] Installing build tools..." -ForegroundColor Yellow
    python -m pip install --quiet --upgrade pip build hatchling

    # 3. Run tests
    if (-not $SkipTests) {
        Write-Host "[3/4] Running tests..." -ForegroundColor Yellow
        python -m pytest tests/ -q --tb=short
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Tests FAILED - aborting build." -ForegroundColor Red
            exit 1
        }
        Write-Host "All tests passed." -ForegroundColor Green
    } else {
        Write-Host "[3/4] Skipping tests (-SkipTests)" -ForegroundColor DarkGray
    }

    # 4. Build wheel
    Write-Host "[4/4] Building wheel..." -ForegroundColor Yellow
    python -m build --wheel --outdir dist
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Build FAILED." -ForegroundColor Red
        exit 1
    }

    $whl = Get-ChildItem "dist\*.whl" | Select-Object -First 1
    Write-Host ""
    Write-Host "Build complete: $($whl.Name)" -ForegroundColor Green
    Write-Host "Full path:      $($whl.FullName)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Install with:" -ForegroundColor Cyan
    Write-Host "  pip install $($whl.FullName)" -ForegroundColor White
} finally {
    Pop-Location
}
