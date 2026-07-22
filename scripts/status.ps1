# UiForgeMax - Show current session/run status (what start vs resume will do)
# Usage: .\scripts\status.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "No .venv found at $root\.venv - run .\scripts\install.ps1 -Editable first." -ForegroundColor Red
    exit 1
}

# Also show whether a server process is currently running, since that
# affects whether file locks / stale state could be in play.
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "uiforgemax\.server" }

if ($procs) {
    Write-Host "Running MCP server process(es): $($procs.Count)" -ForegroundColor Yellow
    foreach ($p in $procs) { Write-Host "  PID $($p.ProcessId)" -ForegroundColor DarkGray }
} else {
    Write-Host "No MCP server process currently running." -ForegroundColor DarkGray
}
Write-Host ""

& $venvPython "$root\scripts\status.py"
