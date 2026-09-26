import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


# --- 2026 GLaDOS API 变更 -------------------------------------------------
# GLaDOS 在 2026 年初更新了签到接口：token 从 "glados.one" 改为 "glados.cloud"。
# 用旧 token 时接口依然返回 HTTP 200，但 message 是
# "please checkin via https://glados.cloud"。
# 该响应很容易被误判为"网站增加了反脚本检测"，实际只是 token 过期。
# 已验证无效的"绕过"方向：补 sec-ch-ua / sec-fetch-* 请求头、
# curl_cffi 伪造 TLS 指纹、挂代理 —— 对这个问题全部无效。
DEFAULT_CHECKIN_TOKEN = "glados.cloud"

DEFAULT_BASE_URL = "https://glados.cloud"
FALLBACK_BASE_URLS = ("https://glados.rocks", "https://glados.network")

TIMEOUT = (10, 30)
MAX_ATTEMPTS = 3

# 退出码约定，供 workflow 判断是否值得重试
EXIT_OK = 0
EXIT_FATAL = 1        # 重试也不会成功（token / cookie / 响应结构变更）
EXIT_RETRYABLE = 2    # 网络或服务端临时故障，值得重试

SUCCESS_MARKERS = (
    "checkin!",
    "checkin repeats",
    "observation logged",
)
TOKEN_ERROR_MARKERS = (
    "please checkin via",
    "checkin via http",
)
AUTH_ERROR_MARKERS = (
    "not login",
    "not logged",
    "please login",
    "unauthorized",
    "invalid cookie",
    "cookie expired",
    "session expired",
    # GLaDOS 的错误消息是中文的：实测 /api/user/status 在 cookie 无效时返回
    # {"code":-2,"message":"没有权限"}。不匹配中文就会被误判成 unknown。
    "没有权限",
    "未登录",
    "请先登录",
    "登录已过期",
)

# 响应里这些字段会被写进 CI 日志。其中 code / domain / port 组合起来足以
# 还原订阅信息，email / password / hashed 属于账号信息，因此统一脱敏。
SENSITIVE_KEYS = frozenset(
    {
        "email",
        "password",
        "hashed",
        "code",
        "domain",
        "port",
        "phone",
        "usdt_address",
        "telegram_id",
        "configureid",
        "configure_id",
        "user_id",
        "userid",
    }
)


class FatalError(RuntimeError):
    """重试也不会成功的错误。"""


class RetryableError(RuntimeError):
    """网络或服务端临时故障。"""


def now_text():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")


def log(message):
    print(f"[{now_text()}] {message}", flush=True)


def get_env(name, default=None):
    """读取环境变量。空字符串按"未设置"处理，否则 GitHub 上定义了但留空的
    变量会覆盖掉脚本里的默认值。"""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def require_cookie():
    cookie = get_env("GLADOS_COOKIE")
    if not cookie:
        raise FatalError(
            "GLADOS_COOKIE is empty. Update the GitHub Actions repository secret."
        )
    if "koa:sess" not in cookie or "koa:sess.sig" not in cookie:
        log("WARNING: GLADOS_COOKIE 里没有同时出现 koa:sess 和 koa:sess.sig。")
    # 只输出长度和指纹，便于排查 cookie 是否为空/被截断，不泄露 cookie 本身
    log(
        f"Cookie 长度 {len(cookie)}，"
        f"sha256 指纹 {hashlib.sha256(cookie.encode()).hexdigest()[:12]}"
    )
    return cookie


def resolve_token():
    """读取签到 token，并拦截已确认失效的旧值。"""
    token = get_env("GLADOS_CHECKIN_TOKEN", DEFAULT_CHECKIN_TOKEN)
    if token == "glados.one":
        log(
            "WARNING: GLADOS_CHECKIN_TOKEN 仍是已失效的旧值 'glados.one'，"
            f"已自动改用 '{DEFAULT_CHECKIN_TOKEN}'。"
            "请把仓库变量 GLADOS_CHECKIN_TOKEN 更新或删除。"
        )
        return DEFAULT_CHECKIN_TOKEN
    return token


