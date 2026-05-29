#!/usr/bin/env python3
"""Interactive Playwright state capture utility for Poleric users.

This tool supports two workflows:
1. popup-state capture (country/consent/promo dismissal)
2. auth-state capture (login/OTP)

Output is a single JSON bundle that includes:
- Playwright storage_state (cookies/localStorage/indexedDB)
- sessionStorage by origin
- metadata
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_OUTPUT_DIR = "state_bundles"
DEFAULT_BUNDLE_FILENAME = "state_bundle.json"
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")
BUNDLE_SCHEMA = "poleric.browser_state_bundle.v1"

SESSION_EXTRACT_JS = """() => {
  const data = {};
  try {
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      if (!key) continue;
      data[key] = window.sessionStorage.getItem(key);
    }
  } catch (e) {
    return {};
  }
  return data;
}"""

DIALOG_SCAN_JS = """() => {
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [data-testid*="modal" i], [class*="modal" i], [class*="overlay" i]'));
  const visible = dialogs.filter((el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  });
  return {
    totalCandidates: dialogs.length,
    visibleCandidates: visible.length,
    sampleText: (visible[0] && visible[0].innerText ? visible[0].innerText.trim().slice(0, 120) : '')
  };
}"""


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    cleaned = text.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned or "site"


def site_key_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    host = host.lower().replace("www.", "")
    return slugify(host)


def ensure_json_filename(name: str) -> str:
    return name if name.lower().endswith(".json") else f"{name}.json"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_session_payload(payload: Any) -> dict[str, dict[str, str]]:
    if isinstance(payload, dict) and isinstance(payload.get("by_origin"), dict):
        payload = payload["by_origin"]
    if not isinstance(payload, dict):
        return {}

    out: dict[str, dict[str, str]] = {}
    for origin, values in payload.items():
        if not isinstance(origin, str) or not isinstance(values, dict):
            continue
        mapped: dict[str, str] = {}
        for key, value in values.items():
            if isinstance(key, str):
                mapped[key] = "" if value is None else str(value)
        if mapped:
            out[origin] = mapped
    return out


def normalize_storage_state_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"cookies": [], "origins": []}

    cookies = payload.get("cookies")
    origins = payload.get("origins")

    normalized = {
        "cookies": cookies if isinstance(cookies, list) else [],
        "origins": origins if isinstance(origins, list) else [],
    }

    # Keep any optional Playwright fields if they exist.
    for key, value in payload.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def local_storage_map_from_state(state: dict[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    origins = state.get("origins") if isinstance(state, dict) else None
    if not isinstance(origins, list):
        return out

    for entry in origins:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        local_items = entry.get("localStorage")
        if not isinstance(origin, str) or not isinstance(local_items, list):
            continue

        mapped: dict[str, str] = {}
        for item in local_items:
            if not isinstance(item, dict):
                continue
            key = item.get("name")
            value = item.get("value")
            if isinstance(key, str):
                mapped[key] = "" if value is None else str(value)
        if mapped:
            out[origin] = mapped
    return out


def build_storage_init_script(storage_map: dict[str, dict[str, str]], storage_kind: str) -> str:
    payload = json.dumps(storage_map, separators=(",", ":"))
    return f"""
