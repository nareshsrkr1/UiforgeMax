# UiForgeMax - Clean restart of the MCP server
#
# Kills every uiforgemax MCP server process (matched by command line, so it
# only touches this project's python.exe -m uiforgemax.server processes) and
# its FULL process tree - node/npm/playwright/pytest children included -
# so a crashed or half-dead session cannot leave orphans holding file locks
# or ports open.
#
# Usage:
#   .\scripts\restart-mcp.ps1            # kill + report; you reload the IDE's MCP connection
#   .\scripts\restart-mcp.ps1 -Verbose   # show every matched process

param(
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"

Write-Host "=== UiForgeMax MCP - Clean Restart ===" -ForegroundColor Cyan

# 1. Find all python.exe processes whose command line runs uiforgemax.server.
#    Matching on command line (not just process name) so we never touch an
#    unrelated python.exe on the machine.
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "uiforgemax\.server" }

if (-not $procs) {
    Write-Host "No running uiforgemax MCP server processes found." -ForegroundColor Green
} else {
    Write-Host "Found $($procs.Count) uiforgemax server process(es):" -ForegroundColor Yellow
    foreach ($p in $procs) {
        Write-Host "  PID $($p.ProcessId)  $($p.CommandLine)" -ForegroundColor DarkGray
    }

    foreach ($p in $procs) {
        Write-Host "Killing PID $($p.ProcessId) and its full process tree..." -ForegroundColor Yellow
        # /T kills the entire descendant tree - this is what reliably clears
        # orphaned node.exe / npm / playwright / pytest children spawned by
        # the pipeline's subprocess calls, not just the python.exe itself.
        taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
    }
}

# 2. Sweep for orphaned children that survived their parent MCP server dying
#    before it could clean up (e.g. IDE force-closed mid test-run). These are
#    identified by command line referencing this repo's runs root or the
#    playwright/vitest/pytest tools UiForgeMax launches.
$repoRoot = Split-Path -Parent $PSScriptRoot
$orphanPatterns = @("playwright", "vitest", "pytest", [Regex]::Escape($repoRoot))

$candidates = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -in @("node.exe", "npm.cmd", "python.exe", "cmd.exe") -and $_.CommandLine
}
$orphans = $candidates | Where-Object {
    $cmd = $_.CommandLine
    ($orphanPatterns | Where-Object { $cmd -match $_ }).Count -gt 0
}

if ($orphans) {
    Write-Host ""
    Write-Host "Found $($orphans.Count) orphaned child process(es) from prior UiForgeMax runs:" -ForegroundColor Yellow
    foreach ($o in $orphans) {
        Write-Host "  PID $($o.ProcessId)  $($o.Name)  $($o.CommandLine)" -ForegroundColor DarkGray
        taskkill /F /T /PID $o.ProcessId 2>&1 | Out-Null
    }
} else {
    Write-Host "No orphaned child processes found." -ForegroundColor Green
}

# 3. Verify nothing is left.
Start-Sleep -Milliseconds 500
$remaining = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "uiforgemax\.server" }

Write-Host ""
if ($remaining) {
    Write-Host "WARNING: $($remaining.Count) uiforgemax process(es) still alive after kill attempt:" -ForegroundColor Red
    foreach ($p in $remaining) { Write-Host "  PID $($p.ProcessId)" -ForegroundColor Red }
    Write-Host "Try running this script again, or reboot if it persists (likely a stuck driver-level handle)." -ForegroundColor Red
} else {
    Write-Host "Clean. No uiforgemax processes remain." -ForegroundColor Green
}

Write-Host ""
Write-Host "Next step: reload the MCP connection in your IDE" -ForegroundColor Cyan
Write-Host "  Cursor:  Settings -> MCP -> toggle 'uiforgemax' off then on (or reload window)" -ForegroundColor White
Write-Host "  VS Code: reload window, or restart the MCP server from the Copilot Chat MCP panel" -ForegroundColor White
