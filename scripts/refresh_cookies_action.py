"""
Runs inside a GitHub Actions Ubuntu runner.

Logs into ivasms.com using Camoufox (a Firefox-based stealth browser that
ships as a tiny Linux binary - no full Chrome install needed), captures the
fresh cookies + User-Agent, then uploads them to a private GitHub Gist so
the bot running on hidencloud (no browser, no terminal access) can pick
them up via simple HTTPS polling.

Required env vars:
    IVASMS_EMAIL
    IVASMS_PASSWORD
    GH_TOKEN          - GitHub PAT with `gist` scope
    GIST_ID           - the target private Gist's id
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests
from camoufox.sync_api import Camoufox

LOGIN_URL = "https://www.ivasms.com/login"
PORTAL_URL = "https://www.ivasms.com/portal"
PORTAL_HINT = "/portal"
TIMEOUT_SECS = 180


def must_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"X env var {name} is required")
    return val


def grab_cookies() -> dict[str, Any]:
    email = must_env("IVASMS_EMAIL")
    password = must_env("IVASMS_PASSWORD")

    print("=> launching Camoufox (virtual display)...", flush=True)
    cookies: list[dict] = []
    user_agent: str = ""

    with Camoufox(
        headless="virtual",   # Xvfb on Linux runners
        humanize=True,        # natural mouse + typing rhythm
    ) as browser:
        page = browser.new_page()
        print(f"=> opening {LOGIN_URL}", flush=True)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

        # Cloudflare may show a challenge. Camoufox passes it; we just wait
        # until the form is interactive.
        try:
            page.wait_for_load_state("networkidle", timeout=60_000)
        except Exception as e:
            print(f"!! networkidle wait timed out: {e}", flush=True)

        try:
            page.wait_for_selector("input[name='email']", timeout=90_000)
        except Exception as e:
            try:
                html = page.content()[:1500]
            except Exception:
                html = "<could not read page content>"
            sys.exit(f"X login form not found: {e}\n--- page (truncated) ---\n{html}")

        print("=> typing credentials...", flush=True)
        page.fill("input[name='email']", email)
        page.fill("input[name='password']", password)
        page.click("button[type='submit']")

        # Wait for redirect to /portal (Cloudflare may interject)
        deadline = time.time() + TIMEOUT_SECS
        while time.time() < deadline:
            url = page.url
            if PORTAL_HINT in url and "/login" not in url:
                break
            time.sleep(2)
        else:
            sys.exit(f"X never reached /portal (last url={page.url})")

        # Touch a couple of pages so the AJAX/CSRF token is also alive
        try:
            page.goto(f"{PORTAL_URL}/sms/received", wait_until="domcontentloaded",
                      timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

        print(f"=> logged in: {page.url}", flush=True)

        cookies = page.context.cookies("https://www.ivasms.com")
        try:
            user_agent = page.evaluate("() => navigator.userAgent") or ""
        except Exception:
            user_agent = ""

    print(f"=> captured {len(cookies)} cookies", flush=True)
    if user_agent:
        print(f"=> user-agent: {user_agent[:80]}...", flush=True)

    return {
        "cookies": cookies,
        "user_agent": user_agent,
        "captured_at": int(time.time()),
    }


def normalise_for_client(cookies: list[dict]) -> list[dict]:
    """Match the shape the bot expects: name/value/domain/path only."""
    out: list[dict] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        out.append({
            "name": name,
            "value": value,
            "domain": c.get("domain") or "www.ivasms.com",
            "path": c.get("path") or "/",
        })
    return out


def has_critical(cookies: list[dict]) -> bool:
    names = {c.get("name") for c in cookies}
    # cf_clearance + a Laravel session = a working login
    return "cf_clearance" in names and any(
        n in names for n in ("ivas_sms_session", "laravel_session", "XSRF-TOKEN")
    )


def upload_to_gist(payload: dict) -> None:
    token = must_env("GH_TOKEN")
    gist_id = must_env("GIST_ID")

    cookies_text = json.dumps(payload["cookies"], indent=2, ensure_ascii=False)
    body = {
        "files": {
            "cookies.json": {"content": cookies_text},
            "user_agent.txt": {"content": payload["user_agent"] or "Mozilla/5.0"},
            "captured_at.txt": {"content": str(payload["captured_at"])},
        }
    }

    url = f"https://api.github.com/gists/{gist_id}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = requests.patch(url, headers=headers, json=body, timeout=30)
    if r.status_code >= 300:
        sys.exit(f"X Gist update failed [{r.status_code}]: {r.text[:600]}")
    print(f"=> cookies uploaded to gist {gist_id}", flush=True)


def main() -> None:
    payload = grab_cookies()
    cookies = normalise_for_client(payload["cookies"])
    if not cookies:
        sys.exit("X no cookies captured")
    if not has_critical(cookies):
        names = [c.get("name") for c in cookies]
        sys.exit(f"X login looks incomplete; got {names}")
    payload["cookies"] = cookies
    upload_to_gist(payload)
    print("== done ==", flush=True)


if __name__ == "__main__":
    main()
