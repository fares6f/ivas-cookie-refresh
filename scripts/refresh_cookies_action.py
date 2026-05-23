"""
Runs inside a GitHub Actions Ubuntu runner.

Logs into ivasms.com via Camoufox routed through a residential proxy
(Cloudflare blocks Azure datacenter IPs that GH Actions runs on).
Captures fresh cookies + UA, uploads them to a private Gist.

Required env vars:
    IVASMS_EMAIL
    IVASMS_PASSWORD
    GH_TOKEN          - GitHub PAT with `gist` scope
    GIST_ID           - the target private Gist's id
    PROXIES           - newline-separated list of host:port:user:pass
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from typing import Any, Optional

import requests
from camoufox.sync_api import Camoufox

LOGIN_URL = "https://www.ivasms.com/login"
PORTAL_URL = "https://www.ivasms.com/portal"
PORTAL_HINT = "/portal"

CHALLENGE_WAIT_SECS = 120
LOGIN_WAIT_SECS = 90
PROXY_ATTEMPTS = 4


def must_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"X env var {name} is required")
    return val


def load_proxies() -> list[dict]:
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue
        host, port, user, pwd = parts
        out.append({
            "server": f"http://{host}:{port}",
            "username": user,
            "password": pwd,
            "label": f"{host}:{port}",
        })
    random.shuffle(out)
    return out


def has_cf_clearance(cookies: list[dict]) -> bool:
    return any(c.get("name") == "cf_clearance" and c.get("value") for c in cookies)


def wait_for_cf_clearance(page, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            cookies = page.context.cookies("https://www.ivasms.com")
        except Exception:
            cookies = []
        if has_cf_clearance(cookies):
            return True
        try:
            page.mouse.move(random.randint(50, 400), random.randint(80, 300))
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_for_login_form(page, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            handle = page.query_selector("input[name='email']")
            if handle and handle.is_visible():
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def try_with_proxy(proxy: Optional[dict]) -> Optional[dict[str, Any]]:
    """One attempt: returns payload on success, None on failure."""
    label = proxy["label"] if proxy else "no-proxy"
    print(f"\n=> attempt via {label}", flush=True)

    kwargs: dict[str, Any] = dict(
        headless="virtual",
        humanize=True,
        os=("windows",),
        locale=("en-US",),
        geoip=True,
    )
    if proxy:
        kwargs["proxy"] = {
            "server": proxy["server"],
            "username": proxy["username"],
            "password": proxy["password"],
        }

    try:
        with Camoufox(**kwargs) as browser:
            page = browser.new_page()
            page.set_default_timeout(60_000)

            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                print(f"!! initial goto raised: {e}", flush=True)
                return None

            if not wait_for_cf_clearance(page, time.time() + CHALLENGE_WAIT_SECS):
                print("!! cf_clearance never appeared via this proxy", flush=True)
                return None
            print("=> cf_clearance acquired", flush=True)

            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass

            if not wait_for_login_form(page, time.time() + LOGIN_WAIT_SECS):
                print("!! login form did not appear", flush=True)
                return None

            print("=> typing credentials...", flush=True)
            email = must_env("IVASMS_EMAIL")
            password = must_env("IVASMS_PASSWORD")
            page.fill("input[name='email']", email)
            page.fill("input[name='password']", password)
            page.click("button[type='submit']")

            deadline = time.time() + 120
            while time.time() < deadline:
                url = page.url
                if PORTAL_HINT in url and "/login" not in url:
                    break
                time.sleep(2)
            else:
                print(f"!! never reached /portal (last url={page.url})", flush=True)
                return None

            try:
                page.goto(f"{PORTAL_URL}/sms/received",
                          wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass

            print(f"=> logged in: {page.url}", flush=True)

            cookies = page.context.cookies("https://www.ivasms.com")
            try:
                user_agent = page.evaluate("() => navigator.userAgent") or ""
            except Exception:
                user_agent = ""

            return {
                "cookies": cookies,
                "user_agent": user_agent,
                "captured_at": int(time.time()),
            }
    except Exception as e:
        print(f"!! attempt errored: {e}", flush=True)
        return None


def normalise(cookies: list[dict]) -> list[dict]:
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
    proxies = load_proxies()
    if not proxies:
        sys.exit("X PROXIES env var is empty")

    print(f"=> {len(proxies)} proxies loaded; will try up to {PROXY_ATTEMPTS}", flush=True)

    payload: Optional[dict] = None
    for proxy in proxies[:PROXY_ATTEMPTS]:
        payload = try_with_proxy(proxy)
        if payload:
            break

    if not payload:
        sys.exit("X every proxy attempt failed")

    cookies = normalise(payload["cookies"])
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