def candidate_base_urls():
    primary = get_env("GLADOS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    urls = [primary]
    if get_env("GLADOS_DOMAIN_FALLBACK", "1") != "0":
        for url in FALLBACK_BASE_URLS:
            url = url.rstrip("/")
            if url not in urls:
                urls.append(url)
    return urls


def build_session(cookie):
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
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
            "text": response.text[:500],
        }


def request_json(session, method, url, origin, payload=None):
    """请求接口，区分致命错误与可重试错误。

    签到是 POST，盲目重复提交并不合适，所以这里只在确实属于
    临时故障（429 / 5xx / 连接异常）时才重试。
    """
    headers = {
        "Origin": origin,
        "Referer": f"{origin}/console/checkin",
    }
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"{method.upper()} {url} (attempt {attempt}/{MAX_ATTEMPTS})")
            if payload is None:
                response = session.request(method, url, timeout=TIMEOUT, headers=headers)
            else:
                response = session.request(
                    method,
                    url,
                    timeout=TIMEOUT,
                    headers=headers,
                    data=json.dumps(payload),
                )
            body = parse_json_response(response)
            log(
                f"HTTP {response.status_code}: "
                f"{json.dumps(redact(body), ensure_ascii=False)}"
            )

            if response.status_code in (401, 403):
                raise FatalError(
                    f"认证失败 (HTTP {response.status_code})，"
                    "请更新 GitHub Secret GLADOS_COOKIE。"
                )

            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableError(f"可重试的 HTTP 状态: {response.status_code}")

            response.raise_for_status()
            return body

        except FatalError:
            raise
        except RetryableError as exc:
            last_error = exc
            log(str(exc))
        except requests.RequestException as exc:
            last_error = exc
            log(f"请求失败: {exc}")

        if attempt < MAX_ATTEMPTS:
            sleep_seconds = min(60, 2 ** attempt * 5)
            log(f"{sleep_seconds}s 后重试。")
            time.sleep(sleep_seconds)

    raise RetryableError(f"{MAX_ATTEMPTS} 次尝试后仍失败: {last_error}")


