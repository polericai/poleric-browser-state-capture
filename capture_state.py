#!/usr/bin/env python3
"""Interactive Playwright state capture utility for Poleric users.

This tool supports four workflows:
1. popup-state capture (broad state minus cart data)
2. auth-state capture (broad state minus cart data)
3. general-state capture (broad state minus cart data)
4. raw-state capture (full exact browser state)

Output is a single JSON bundle that includes:
- Playwright storage_state (cookies/localStorage/indexedDB)
- sessionStorage by origin
- metadata
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_OUTPUT_DIR = "browser_state"
SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")
BUNDLE_SCHEMA = "poleric.browser_state_bundle.v1"
SUPPORTED_MODES = ("popup", "auth", "general", "raw")

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


def ensure_json_filename(name: str) -> str:
    return name if name.lower().endswith(".json") else f"{name}.json"


def domain_main_name(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or parsed.netloc or parsed.path or "").lower()
    host = host.replace("www.", "")
    labels = [label for label in host.split(".") if label]
    if not labels:
        return "site"

    multipart_suffixes = {
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.in",
        "com.br",
        "com.mx",
        "co.jp",
        "co.kr",
    }

    if len(labels) >= 3:
        suffix = ".".join(labels[-2:])
        if suffix in multipart_suffixes:
            return slugify(labels[-3])

    if len(labels) >= 2:
        return slugify(labels[-2])

    return slugify(labels[0])


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

    normalized: dict[str, Any] = {
        "cookies": cookies if isinstance(cookies, list) else [],
        "origins": origins if isinstance(origins, list) else [],
    }

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
AUTH_KEYWORDS = (
    "auth",
    "login",
    "logged",
    "session",
    "sess",
    "token",
    "jwt",
    "customer",
    "account",
    "user",
    "member",
    "secure_customer_sig",
    "password",
    "otp",
    "access",
    "refresh",
    "id_token",
    "firebase",
    "supabase",
    "next-auth",
    "csrf",
    "xsrf",
    "remember",
    "identity",
    "credential",
)

DATA_KEYWORDS = (
    "cart",
    "checkout",
    "basket",
    "bag",
    "wishlist",
)

def key_matches(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def item_identity(name: str, extra: str = "") -> str:
    return f"{name or ''} {extra or ''}".strip().lower()


def should_keep_state_item(identity: str, mode: str) -> bool:
    """Decide whether a cookie/storage key should be saved for a mode.

    Mode meaning:
    - popup: broad state minus cart data
    - auth: broad state minus cart data
    - general: broad state minus cart data
    - raw: full raw state
    """
    if mode == "raw":
        return True

    is_cart_data = key_matches(identity, DATA_KEYWORDS)

    if mode in {"popup", "auth", "general"}:
        return not is_cart_data

    return True


def count_local_storage_items(storage_state: dict[str, Any]) -> int:
    count = 0
    origins = storage_state.get("origins") if isinstance(storage_state, dict) else []
    if not isinstance(origins, list):
        return 0
    for entry in origins:
        if not isinstance(entry, dict):
            continue
        items = entry.get("localStorage")
        if isinstance(items, list):
            count += len(items)
    return count


def count_session_storage_items(session_by_origin: dict[str, dict[str, str]]) -> int:
    return sum(len(values) for values in session_by_origin.values() if isinstance(values, dict))


def filter_cookies(cookies: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if mode == "raw":
        return cookies

    filtered: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        identity = item_identity(name, domain)
        if should_keep_state_item(identity, mode):
            filtered.append(cookie)
    return filtered


def filter_local_storage_items(items: Any, mode: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    if mode == "raw":
        return [item for item in items if isinstance(item, dict)]

    filtered: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        identity = item_identity(name, value[:120])
        if should_keep_state_item(identity, mode):
            filtered.append(item)
    return filtered


def filter_session_storage(
    session_by_origin: dict[str, dict[str, str]],
    mode: str,
) -> dict[str, dict[str, str]]:
    if mode == "raw":
        return session_by_origin

    filtered_by_origin: dict[str, dict[str, str]] = {}
    for origin, values in session_by_origin.items():
        if not isinstance(origin, str) or not isinstance(values, dict):
            continue
        filtered_values: dict[str, str] = {}
        for key, value in values.items():
            identity = item_identity(str(key), str(value)[:120])
            if should_keep_state_item(identity, mode):
                filtered_values[str(key)] = "" if value is None else str(value)
        if filtered_values:
            filtered_by_origin[origin] = filtered_values
    return filtered_by_origin


def filter_storage_state(
    storage_state: dict[str, Any],
    mode: str,
    *,
    keep_indexed_db: bool = False,
) -> dict[str, Any]:
    if mode == "raw":
        return storage_state

    normalized = normalize_storage_state_payload(storage_state)
    filtered_state: dict[str, Any] = {}

    for key, value in normalized.items():
        if key not in {"cookies", "origins"}:
            filtered_state[key] = value

    raw_cookies = normalized.get("cookies")
    cookies = raw_cookies if isinstance(raw_cookies, list) else []
    filtered_state["cookies"] = filter_cookies(cookies, mode)

    filtered_origins: list[dict[str, Any]] = []
    origins = normalized.get("origins")
    if isinstance(origins, list):
        for entry in origins:
            if not isinstance(entry, dict):
                continue
            origin = entry.get("origin")
            if not isinstance(origin, str):
                continue

            new_entry: dict[str, Any] = {"origin": origin}
            filtered_local_storage = filter_local_storage_items(entry.get("localStorage"), mode)
            if filtered_local_storage:
                new_entry["localStorage"] = filtered_local_storage

            # IndexedDB cannot be filtered safely at key level from Playwright's
            # storage_state output. Keep it only for raw mode by default.
            # Use --keep-indexed-db if a site needs IndexedDB for a sanitized mode.
            if keep_indexed_db and "indexedDB" in entry:
                new_entry["indexedDB"] = entry["indexedDB"]

            if len(new_entry) > 1:
                filtered_origins.append(new_entry)

    filtered_state["origins"] = filtered_origins
    return filtered_state


def sanitize_bundle_state(
    *,
    storage_state: dict[str, Any],
    session_by_origin: dict[str, dict[str, str]],
    mode: str,
    keep_indexed_db: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, str]], dict[str, Any]]:
    before_cookies = len(storage_state.get("cookies", [])) if isinstance(storage_state.get("cookies"), list) else 0
    before_local_storage = count_local_storage_items(storage_state)
    before_session_storage = count_session_storage_items(session_by_origin)

    sanitized_storage_state = filter_storage_state(storage_state, mode, keep_indexed_db=keep_indexed_db)
    sanitized_session_storage = filter_session_storage(session_by_origin, mode)

    after_cookies = len(sanitized_storage_state.get("cookies", [])) if isinstance(sanitized_storage_state.get("cookies"), list) else 0
    after_local_storage = count_local_storage_items(sanitized_storage_state)
    after_session_storage = count_session_storage_items(sanitized_session_storage)

    report: dict[str, Any] = {
        "mode": mode,
        "keep_indexed_db": keep_indexed_db,
        "cookies_before": before_cookies,
        "cookies_after": after_cookies,
        "cookies_removed": max(before_cookies - after_cookies, 0),
        "local_storage_before": before_local_storage,
        "local_storage_after": after_local_storage,
        "local_storage_removed": max(before_local_storage - after_local_storage, 0),
        "session_storage_before": before_session_storage,
        "session_storage_after": after_session_storage,
        "session_storage_removed": max(before_session_storage - after_session_storage, 0),
    }

    if mode != "raw" and not keep_indexed_db:
        report["indexed_db_note"] = "IndexedDB removed for sanitized modes unless --keep-indexed-db is used."

    return sanitized_storage_state, sanitized_session_storage, report


def find_bundle_family_files(bundle_file: Path) -> list[tuple[int, Path]]:
    pattern = re.compile(rf"^{re.escape(bundle_file.stem)}(?:-(\d+))?{re.escape(bundle_file.suffix)}$")
    matches: list[tuple[int, Path]] = []

    for candidate in bundle_file.parent.glob(f"{bundle_file.stem}*{bundle_file.suffix}"):
        match = pattern.match(candidate.name)
        if not match:
            continue
        version = int(match.group(1)) if match.group(1) else 1
        matches.append((version, candidate))

    return sorted(matches, key=lambda item: item[0])


def next_versioned_bundle_file(bundle_file: Path) -> Path:
    matches = find_bundle_family_files(bundle_file)
    if not matches:
        return bundle_file

    next_version = matches[-1][0] + 1
    return bundle_file.with_name(f"{bundle_file.stem}-{next_version}{bundle_file.suffix}")


def latest_existing_bundle_file(bundle_file: Path) -> Path:
    matches = find_bundle_family_files(bundle_file)
    if not matches:
        return bundle_file
    return matches[-1][1]


def resolve_bundle_paths(args: argparse.Namespace) -> dict[str, Path]:
    output_dir = Path(args.output_dir).resolve()
    mode = normalized_mode(args.mode)

    if args.file_name:
        file_name = ensure_json_filename(Path(args.file_name).name)
    else:
        domain = domain_main_name(args.url)
        if mode == "popup":
            file_name = f"{domain}-popup-state.json"
        elif mode == "auth":
            file_name = f"{domain}-auth-state.json"
        elif mode == "raw":
            file_name = f"{domain}-raw-state.json"
        else:
            file_name = f"{domain}-state.json"

    bundle_file = output_dir / file_name
    existing_bundle_file = latest_existing_bundle_file(bundle_file)
    if args.command == "capture":
        bundle_file = next_versioned_bundle_file(bundle_file)
    bundle_key = Path(file_name).stem or domain_main_name(args.url)

    if args.profile_dir:
        profile_dir = Path(args.profile_dir).resolve()
    else:
        profile_dir = output_dir / f"{bundle_key}_profile"

    return {
        "output_dir": output_dir,
        "bundle_file": bundle_file,
        "existing_bundle_file": existing_bundle_file,
        "profile_dir": profile_dir,
    }


def ensure_url(url: str) -> str:
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return f"https://{url}"
    return url


def normalized_mode(raw_mode: str | None) -> str:
    if raw_mode in SUPPORTED_MODES:
        return raw_mode
    return "general"


def parse_bundle_payload(payload: Any) -> tuple[dict[str, Any], dict[str, dict[str, str]], dict[str, Any]]:
    # Legacy support: raw Playwright storage_state JSON.
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

    existing_bundle_file = paths.get("existing_bundle_file", paths["bundle_file"])
    reuse_bundle = args.reuse_existing and existing_bundle_file.exists()
    if reuse_bundle:
        storage_state_payload, session_by_origin, _ = load_existing_bundle(existing_bundle_file)

    storage_map_for_inject = local_storage_map_from_state(storage_state_payload)
    cookies_for_add: list[dict[str, Any]] = [
        c for c in storage_state_payload.get("cookies", []) if isinstance(c, dict)
    ]

    browser = None
    temporary_profile_dir: Path | None = None
    if args.persistent_profile:
        profile_dir = paths["profile_dir"]
        if not reuse_bundle:
            temp_profiles_root = paths["output_dir"] / ".tmp_profiles"
            temp_profiles_root.mkdir(parents=True, exist_ok=True)
            temporary_profile_dir = Path(
                tempfile.mkdtemp(prefix=f"{paths['bundle_file'].stem}-", dir=str(temp_profiles_root))
            )
            profile_dir = temporary_profile_dir
        context = browser_type.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            service_workers=args.service_workers,
        )
    else:
        browser = browser_type.launch(headless=args.headless)
        context_kwargs: dict[str, Any] = {
            "service_workers": args.service_workers,
        }
        if reuse_bundle:
            context_kwargs["storage_state"] = storage_state_payload
        context = browser.new_context(**context_kwargs)

    if session_by_origin:
        context.add_init_script(script=build_storage_init_script(session_by_origin, "sessionStorage"))

    if args.persistent_profile and reuse_bundle:
        if cookies_for_add:
            context.add_cookies(cookies_for_add)
        if storage_map_for_inject:
            context.add_init_script(script=build_storage_init_script(storage_map_for_inject, "localStorage"))

    page = context.pages[0] if context.pages else context.new_page()
    return context, page, browser, temporary_profile_dir


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
    args.mode = normalized_mode(args.mode)
    paths = resolve_bundle_paths(args)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Output dir: {paths['output_dir']}")
    print(f"[INFO] Bundle file: {paths['bundle_file']}")
    print(f"[INFO] Mode: {args.mode}")
    print(f"[INFO] URL: {args.url}")
    print(f"[INFO] Clean capture: {not args.reuse_existing}")

    context = None
    browser = None
    temporary_profile_dir: Path | None = None
    try:
        with sync_playwright() as playwright:
            context, page, browser, temporary_profile_dir = launch_context_and_page(playwright, args, paths)
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)

            if args.mode == "popup":
                print("[ACTION] Dismiss popup/consent/country/newsletter prompts only. Avoid cart activity.")
            elif args.mode == "auth":
                print("[ACTION] Dismiss popups and complete login/OTP/account steps. Avoid cart activity.")
            elif args.mode == "general":
                print("[ACTION] Do any setup needed. General mode removes cart data only.")
            else:
                print("[ACTION] Do any setup needed. Raw mode saves full exact browser state.")

            input("[PROMPT] Press Enter after you finish manual actions and page is stable... ")

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

            normalized_state, session_entries, filter_report = sanitize_bundle_state(
                storage_state=normalized_state,
                session_by_origin=session_entries,
                mode=args.mode,
                keep_indexed_db=args.keep_indexed_db,
            )

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
                "filter_report": filter_report,
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
        if temporary_profile_dir:
            shutil.rmtree(temporary_profile_dir, ignore_errors=True)


def verify_command(args: argparse.Namespace) -> int:
    args.url = ensure_url(args.url)
    args.mode = normalized_mode(args.mode)
    # Verify must load the previously saved bundle, even though capture defaults to clean.
    args.reuse_existing = True
    paths = resolve_bundle_paths(args)
    paths["bundle_file"] = paths["existing_bundle_file"]

    if not paths["bundle_file"].exists():
        print(f"[ERROR] Missing bundle file: {paths['bundle_file']}")
        return 1

    context = None
    browser = None
    temporary_profile_dir: Path | None = None
    try:
        with sync_playwright() as playwright:
            context, page, browser, temporary_profile_dir = launch_context_and_page(playwright, args, paths)
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
                    return 3

            if args.reject_visible_css:
                visible = page.locator(args.reject_visible_css).first.is_visible()
                checks["reject_visible_css"] = {"selector": args.reject_visible_css, "visible": visible}
                if visible:
                    print(f"[ERROR] Rejected selector is visible: {args.reject_visible_css}")
                    return 4

            dialog_probe = page.evaluate(DIALOG_SCAN_JS)
            if isinstance(dialog_probe, dict):
                checks["dialog_probe"] = dialog_probe

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
        if temporary_profile_dir:
            shutil.rmtree(temporary_profile_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and verify popup/auth/general/raw browser state for local user-driven flows.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared_args(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--url", required=True, help="Target URL to open in browser.")
        cmd.add_argument(
            "--mode",
            choices=SUPPORTED_MODES,
            required=False,
            help="Optional mode tag. Use popup, auth, general, or raw. Default is general.",
        )
        cmd.add_argument(
            "--file-name",
            help=(
                "Output JSON filename. If omitted, defaults to <domain>-state.json, "
                "<domain>-popup-state.json, <domain>-auth-state.json, or "
                "<domain>-raw-state.json based on mode."
            ),
        )
        cmd.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Output directory for state bundle files.")
        cmd.add_argument("--browser", choices=SUPPORTED_BROWSERS, default="chromium")
        cmd.add_argument("--headless", action="store_true", help="Run headless (not recommended for manual capture).")
        cmd.add_argument("--timeout-ms", type=int, default=60000, help="Navigation timeout in milliseconds.")
        cmd.add_argument("--service-workers", choices=("block", "allow"), default="block")
        cmd.add_argument(
            "--keep-indexed-db",
            action="store_true",
            help=(
                "Keep IndexedDB in sanitized modes. By default IndexedDB is only kept in raw mode "
                "because it cannot be filtered safely by key."
            ),
        )
        cmd.add_argument("--persistent-profile", action="store_true", help="Use a persistent browser profile directory.")
        cmd.add_argument("--profile-dir", help="Custom profile directory (used with --persistent-profile).")
        cmd.add_argument(
            "--reuse-existing",
            dest="reuse_existing",
            action="store_true",
            default=False,
            help=(
                "Load an existing saved bundle before opening the page. "
                "Default is false, so capture starts from a clean incognito-like Playwright context."
            ),
        )
        cmd.add_argument(
            "--no-reuse-existing",
            dest="reuse_existing",
            action="store_false",
            help="Do not load an existing saved bundle before opening the page.",
        )

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
    verify.add_argument("--post-wait-ms", type=int, default=5000, help="Extra wait after navigation.")

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
