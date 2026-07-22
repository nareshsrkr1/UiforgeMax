# UiForgeMax - Build & Installation Guide

UiForgeMax is a Python MCP server. This guide covers building a distributable
wheel, installing it, and the day-to-day upgrade loop while you develop.

## Prerequisites

- Python 3.11+ (this repo currently uses the Python 3.13 launcher on Windows)
- PowerShell (Windows) - scripts live in `scripts\*.ps1`
- Git Bash is fine for editing/testing, but run the `.ps1` scripts through
  `powershell.exe`, not Git Bash, to avoid shell quoting differences

Check your Python:

```powershell
python --version
```

---

## 1. First-time setup (development)

Clone the repo, create a venv, and install in **editable mode** so code edits
take effect immediately without rebuilding a wheel:

```powershell
cd C:\Users\nares\Projects\UiForgeMax\UiforgeMax
python -m venv .venv
.\scripts\install.ps1 -Editable
```

This runs `pip install -e .` under `.venv`. From here on:
- Editing any file under `src\uiforgemax\` takes effect immediately.
- `.cursor\mcp.json` already points its `command` at `.venv\Scripts\python.exe`
  with `PYTHONPATH` set to `src`, so the MCP server always picks up the
  working copy regardless of what's pip-installed. This is the mode you want
  while actively developing the pipeline.

Run the test suite any time:

```powershell
.venv\Scripts\python.exe -m pytest tests\ -v
```

---

## 2. Building a wheel (`.whl`)

When you want a distributable artifact (to hand to someone else, or to
install into an IDE that isn't wired to this repo's `PYTHONPATH`), build a
wheel:

```powershell
.\scripts\build.ps1
```

What it does:
1. Cleans `dist\` and `src\uiforgemax.egg-info\` from any previous build
2. Installs/updates `pip`, `build`, `hatchling`
3. Runs the full test suite (`pytest tests\`) - **aborts the build on any
   test failure**
4. Builds the wheel into `dist\uiforgemax-<version>-py3-none-any.whl`

Flags:

```powershell
.\scripts\build.ps1 -SkipTests   # skip pytest (fast, use sparingly)
.\scripts\build.ps1 -Clean       # force-clean even if dist\ looks empty
```

The output wheel is a normal PEP 517 wheel - pure Python, no compiled
extensions - so it installs the same way on any machine with a matching
Python version.

---

## 3. Installing the wheel

### Option A - one-shot script (recommended)

```powershell
.\scripts\install.ps1
```

This builds a fresh wheel (unless `-SkipBuild` is passed) and installs it
into `.venv` with `pip install --force-reinstall`, replacing any previous
version.

```powershell
.\scripts\install.ps1 -SkipBuild   # install whatever's already in dist\
.\scripts\install.ps1 -SkipTests   # build without running tests, then install
```

### Option B - manual pip install

```powershell
.venv\Scripts\python.exe -m pip install --force-reinstall dist\uiforgemax-0.1.0-py3-none-any.whl
```

### Installing into a different machine / venv

Copy the `.whl` file to the target machine and:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install path\to\uiforgemax-0.1.0-py3-none-any.whl
```

Then point that machine's `.cursor\mcp.json` (or IDE MCP config) at that
venv's `python.exe`, e.g.:

```json
{
  "mcpServers": {
    "uiforgemax": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "uiforgemax.server"],
      "env": {
        "UIFORGEMAX_DATA_ROOT": "C:\\Users\\<you>\\.uiforgemax"
      }
    }
  }
}
```

No `PYTHONPATH` is needed for a wheel-based install - the package is already
on `site-packages`. `PYTHONPATH` is only used in this repo's own
`.cursor\mcp.json` for editable/dev mode.

---

## 4. Rebuilding after code changes (upgrade loop)

**If you're actively developing in this repo** (editable install): you don't
need to rebuild anything. Just save your file and reload the MCP connection
in your IDE (Cursor: reload window, or toggle the MCP server off/on in
Settings -> MCP).

**If you're shipping a new version to install elsewhere:**

1. Bump the version in `pyproject.toml`:
   ```toml
   [project]
   version = "0.2.0"
   ```
2. Rebuild:
   ```powershell
   .\scripts\build.ps1
   ```
