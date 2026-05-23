"""
Runs inside a GitHub Actions Ubuntu runner.

Logs into ivasms.com using Camoufox (Firefox-based stealth browser),
captures fresh cookies + UA, uploads them to a private GitHub Gist.

Cloudflare on GH Actions Azure IPs throws hard challenges, so we patiently
wait for cf_clearance to land before touching the form.

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

# Cloudflare needs time on Azure IPs (GH Actions). Be generous.
CHALLENGE_WAIT_SECS = 240
LOGIN_WAIT_SECS = 240


def must_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"X env var {name} is required")
    return val


def has_cf_clearance(cookies: list[dict]) -> bool:
    return any(c.get("name") == "cf_clearance" and c.get("value") for c in cookies)


def wait_for_cf_clearance(page, deadline: float) -> bool:
    """Spin until cf_clearance lands in the cookie jar."""
    while time.time() < deadline:
        try:
            cookies = page.context.cookies("https://www.ivasms.com")
        except Exception:
            cookies = []
        if has_cf_clearance(cookies):
            return True
        try:
            # Nudge the page so Cloudflare's challenge keeps progressing
            page.mouse.move(100, 100)
            page.mouse.move(300, 220)
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_for_login_form(page, deadline: float) -> bool:
    """Wait for the email field to appear, retrying through CF redirects."""
    while time.time() < deadline:
        try:
            handle = page.query_selector("input[name='email']")
            if handle and handle.is_visible():
                return True
        except Exception:
            pass
        try:
            url = page.url
        except Exception:
            url = ""
        if "challenges.cloudflare.com" in url or "cdn-cgi" in url:
            time.sleep(3)
            continue
        if "/login" not in url and PORTAL_HINT not in url:
            # Got bounced somewhere weird - try a hard reload
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass
        time.sleep(2)
    return False


def grab_cookies() -> dict[str, Any]:
    email = must_env("IVASMS_EMAIL")
    password = must_env("IVASMS_PASSWORD")

    print("=> launching Camoufox (virtual display)...", flush=True)
    cookies: list[dict] = []
    user_agent: str = ""

    with Camoufox(
        headless="virtual",
        humanize=True,
        os=("windows",),
        geoip=True,
        locale=("en-US",),
    ) as browser:
        page = browser.new_page()
        page.set_default_timeout(60_000)

        print(f"=> opening {LOGIN_URL}", flush=True)
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"!! initial goto raised: {e}", flush=True)

        # Pass the Cloudflare challenge
        cf_deadline = time.time() + CHALLENGE_WAIT_SECS
        if wait_for_cf_clearance(page, cf_deadline):
            print("=> cf_clearance acquired", flush=True)
        else:
            print("!! cf_clearance never appeared, trying anyway", flush=True)

        # Sometimes the form needs an explicit nudge after the challenge
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass

        login_deadline = time.time() + LOGIN_WAIT_SECS
        if not wait_for_login_form(page, login_deadline):
            try:
                page.screenshot(path="/tmp/cf_blocked.png", full_page=True)
                print("!! screenshot saved /tmp/cf_blocked.png", flush=True)
            except Exception:
                pass
            try:
                html = page.content()[:1500]
            except Exception:
                html = "<no content>"
            sys.exit(f"X login form never appeared\n--- (truncated) ---\n{html}")

        print("=> typing credentials...", flush=True)
        page.fill("input[name='email']", email)
        page.fill("input[name='password']", password)
        page.click("button[type='submit']")

        # Wait for /portal redirect
        deadline = time.time() + 180
        while time.time() < deadline:
            url = page.url
            if PORTAL_HINT in url and "/login" not in url:
                break
            time.sleep(2)
        else:
            sys.exit(f"X never reached /portal (last url={page.url})")

        try:
            page.goto(f"{PORTAL_URL}/sms/received",
                      wait_until="domcontentloaded", timeout=60_000)
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
