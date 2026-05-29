# poleric-browser-state-capture

Local Playwright helper for generating reusable browser state bundles.

This repository is designed for two real workflows:
1. `popup` mode: user dismisses promo/country/consent overlays once.
2. `auth` mode: user logs in (including OTP) once.

The output format is Playwright-native (`storage_state.json`) plus `session_storage.json`.

## Why this exists

SaaS runners are server-side and cannot do manual OTP or manual popup dismissal by themselves.
This tool lets a user do that one-time manual interaction locally, then upload state for automated server runs.

## Requirements

- Python 3.10+
- Playwright Python package
- Browser binaries installed for Playwright

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install
```

## Commands

### Capture popup-state (example: Bombas)

```bash
python capture_state.py capture \
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
python capture_state.py capture \
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
python capture_state.py verify \
  --mode popup \
  --url https://bombas.com/ \
  --bundle bombas-popup \
  --reject-visible-css '[role="dialog"]'
```

```bash
python capture_state.py verify \
  --mode auth \
  --url https://drinklmnt.com/account \
  --bundle lmnt-auth \
  --expect-visible-css 'a[href*="logout"], button:has-text("Log out")'
```

## Output structure

Each bundle is created under `state_bundles/<bundle-name>/`:

- `storage_state.json`
- `session_storage.json`
- `meta.json`
- `user_profile/` (if `--persistent-profile` is enabled)

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