3. Reinstall wherever it's deployed:
   ```powershell
   .\scripts\install.ps1 -SkipBuild
   ```
   (or copy the new `.whl` to the target machine and `pip install
   --force-reinstall` it there)
4. Restart/reload the MCP server so the IDE picks up the new package.

There's no need to manually bump the version for local iteration - only bump
it when cutting a wheel you intend to hand off or archive.

---

## 5. Verifying an install

```powershell
.venv\Scripts\python.exe -c "import uiforgemax; print(uiforgemax.__file__)"
.venv\Scripts\python.exe -m pip show uiforgemax
```

The first command's output tells you exactly which copy of the source Python
is importing - this is the fastest way to catch a stale install (e.g. if it
prints a path under `site-packages` when you expected editable mode pointing
at `src\uiforgemax`, or vice versa).

---

## 6. Common issues

**"uiforgemax location" points to the wrong place** - you have two
installs fighting (editable + wheel, or two venvs). Uninstall and reinstall
cleanly:

```powershell
.venv\Scripts\python.exe -m pip uninstall -y uiforgemax
.\scripts\install.ps1 -Editable    # for dev
# or
.\scripts\install.ps1              # for a real build
```

**PowerShell script fails with "string is missing the terminator"** - this
means the `.ps1` file has non-ASCII characters (curly quotes, em dashes) and
is being read under the system ANSI codepage instead of UTF-8. Windows
PowerShell 5.1 doesn't reliably detect UTF-8 without a BOM. Keep these
scripts ASCII-only, or save them as UTF-8 with BOM if you add non-ASCII text.

**Tests fail during `build.ps1`** - the build aborts on purpose; a broken
wheel is worse than no wheel. Fix the failing test, or use `-SkipTests` only
if you know why it's failing and it's unrelated to your change.

**MCP server doesn't pick up your changes** - check whether the IDE's
`mcp.json` uses `PYTHONPATH` (dev/editable mode, live-reads `src\uiforgemax`)
or a wheel install (`site-packages`, needs a rebuild + reinstall + IDE
reload).

---

## 7. Clean restart (when things get stuck)

MCP servers run subprocess children - `npm`, `node`, `playwright`,
`pytest` - during TEST_GENERATION and toolchain steps. If the IDE is force-
closed or crashes mid-run, those children can survive as orphans, holding
file locks or ports that make the *next* run behave strangely (stale
`node_modules` locks, a port still bound, a half-written run.json).

### `scripts\restart-mcp.ps1` - kill everything cleanly

```powershell
.\scripts\restart-mcp.ps1
```

What it does:
1. Finds every `python.exe` process whose command line runs
   `uiforgemax.server` (matched by command line, so it never touches an
   unrelated Python process on your machine).
2. Kills each one with `taskkill /F /T` - the `/T` flag kills the **entire
   process tree**, which is what actually clears orphaned `node.exe` / `npm`
   / `playwright` / `pytest` children spawned during test generation, not
   just the top-level python process.
3. Separately sweeps for orphans that already lost their parent (command
   line references `playwright`, `vitest`, `pytest`, or this repo's path)
   and kills those too.
4. Verifies nothing uiforgemax-related is left running, and tells you to
   reload the MCP connection in your IDE (Cursor: Settings -> MCP -> toggle
   off/on, or reload window).

Run this any time a run seems stuck, a test hangs, or after an IDE crash -
before starting a new session.

### `scripts\status.ps1` - see what start vs resume will actually do

```powershell
.\scripts\status.ps1
```

Answers the two questions that get confusing after a restart:

- **Is a server still running?** Lists any live `uiforgemax.server`
  process PIDs.
- **What does the session currently point at?** Prints the saved
  `workspaceRoot`, `activeRunId`, and whether that run still exists.
- **What will `start_run` vs `resume_run` do right now?**
  - If `activeRunId` points at a run that no longer exists on disk, it's
    called out explicitly as a **stale session pointer** (harmless -
    `start_run(mode='start')` makes a fresh run regardless).
  - If the active run is **terminal** (completed/failed/cancelled),
    `resume_run` on that id will be blocked - `start_run(mode='start')`
    archives it and starts clean.
  - If the active run is **not terminal**, `resume_run` will pick it back
    up; `start_run(mode='start')` would archive it instead - the script
    tells you which one applies before you call anything.

Use this before deciding whether to say "start" or "resume" to the agent -
it removes the guesswork.