def redact(payload):
    """隐藏响应中的账号与订阅敏感字段，避免泄露到 CI 日志。

    这些日志可能被仓库协作者看到，排查时也常被整段贴到别处，
    所以默认就不打印敏感字段。
    """
    if isinstance(payload, dict):
        return {
            key: ("<redacted>" if key.lower() in SENSITIVE_KEYS else redact(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def response_text(payload):
    if isinstance(payload, dict):
        parts = [str(payload.get(key, "")) for key in ("message", "msg", "error")]
        parts.append(json.dumps(payload, ensure_ascii=False))
        return " ".join(parts).lower()
    return str(payload).lower()


def response_message(payload):
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("msg") or "")
    return str(payload)


def classify_checkin(payload):
    """把签到响应归类。

    原脚本只看认证错误，从不判断签到本身是否成功，所以 token 失效时
    它照样打印 leftDays 并以退出码 0 结束 —— 表面成功、实际没签到，
    这正是"看起来被拦截了"的观感来源。这里显式分类，
    宁可报 unknown 也不谎报成功。
    """
    text = response_text(payload)
    code = payload.get("code") if isinstance(payload, dict) else None

    if any(marker in text for marker in TOKEN_ERROR_MARKERS):
        return "token_error"
    if any(marker in text for marker in AUTH_ERROR_MARKERS):
        return "auth_error"
    if any(marker in text for marker in SUCCESS_MARKERS):
        return "success"
    if code == 0:
        return "success"
    return "unknown"


def explain_failure(status, payload, token, base_url):
    message = response_message(payload)

    if status == "token_error":
        return (
            "签到 token 已失效，本次签到未生效。\n"
            f"  当前 GLADOS_CHECKIN_TOKEN = {token!r}\n"
            f"  {base_url}/api/user/checkin 返回: {message!r}\n"
            "  GLaDOS 2026 年初把 token 从 'glados.one' 改成了 'glados.cloud'。\n"
            "  修复: 删除仓库变量 GLADOS_CHECKIN_TOKEN，"
            "或把它设为 'glados.cloud'。\n"
            "  注意: 这不是反脚本拦截 —— 补请求头、换 UA、伪造 TLS 指纹、"
            "挂代理对这个响应都不会生效。"
        )
    if status == "auth_error":
        return (
            "Cookie 已失效。请重新登录 glados.cloud，复制 koa:sess 与 "
            "koa:sess.sig，更新 GitHub Secret GLADOS_COOKIE。\n"
            f"  接口返回: {message!r}"
        )
    return (
        "签到响应无法识别，不能确认签到是否生效。\n"
        f"  {base_url}/api/user/checkin 返回: {message!r}\n"
        "  请对照 glados.cloud 控制台的积分变化确认。"
    )


def left_days_from_status(payload):
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("leftDays")
    return None


def do_checkin(session, base_urls, token):
    """在候选域名上执行签到，返回 (base_url, payload)。"""
    last_error = None

    for base_url in base_urls:
        try:
            payload = request_json(
                session,
                "post",
                f"{base_url}/api/user/checkin",
                base_url,
                payload={"token": token},
            )
        except RetryableError as exc:
            last_error = exc
            log(f"{base_url} 不可用: {exc}")
            continue

        status = classify_checkin(payload)
        if status in ("token_error", "auth_error"):
            # token / cookie 问题和域名无关，换域名没有意义
            raise FatalError(explain_failure(status, payload, token, base_url))
        return base_url, payload

    raise RetryableError(f"所有候选域名均失败: {last_error}")


def report_account(session, base_url):
    """读取剩余天数与积分，仅作展示；失败不影响签到结论。

    注意 leftDays 是会员剩余天数，和签到是否生效无关（签到发的是积分），
    所以这里额外读一次 points 作为对照。
    """
    try:
        status_payload = request_json(
            session, "get", f"{base_url}/api/user/status", base_url
        )
    except (FatalError, RetryableError) as exc:
        log(f"WARNING: 读取 status 失败: {exc}")
    else:
        left_days = left_days_from_status(status_payload)
        if left_days is None:
            log("WARNING: 无法从 status 响应读取 data.leftDays，接口结构可能又变了。")
        else:
            log(f"Current leftDays: {left_days}")

    try:
        points_payload = request_json(
            session, "get", f"{base_url}/api/user/points", base_url
        )
    except (FatalError, RetryableError) as exc:
        log(f"WARNING: 读取 points 失败: {exc}")
        return

    if not isinstance(points_payload, dict) or points_payload.get("points") is None:
        log("WARNING: 无法从 points 响应读取 points 字段。")
        return

    change = ""
    history = points_payload.get("history")
    if isinstance(history, list) and history and isinstance(history[0], dict):
        delta = history[0].get("change")
        if delta is not None:
            change = f"（最近一次变化 {delta}）"
    log(f"Current points: {points_payload.get('points')}{change}")


def main():
    cookie = require_cookie()
    token = resolve_token()
    base_urls = candidate_base_urls()
    session = build_session(cookie)

    log(f"签到域名候选: {', '.join(base_urls)}")

    base_url, checkin_payload = do_checkin(session, base_urls, token)
    status = classify_checkin(checkin_payload)
    message = response_message(checkin_payload)

    log(f"签到结果: {message or status}")
    report_account(session, base_url)

    if status != "success":
        log(f"ERROR: {explain_failure(status, checkin_payload, token, base_url)}")
        return EXIT_FATAL

    log("GLaDOS checkin workflow completed.")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FatalError as exc:
        log(f"ERROR: {exc}")
        sys.exit(EXIT_FATAL)
    except RetryableError as exc:
        log(f"ERROR: {exc}")
        sys.exit(EXIT_RETRYABLE)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证退出码语义明确
        log(f"ERROR: {exc}")
        sys.exit(EXIT_FATAL)
