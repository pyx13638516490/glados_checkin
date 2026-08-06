import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


DEFAULT_BASE_URL = "https://glados.cloud"
DEFAULT_CHECKIN_TOKEN = "glados.one"
TIMEOUT = (10, 30)
MAX_ATTEMPTS = 4


def now_text():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")


def log(message):
    print(f"[{now_text()}] {message}", flush=True)


def get_env(name, default=None):
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
    return value


def require_cookie():
    cookie = get_env("GLADOS_COOKIE")
    if not cookie:
        raise RuntimeError(
            "GLADOS_COOKIE is empty. Update the GitHub Actions repository secret."
        )
    if "koa:sess" not in cookie or "koa:sess.sig" not in cookie:
        log("Warning: GLADOS_COOKIE does not appear to contain koa:sess and koa:sess.sig.")
    return cookie


def build_session(cookie):
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://glados.cloud",
            "Referer": "https://glados.cloud/console/checkin",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Cookie": cookie,
        }
    )
    return session


def parse_json_response(response):
    try:
        return response.json()
    except ValueError:
        return {
            "code": None,
            "message": "Non-JSON response",
            "text": response.text[:1000],
        }


def request_json(session, method, url, **kwargs):
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"{method.upper()} {url} (attempt {attempt}/{MAX_ATTEMPTS})")
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
            payload = parse_json_response(response)
            log(f"HTTP {response.status_code}: {json.dumps(payload, ensure_ascii=False)}")

            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Authentication failed. Refresh GLADOS_COOKIE in GitHub Secrets."
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"Retryable HTTP status: {response.status_code}")
                raise last_error

            response.raise_for_status()
            return payload

        except requests.RequestException as exc:
            last_error = exc
            log(f"Request failed: {exc}")
        except RuntimeError as exc:
            if "Authentication failed" in str(exc):
                raise
            last_error = exc
            log(str(exc))

        if attempt < MAX_ATTEMPTS:
            sleep_seconds = min(60, 2 ** attempt * 5)
            log(f"Sleeping {sleep_seconds}s before retry.")
            time.sleep(sleep_seconds)

    raise RuntimeError(f"Request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def is_auth_error(payload):
    text = json.dumps(payload, ensure_ascii=False).lower()
    return any(
        marker in text
        for marker in (
            "not login",
            "not logged",
            "login",
            "unauthorized",
            "permission",
            "forbidden",
            "cookie",
        )
    )


def left_days_from_status(payload):
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("leftDays")
    return None


def main():
    cookie = require_cookie()
    base_url = get_env("GLADOS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    token = get_env("GLADOS_CHECKIN_TOKEN", DEFAULT_CHECKIN_TOKEN)

    checkin_url = f"{base_url}/api/user/checkin"
    status_url = f"{base_url}/api/user/status"
    session = build_session(cookie)

    checkin_payload = request_json(
        session,
        "post",
        checkin_url,
        data=json.dumps({"token": token}),
    )

    if is_auth_error(checkin_payload):
        raise RuntimeError("Checkin API says the cookie is invalid. Refresh GLADOS_COOKIE.")

    status_payload = request_json(session, "get", status_url)
    if is_auth_error(status_payload):
        raise RuntimeError("Status API says the cookie is invalid. Refresh GLADOS_COOKIE.")

    left_days = left_days_from_status(status_payload)
    if left_days is None:
        raise RuntimeError(
            "Could not read data.leftDays from status response. "
            "The API response shape may have changed."
        )

    log(f"Current leftDays: {left_days}")
    log("GLaDOS checkin workflow completed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
