# Deploy & Validate UiForgeMax on Another Machine

Simple checklist for updating UiForgeMax on a second machine (e.g. office laptop)
and proving it actually picked up the new code. Do the steps in order.

Replace these placeholders as you go:
- `<REPO>`   = the UiForgeMax repo folder on this machine (e.g. `C:\Users\U520992\CIBDS\Repos\UiforgeMax`)
- `<VENV>`   = `<REPO>\.venv`
- `<WORKSPACE>` = the project folder you actually open in the IDE (e.g. `...\App-cibds-marketplace-ui`)

---

## 1. Get the new code

```powershell
cd <REPO>
git pull
<VENV>\Scripts\pip.exe install -e . --force-reinstall --no-deps
```

The `pip install -e .` line matters: without it, `git pull` updates the source
files but the server may still import an old installed copy.

---

## 2. Prove the code is FRESH (not stale)

Run this ONE command. It loads the code the same way the server does and checks
the intake fix is present:

```powershell
<VENV>\Scripts\python.exe -c "from uiforgemax.mcp_response import _gate_next; from uiforgemax.state import RunState, Status; s=RunState(run_id='x', project_root='.', status=Status.INTAKE); s.inputs['modes']=['jira']; nt=_gate_next(s, jira_configured=True)[0]; print('RESULT:', 'FRESH' if nt=='uiforgemax_advance' else 'STALE')"
```

- Prints **FRESH** -> good, code is updated.
- Prints **STALE** -> the running code is old. Re-run step 1's `pip install -e .`
  line, then check again.

Also confirm WHERE it imports from (should be under `<REPO>\src\...`):

```powershell
<VENV>\Scripts\python.exe -c "import uiforgemax.mcp_response as m; print(m.__file__)"
```

If it prints a path under `site-packages\uiforgemax\` instead of `src\`, the
install is not editable -> re-run the `pip install -e .` line in step 1.

---

## 3. Clean restart

```powershell
# Kill any running MCP server + its child processes
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match "uiforgemax\.server" } |
  ForEach-Object { taskkill /F /T /PID $_.ProcessId }
```

Then in the IDE: reload the window (or toggle the `uiforgemax` MCP server off/on).

Confirm the server that comes back is NEW (CreationDate must be after your restart):

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match "uiforgemax\.server" } |
  Select-Object ProcessId, CreationDate
```

---

## 4. Update the agent rules (separate from code!)

The agent instructions live at `<REPO>\agent\uiforgemax-agent.md` (git pull
already updated this file). BUT the IDE only reads it from wherever its agent
config points -- git pull does NOT wire that up for you.

Find where your IDE currently reads agent rules from:

```powershell
Get-ChildItem -Recurse -Include "copilot-instructions.md","*.mdc" -Path "<WORKSPACE>" -ErrorAction SilentlyContinue | Select-Object FullName
```

Copy the updated rules into that location. Example for a Copilot-style IDE:

```powershell
New-Item -ItemType Directory -Force "<WORKSPACE>\.github" | Out-Null
Copy-Item "<REPO>\agent\uiforgemax-agent.md" "<WORKSPACE>\.github\copilot-instructions.md" -Force
```

Then start a **fresh chat** (reloading alone does not reload agent rules into an
existing conversation).

---

## 5. Start a run

In a fresh chat, say (with your real path):

```
start BBHJ-6545, <WORKSPACE>
```

Expected: it runs preflight -> start_run -> add_jira -> then AUTO-ADVANCES all
the way to the plan-approval gate WITHOUT stopping to ask "should I continue?".
The only human stop is plan approval.

---

## If graphify update is slow / times out (office machine)

Every graphify run now writes a live log you can watch in a SECOND terminal:

```powershell
Get-Content -Wait "<UIFORGEMAX_DATA_ROOT>\runs\<run_id>\graph\by-root\default\graphify-update.log"
```

The log's header shows the exact command, the working dir, and confirms
PYTHONPATH is stripped from the subprocess (`subprocess PYTHONPATH (used): (unset)`).
If it still crawls, the live output shows exactly which file/step it is stuck on.

To compare against a clean manual run (should be fast):

```powershell
cd <WORKSPACE>
Measure-Command { python -m graphify update . --no-cluster }
```

If manual is fast but the MCP run is slow even with PYTHONPATH stripped, the
remaining suspect is endpoint AV/EDR scanning file reads -- capture the log and
the manual timing and report those.
