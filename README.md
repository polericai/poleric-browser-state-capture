# poleric-browser-state-capture

Local Playwright helper for generating reusable browser state bundles.

This repository is designed for two real workflows:
1. `popup` mode: user dismisses promo/country/consent overlays once.
2. `auth` mode: user logs in (including OTP) once.

The output format is a single JSON bundle file.

## One-command install (macOS/Linux, `.sh`)

```bash
curl -fsSL https://raw.githubusercontent.com/polericai/poleric-browser-state-capture/main/install.sh | bash
```

What this does:
1. Detects Python
2. Installs/upgrades `pipx`
3. Installs/upgrades `poleric-browser-state-capture`
4. Installs Playwright Chromium browser binaries

After install:

```bash
poleric-state-capture --help
```

If command is not found immediately, run:

```bash
python3 -m pipx ensurepath
```

Then open a new terminal.

## Windows note

Windows can run the cross-platform Python bootstrap:

```bash
py -3 -c "import urllib.request,tempfile,pathlib,runpy;u='https://raw.githubusercontent.com/polericai/poleric-browser-state-capture/main/install.py';p=pathlib.Path(tempfile.gettempdir())/'poleric_install.py';p.write_bytes(urllib.request.urlopen(u,timeout=60).read());runpy.run_path(str(p),run_name='__main__')"
```

## Bundle naming

- If `--bundle` is provided, that name is used as JSON filename.
  - Example: `--bundle bombas-popup` => `state_bundles/bombas-popup.json`
- If `--bundle` is omitted, static default name is used:
  - `state_bundles/state_bundle.json`

## Commands

### Capture popup-state (example: Bombas)

```bash
poleric-state-capture capture \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup
```

What to do in browser:
- close promo popup
- accept/deny cookie banner as desired
- select country/store preference if needed
- return to terminal and press Enter

### Capture auth-state (example: LMNT account OTP)

```bash
poleric-state-capture capture \
  --mode auth \
  --url https://drinklmnt.com/account \
  --bundle lmnt-auth
```

What to do in browser:
- login with OTP/email/password
- wait until account page is stable
- return to terminal and press Enter

### Verify bundle reuse

```bash
poleric-state-capture verify \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup
```

```bash
poleric-state-capture verify \
  --mode auth \
  --url https://drinklmnt.com/account \
  --bundle lmnt-auth
```

Optional strict checks:

```bash
poleric-state-capture verify \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup \
  --reject-visible-css '[role="dialog"]'
```

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
