# poleric-browser-state-capture

Local Playwright helper for generating reusable browser state bundles.

This repository is designed for two real workflows:
1. `popup` mode: user dismisses promo/country/consent overlays once.
2. `auth` mode: user logs in (including OTP) once.

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

Capture popup-state (example: Bombas):

```bash
python capture_state.py capture \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup
```

Capture auth-state (example: LMNT account OTP):

```bash
python capture_state.py capture \
  --mode auth \
  --url https://drinklmnt.com/account \
  --bundle lmnt-auth
```

Verify popup bundle:

```bash
python capture_state.py verify \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup
```

Verify auth bundle:

```bash
python capture_state.py verify \
  --mode auth \
  --url https://drinklmnt.com/account \
  --bundle lmnt-auth
```

Optional strict popup check:

```bash
python capture_state.py verify \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup \
  --reject-visible-css '[role="dialog"]'
```

## Bundle naming

- If `--bundle` is provided, that name is used as JSON filename.
  - Example: `--bundle bombas-popup` => `state_bundles/bombas-popup.json`
- If `--bundle` is omitted, static default name is used:
  - `state_bundles/state_bundle.json`

## Output structure

Each capture writes one JSON bundle file under `state_bundles/`.

The bundle includes:
- `storage_state` (Playwright cookies/localStorage/indexedDB)
- `session_storage` (by origin)
- `meta` (capture metadata)

Verification screenshot is saved as:
- `state_bundles/<bundle-stem>_verify.png`

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
