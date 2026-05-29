# poleric-browser-state-capture

Local Playwright helper for generating reusable browser state bundles.

The output format is a single JSON bundle file.

## Dependencies

1. Python `3.10+`
2. Python package from `requirements.txt`:
   - `playwright>=1.51,<2.0`
3. Playwright browser binary:
   - `chromium` via `playwright install chromium`

## Setup (Git clone + venv)

```bash
git clone https://github.com/polericai/poleric-browser-state-capture.git
cd poleric-browser-state-capture
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

Windows PowerShell activation:

```powershell
.venv\Scripts\Activate.ps1
```

## Update Existing Clone

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run commands

Check CLI:

```bash
python capture_state.py --help
```

Capture with no mode (default mode = `general`):

```bash
python capture_state.py capture --url https://bombas.com/
```

Capture popup mode:

```bash
python capture_state.py capture --mode popup --url https://bombas.com/
```

Capture auth mode:

```bash
python capture_state.py capture --mode auth --url https://drinklmnt.com/account
```

Use your own file name:

```bash
python capture_state.py capture \
  --url https://bombas.com/ \
  --file-name my-bombas-state.json
```

Verify:

```bash
python capture_state.py verify --url https://bombas.com/
```

Verify with a specific file:

```bash
python capture_state.py verify \
  --url https://bombas.com/ \
  --file-name my-bombas-state.json
```

## Naming rules

- All output files are written into `browser_state/` by default.
- If `--file-name` is provided, that user-provided file name is used.
  - Example: `--file-name my-login.json` => `browser_state/my-login.json`
- If `--file-name` is omitted, default naming uses domain + mode:
  - General mode: `<domain>-state.json`
  - Popup mode: `<domain>-popup-state.json`
  - Auth mode: `<domain>-auth-state.json`

Examples:
- `https://bombas.com` + no mode => `browser_state/bombas-state.json`
- `https://bombas.com` + `--mode popup` => `browser_state/bombas-popup-state.json`
- `https://drinklmnt.com` + `--mode auth` => `browser_state/drinklmnt-auth-state.json`

## Output structure

Each capture writes one JSON bundle file under `browser_state/`.

The bundle includes:
- `storage_state` (Playwright cookies/localStorage/indexedDB)
- `session_storage` (by origin)
- `meta` (capture metadata)

## Security note

State files may include active auth cookies/tokens.
Treat them like credentials:
- do not commit to git
- store encrypted at rest
- rotate/re-capture when expired or revoked

## SaaS integration model

Recommended project-level uploads in SaaS:
1. `popup_state_bundle`
2. `auth_state_bundle`

Runner strategy:
1. attach relevant bundle before run
2. run preflight check
3. if invalid, fallback/retry
4. if still invalid, request recapture

This keeps manual work as a last resort.