(() => {{
  const map = {payload};
  const origin = window.location.origin;
  const values = map[origin];
  if (!values || typeof values !== "object") return;
  for (const [key, value] of Object.entries(values)) {{
    try {{
      window.{storage_kind}.setItem(key, String(value));
    }} catch (err) {{
      // Best-effort only.
    }}
  }}
}})();
"""


def summarize_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    expiries: list[int] = []
    for cookie in cookies:
        exp = cookie.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            expiries.append(int(exp))

    summary: dict[str, Any] = {
        "count": len(cookies),
        "http_only_count": sum(1 for c in cookies if c.get("httpOnly") is True),
        "secure_count": sum(1 for c in cookies if c.get("secure") is True),
    }
    if expiries:
        summary["earliest_expiry_unix"] = min(expiries)
        summary["latest_expiry_unix"] = max(expiries)
        summary["expired_count"] = sum(1 for value in expiries if value <= now_ts)
    return summary


def resolve_bundle_paths(args: argparse.Namespace) -> dict[str, Path]:
    output_dir = Path(args.output_dir).resolve()

    if args.bundle:
        file_name = ensure_json_filename(args.bundle)
    else:
        file_name = DEFAULT_BUNDLE_FILENAME

    bundle_file = output_dir / file_name
    bundle_key = Path(file_name).stem or site_key_from_url(args.url)

    if args.profile_dir:
        profile_dir = Path(args.profile_dir).resolve()
    else:
        profile_dir = output_dir / f"{bundle_key}_profile"

    verify_screenshot = output_dir / f"{bundle_key}_verify.png"

    return {
        "output_dir": output_dir,
        "bundle_file": bundle_file,
        "verify_screenshot": verify_screenshot,
        "profile_dir": profile_dir,
    }


def ensure_url(url: str) -> str:
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return f"https://{url}"
    return url


def parse_bundle_payload(payload: Any) -> tuple[dict[str, Any], dict[str, dict[str, str]], dict[str, Any]]:
    # Legacy support: allow a raw Playwright storage_state JSON as input.
    if isinstance(payload, dict) and "cookies" in payload and "origins" in payload and "storage_state" not in payload:
        storage_state = normalize_storage_state_payload(payload)
        return storage_state, {}, {}

    if not isinstance(payload, dict):
        return {"cookies": [], "origins": []}, {}, {}

    storage_state = normalize_storage_state_payload(payload.get("storage_state", {}))
    session_storage = normalize_session_payload(payload.get("session_storage", {}))
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return storage_state, session_storage, meta


def load_existing_bundle(bundle_file: Path) -> tuple[dict[str, Any], dict[str, dict[str, str]], dict[str, Any]]:
    if not bundle_file.exists():
        return {"cookies": [], "origins": []}, {}, {}

    try:
        payload = read_json(bundle_file)
    except Exception:
        return {"cookies": [], "origins": []}, {}, {}

    return parse_bundle_payload(payload)


def launch_context_and_page(playwright: Any, args: argparse.Namespace, paths: dict[str, Path]):
    browser_type = getattr(playwright, args.browser)

    storage_state_payload: dict[str, Any] = {"cookies": [], "origins": []}
    session_by_origin: dict[str, dict[str, str]] = {}

    reuse_bundle = args.reuse_existing and paths["bundle_file"].exists()
    if reuse_bundle:
        storage_state_payload, session_by_origin, _ = load_existing_bundle(paths["bundle_file"])

    storage_map_for_inject = local_storage_map_from_state(storage_state_payload)
    cookies_for_add: list[dict[str, Any]] = [
        c for c in storage_state_payload.get("cookies", []) if isinstance(c, dict)
    ]

    browser = None
    if args.persistent_profile:
        context = browser_type.launch_persistent_context(
            str(paths["profile_dir"]),
            headless=args.headless,
            viewport=DEFAULT_VIEWPORT,
            service_workers=args.service_workers,
        )
    else:
        browser = browser_type.launch(headless=args.headless)
        context_kwargs: dict[str, Any] = {
            "viewport": DEFAULT_VIEWPORT,
            "service_workers": args.service_workers,
        }
        if reuse_bundle:
            context_kwargs["storage_state"] = storage_state_payload
        context = browser.new_context(**context_kwargs)

    # sessionStorage is not in Playwright storage_state, so we inject it.
    if session_by_origin:
        context.add_init_script(script=build_storage_init_script(session_by_origin, "sessionStorage"))

    # For persistent profile mode, rehydrate cookies/localStorage from bundle too.
    if args.persistent_profile and reuse_bundle:
        if cookies_for_add:
            context.add_cookies(cookies_for_add)
        if storage_map_for_inject:
            context.add_init_script(script=build_storage_init_script(storage_map_for_inject, "localStorage"))

    page = context.pages[0] if context.pages else context.new_page()
    return context, page, browser


def build_bundle_payload(
    *,
    args: argparse.Namespace,
    paths: dict[str, Path],
    meta: dict[str, Any],
    storage_state: dict[str, Any],
    session_by_origin: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema": BUNDLE_SCHEMA,
        "version": 1,
        "bundle_name": Path(paths["bundle_file"]).stem,
        "meta": meta,
        "storage_state": storage_state,
        "session_storage": {
            "version": 1,
            "captured_at": now_iso_utc(),
            "by_origin": session_by_origin,
        },
        "capture_context": {
            "mode": args.mode,
            "url": args.url,
            "created_at": now_iso_utc(),
        },
    }


def capture_command(args: argparse.Namespace) -> int:
    args.url = ensure_url(args.url)
    paths = resolve_bundle_paths(args)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Output dir: {paths['output_dir']}")
    print(f"[INFO] Bundle file: {paths['bundle_file']}")
    print(f"[INFO] Mode: {args.mode}")
    print(f"[INFO] URL: {args.url}")

    context = None
    browser = None
    try:
        with sync_playwright() as playwright:
            context, page, browser = launch_context_and_page(playwright, args, paths)
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)

            if args.mode == "popup":
                print("[ACTION] In the browser, dismiss popup/consent/country prompts as needed.")
            else:
                print("[ACTION] In the browser, complete login/OTP/account steps.")

            input("[PROMPT] Press Enter after you finish manual actions and page is stable... ")

            # Save Playwright storage state in-memory first.
            try:
                state_payload = context.storage_state(indexed_db=True)
                indexed_db_saved = True
            except TypeError:
                state_payload = context.storage_state()
                indexed_db_saved = False

            normalized_state = normalize_storage_state_payload(state_payload)

            session_entries: dict[str, dict[str, str]] = {}
            parsed_url = urlparse(page.url)
            current_origin = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.scheme else ""
            if current_origin:
                raw_session = page.evaluate(SESSION_EXTRACT_JS)
                if isinstance(raw_session, dict) and raw_session:
                    session_entries[current_origin] = {
                        str(k): "" if v is None else str(v)
                        for k, v in raw_session.items()
                        if isinstance(k, str)
                    }

            cookies = normalized_state.get("cookies", [])
            cookie_summary = summarize_cookies(cookies if isinstance(cookies, list) else [])

            meta = {
                "version": 1,
                "created_at": now_iso_utc(),
                "mode": args.mode,
                "url": args.url,
                "browser": args.browser,
                "headless": args.headless,
                "persistent_profile": args.persistent_profile,
                "service_workers": args.service_workers,
                "reuse_existing": args.reuse_existing,
                "indexed_db_saved": indexed_db_saved,
                "cookie_summary": cookie_summary,
                "session_origin_count": len(session_entries),
            }

            bundle_payload = build_bundle_payload(
                args=args,
                paths=paths,
                meta=meta,
                storage_state=normalized_state,
                session_by_origin=session_entries,
            )
            write_json(paths["bundle_file"], bundle_payload)

            if args.keep_open_seconds > 0:
                print(f"[INFO] Keeping browser open for {args.keep_open_seconds} second(s)...")
                time.sleep(args.keep_open_seconds)

            print(f"[DONE] Saved bundle JSON: {paths['bundle_file']}")
            return 0

    except PlaywrightTimeoutError as exc:
        print(f"[ERROR] Timeout while loading page: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\n[ERROR] Interrupted by user.")
        return 130
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def verify_command(args: argparse.Namespace) -> int:
    args.url = ensure_url(args.url)
    paths = resolve_bundle_paths(args)

    if not paths["bundle_file"].exists():
        print(f"[ERROR] Missing bundle file: {paths['bundle_file']}")
        return 1

    context = None
    browser = None
    try:
        with sync_playwright() as playwright:
            context, page, browser = launch_context_and_page(playwright, args, paths)
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_timeout(args.post_wait_ms)

            checks: dict[str, Any] = {
                "url_after_load": page.url,
                "has_bundle_file": True,
            }

            if args.expect_visible_css:
                visible = page.locator(args.expect_visible_css).first.is_visible()
                checks["expect_visible_css"] = {"selector": args.expect_visible_css, "visible": visible}
                if not visible:
                    print(f"[ERROR] Expected visible selector not found: {args.expect_visible_css}")
                    page.screenshot(path=str(paths["verify_screenshot"]), full_page=True)
                    return 3

            if args.reject_visible_css:
                visible = page.locator(args.reject_visible_css).first.is_visible()
                checks["reject_visible_css"] = {"selector": args.reject_visible_css, "visible": visible}
                if visible:
                    print(f"[ERROR] Rejected selector is visible: {args.reject_visible_css}")
                    page.screenshot(path=str(paths["verify_screenshot"]), full_page=True)
                    return 4

            dialog_probe = page.evaluate(DIALOG_SCAN_JS)
            if isinstance(dialog_probe, dict):
                checks["dialog_probe"] = dialog_probe

            page.screenshot(path=str(paths["verify_screenshot"]), full_page=True)
            print(f"[DONE] Verification screenshot: {paths['verify_screenshot']}")
            print(json.dumps(checks, indent=2))
            return 0

    except PlaywrightTimeoutError as exc:
        print(f"[ERROR] Timeout while loading page: {exc}")
        return 2
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and verify popup/auth browser state for local user-driven flows.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared_args(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--url", required=True, help="Target URL to open in browser.")
        cmd.add_argument("--mode", choices=("popup", "auth"), required=True, help="Capture mode.")
        cmd.add_argument(
            "--bundle",
            help=(
                "Bundle JSON filename. If omitted, static default is state_bundle.json "
                "inside --output-dir."
            ),
        )
        cmd.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Bundle output directory.")
        cmd.add_argument("--browser", choices=SUPPORTED_BROWSERS, default="chromium")
        cmd.add_argument("--headless", action="store_true", help="Run headless (not recommended for manual capture).")
        cmd.add_argument("--timeout-ms", type=int, default=60000, help="Navigation timeout in milliseconds.")
        cmd.add_argument("--service-workers", choices=("block", "allow"), default="block")
        cmd.add_argument("--persistent-profile", action="store_true", help="Use a persistent browser profile directory.")
        cmd.add_argument("--profile-dir", help="Custom profile directory (used with --persistent-profile).")
        cmd.add_argument("--reuse-existing", dest="reuse_existing", action="store_true", default=True)
        cmd.add_argument("--no-reuse-existing", dest="reuse_existing", action="store_false")

    capture = subparsers.add_parser("capture", help="Open site, let user interact, save state file.")
    add_shared_args(capture)
    capture.add_argument(
        "--keep-open-seconds",
        type=int,
        default=0,
        help="Keep browser open after save (seconds). Useful for inspection.",
    )

    verify = subparsers.add_parser("verify", help="Load saved state and verify it on target URL.")
    add_shared_args(verify)
    verify.add_argument("--expect-visible-css", help="Fail if this CSS selector is not visible.")
    verify.add_argument("--reject-visible-css", help="Fail if this CSS selector is visible.")
    verify.add_argument("--post-wait-ms", type=int, default=2500, help="Extra wait after navigation.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "capture":
        return capture_command(args)
    if args.command == "verify":
        return verify_command(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
