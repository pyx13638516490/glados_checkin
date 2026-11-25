#!/usr/bin/env python3
"""GLaDOS 全自动签到：默认走纯 HTTP API，只有真正需要时才动用浏览器。

2026-11-25 实测：服务端已不再对签到强制 Cloudflare Turnstile ——
`GET /api/user/checkin/challenge` 拿 nonce → `sha256(nonce + ".glados-observe-v1")`
→ `POST /api/user/checkin`，直接返回 code=0（已签到时 code=1 / repeated:true），
不会报 `code=5 / reason=turnstile`。

所以主路径回到纯 HTTP：不依赖 bsk daemon、不依赖 Chrome 在前台、
不会有 cooperate context 过期和 observation 文本截断 —— 这些正是 2026-11-24/25
在无人值守环境下连续失败两个晚上的原因。

降级到浏览器兜底的两种情况：
  1. API 返回 code=5 / reason=turnstile（Turnstile 又收紧了）
  2. API 返回 401/403（cookie.txt 过期，浏览器里通常还登录着）

浏览器兜底流程（2026-11 之前的主路径）：
  1. 确保 bsk daemon 在跑（机器重启后它不自启）
  2. 连 Chrome 开会话，打开 glados.rocks/console/checkin
  3. 等页面渲染，observe 找 Turnstile 复选框；有就点，等它变成 Success!
  4. observe 找纯 "Checkin" 按钮（不带 Got 字样）；有就点
  5. 再 observe 读结果：`Checkin! Got N Points` 表示签到成功

退出码：0 签到成功 / 1 今天已签到或未能确认 / 2 执行失败
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONF_DIR = Path(os.path.expanduser("~")) / ".glados"
STATE_FILE = CONF_DIR / "state.json"
SENDKEY_FILE = CONF_DIR / "sendkey.txt"
COOKIE_FILE = CONF_DIR / "cookie.txt"

API_BASES = ["https://glados.rocks", "https://glados.cloud"]
API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36 Edg/158.0.0.0"
)

URL = "https://glados.rocks/console/checkin"
# 优先 Chrome（脚本猫所在），Chrome 没连上时退回 Edge——本脚本自己做点击，
# 不依赖脚本猫，只要那个浏览器里登录着 glados.rocks 就行。
BROWSER_CANDIDATES = ["chrome", "edge"]

BSK_CANDIDATES = [
    os.path.expandvars(r"%USERPROFILE%\.local\bin\bsk.exe"),
    "bsk.exe",
    "bsk",
]


def bsk_path():
    for c in BSK_CANDIDATES:
        if os.path.sep in c or c.endswith(".exe"):
            if Path(c).exists():
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def log(msg):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def run_bsk(args, timeout=90):
    exe = bsk_path()
    if not exe:
        raise RuntimeError("找不到 bsk.exe")
    p = subprocess.run(
        [exe, *args],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ensure_daemon():
    code, out = run_bsk(["status"], timeout=15)
    if code == 0 and "pid" in out:
        return True
    log("bsk daemon 未运行，尝试启动")
    exe = bsk_path()
    if not exe:
        return False
    try:
        subprocess.Popen(
            [exe, "daemon", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:  # noqa: BLE001
        log(f"启动失败: {type(e).__name__}")
        return False
    for _ in range(12):
        time.sleep(1)
        code, out = run_bsk(["status"], timeout=10)
        if code == 0 and "pid" in out:
            log("bsk daemon 已启动")
            return True
    return False


def ensure_chrome():
    """浏览器兜底前确保有 Chrome 在跑。

    API 正常时根本走不到这里，所以日常运行不会启动任何浏览器。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=20, errors="replace",
        ).stdout
        if "chrome.exe" in out.lower():
            return True
    except Exception as e:  # noqa: BLE001
        log(f"检测 Chrome 失败: {type(e).__name__}")

    for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if not Path(exe).exists():
            continue
        log("启动 Chrome（浏览器兜底需要）")
        try:
            subprocess.Popen([exe, "--no-startup-window"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            log(f"启动 Chrome 失败: {type(e).__name__}")
            return False
        time.sleep(12)  # 等扩展连上 daemon
        return True
    log("找不到 chrome.exe")
    return False


def pick_browser():
    code, out = run_bsk(["browsers", "--json"])
    if code != 0:
        raise RuntimeError(f"bsk browsers 失败: {out[:200]}")
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        raise RuntimeError("无法解析 bsk browsers")
    data = json.loads(m.group(0))
    for name in BROWSER_CANDIDATES:
        for b in data:
            if b.get("browser_name") == name and not b.get("unresponsive"):
                return b["instance_id"], name
    raise RuntimeError("没有可用的 Chrome / Edge 实例")


def grab_number(text, label):
    m = re.search(rf'"{label}"\s*\n\s*(?:StaticText\s+)?"?([\d.]+)"?', text)
    if m:
        return m.group(1)
    m = re.search(rf"{label}\s+([\d.]+)", text)
    return m.group(1) if m else None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(state):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_sendkey():
    key = (os.getenv("SERVERCHAN_SENDKEY") or "").strip()
    if not key and SENDKEY_FILE.exists():
        key = SENDKEY_FILE.read_text(encoding="utf-8").strip()
    return key or None


def push(sendkey, title, body):
    if not sendkey:
        log("没有 SendKey，跳过推送")
        return False
    import requests

    if sendkey.startswith("sctp"):
        m = re.match(r"sctp([0-9]+)t", sendkey)
        if not m:
            return False
        url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(url, json={"title": title, "desp": body}, timeout=15)
        if isinstance(r.json(), dict) and r.json().get("code") == 0:
            log("Server酱 推送成功")
            return True
        log(f"推送失败: {r.text[:150]}")
    except Exception as e:  # noqa: BLE001
        log(f"推送异常: {type(e).__name__}")
    return False


def api_checkin(cookie):
    """纯 HTTP 签到。返回 dict，status 取值：

    ok        签到成功（本次入账 points 分）
    repeated  今天已经签过，服务器拒绝重复
    turnstile 被 Turnstile 拦住，需要浏览器兜底
    auth      cookie 失效（401/403），需要浏览器兜底
    error     网络/解析失败，可以再试浏览器
    """
    import hashlib

    import requests

    headers = {
        "User-Agent": API_USER_AGENT,
        "Cookie": cookie,
        "Content-Type": "application/json",
    }
    for base in API_BASES:
        h = dict(headers, Origin=base, Referer=f"{base}/console/checkin")
        payload = None
        for attempt in range(2):
            try:
                ch = requests.get(
                    f"{base}/api/user/checkin/challenge", headers=h, timeout=20
                )
                if ch.status_code in (401, 403):
                    log(f"{base} challenge 返回 HTTP {ch.status_code}（cookie 失效）")
                    return {"status": "auth"}
                payload = ch.json()
                break
            except Exception as e:  # noqa: BLE001
                log(f"{base} challenge 异常({attempt + 1}/2): {type(e).__name__}")
                time.sleep(3)
        if payload is None:
            continue
        if payload.get("code") != 0 or not payload.get("nonce"):
            log(f"{base} challenge 内容异常: {str(payload)[:120]}")
            continue

        nonce = payload["nonce"]
        proof = hashlib.sha256((nonce + ".glados-observe-v1").encode()).hexdigest()
        try:
            r = requests.post(
                f"{base}/api/user/checkin",
                headers=h,
                json={"token": cookie, "nonce": nonce, "proof": proof},
                timeout=25,
            )
        except Exception as e:  # noqa: BLE001
            log(f"{base} 签到请求异常: {type(e).__name__}")
            continue
        if r.status_code in (401, 403):
            log(f"{base} 签到返回 HTTP {r.status_code}（cookie 失效）")
            return {"status": "auth"}
        try:
            data = r.json()
        except ValueError:
            log(f"{base} 签到响应不是 JSON: {r.text[:120]}")
            continue

        log(f"{base} 签到响应: {json.dumps(data, ensure_ascii=False)[:220]}")
        if data.get("code") == 5 or data.get("reason") == "turnstile":
            return {"status": "turnstile"}

        balance = None
        for row in data.get("list") or []:
            if row.get("business") == "system:checkin" and row.get("balance"):
                balance = row["balance"]
                break
        result = {
            "status": "repeated" if data.get("repeated") else "ok",
            "points": data.get("points"),
            "streak": data.get("streak"),
            "balance": balance,
            "message": data.get("message"),
        }
        # 剩余天数只在 status 接口里有；失败不影响主流程
        try:
            st = requests.get(f"{base}/api/user/status", headers=h, timeout=15).json()
            left = (st.get("data") or {}).get("leftDays")
            if left is not None:
                result["days"] = int(float(left))
        except Exception:  # noqa: BLE001
            pass
        return result
    return {"status": "error"}


def report_api(sendkey, state, res):
    """把 API 结果整理成推送并发出去，返回退出码。"""
    balance = res.get("balance")
    points = "-"
    try:
        if balance is not None:
            points = int(float(balance))
    except (TypeError, ValueError):
        pass
    streak = res.get("streak")
    if streak is None:
        streak = state.get("streak", "-")
    days = res.get("days")
    days_line = f"剩余天数：{days} 天\n" if days is not None else ""

    prev_points = state.get("points")
    delta = ""
    try:
        if prev_points is not None and isinstance(points, int):
            delta = f"（较上次 {points - int(prev_points):+d} 分）"
    except (TypeError, ValueError):
        pass

    if res.get("status") == "ok":
        got = res.get("points")
        title = f"GLaDOS 签到成功 ✅ +{got} 分（共 {points}）"
        body = (f"本次入账：+{got} 分\n"
                f"当前积分：{points}{delta}\n"
                f"连续签到：{streak} 天\n{days_line}"
                f"方式：API")
        rc = 0
    else:
        title = f"GLaDOS 今天已签到（{points} 分）"
        body = (f"当前积分：{points}{delta}\n"
                f"连续签到：{streak} 天\n{days_line}"
                f"今天之前已经签过，未重复提交。\n方式：API")
        rc = 1

    push(sendkey, title, body)
    save_state({
        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "points": points if isinstance(points, int) else prev_points,
        "streak": streak,
    })
    return rc


def solve_turnstile(session, coop, click):
    """如果页面出现 Turnstile 复选框就点掉，返回 (文本, 是否处理过)。

    注意：必须用 cooperate sync 返回的 context 和「同一次 sync 里的」ref，
    旧的 observe ref 会失效（实测 ref 编号每次 sync 都会变，用旧 ref 会报
    "shared page control or observation changed"）。
    """
    ctx, text = coop()
    if "Verify you are human" not in text:
        log("没有 Turnstile 复选框（可能已通过或未出现）")
        return text, False

    m = re.search(r'(@e\d+) checkbox "Verify you are human"', text)
    if not m:
        log("看到验证字样但没定位到复选框")
        return text, False

    log(f"发现 Turnstile 复选框 {m.group(1)}，点击")
    click(m.group(1), ctx)
    time.sleep(8)

    ctx, text = coop()
    if "Success!" in text:
        log("Turnstile 验证通过（Success!）")
        return text, True
    if "Verify you are human" not in text:
        log("Turnstile 已消失，按通过处理")
        return text, True
    log("Turnstile 仍未通过")
    return text, False


def try_refresh_cookie():
    """浏览器兜底之后，把浏览器里那份 cookie 读回 cookie.txt，恢复 API 路径。

    读 cookie 要求 Chrome 独占 user-data-dir，所以这里会：
      1. 记下 Chrome 原本是否开着
      2. 开着就 taskkill 掉（未保存的页面会丢——这是你选的激进模式）
      3. 用 CDP 把登录态读回来
      4. 不管成功失败，都把 Chrome 重新打开

    18:10 浏览器兜底时 Chrome 必然是开着的，所以这条路径就是那时候的主力。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import refresh_cookie as rc
    except Exception as e:  # noqa: BLE001
        log(f"加载 refresh_cookie 失败: {type(e).__name__}")
        return ""

    chrome = rc.find_chrome()
    if not chrome:
        log("找不到 chrome.exe，跳过自动恢复")
        return ""

    was_running = rc.chrome_running()
    if was_running:
        log("关闭 Chrome 以读取登录态（读完后会自动重新打开）")
        try:
            subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                           capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            log(f"关闭 Chrome 失败: {type(e).__name__}")
        time.sleep(3)

    code = 2
    try:
        log("从浏览器读取 cookie")
        code = rc.read_from_browser(
            chrome, 9222,
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
            "Default", False,
        )
    except Exception as e:  # noqa: BLE001
        log(f"读取 cookie 异常: {type(e).__name__}")
    finally:
        if was_running:
            log("重新打开 Chrome")
            try:
                subprocess.Popen([chrome], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:  # noqa: BLE001
                log(f"重新打开 Chrome 失败: {type(e).__name__}")

    if code == 0:
        return "✓ 已从浏览器把登录态读回 cookie.txt，下次会走 2 秒的 API 路径。"
    return ("从浏览器读 cookie 没成功。手动跑一次：\n"
            "  python tools/refresh_cookie.py --read")


def main():
    sendkey = load_sendkey()
    state = load_state()

    # ---------- 第一阶段：纯 HTTP API（默认路径） ----------
    # 不碰浏览器：没有 daemon、没有 Chrome 前台、没有 cooperate context。
    cookie = ""
    api_note = ""
    if COOKIE_FILE.exists():
        cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not cookie:
        log("没有 cookie.txt，跳过 API，直接用浏览器")
        api_note = "missing"
    else:
        res = api_checkin(cookie)
        status = res.get("status")
        if status in ("ok", "repeated"):
            return report_api(sendkey, state, res)
        if status == "turnstile":
            log("API 被 Turnstile 拦住 → 降级到浏览器")
            api_note = "turnstile"
        elif status == "auth":
            log("Cookie 已失效 → 降级到浏览器")
            log("提示：浏览器签完后运行 python tools/save_cookie.py 更新 cookie.txt")
            api_note = "cookie"
        else:
            log("API 请求失败 → 降级到浏览器")
            api_note = "error"

    # ---------- 第二阶段：浏览器兜底 ----------
    session = None
    try:
        if not ensure_daemon():
            raise RuntimeError("bsk daemon 无法启动")
        ensure_chrome()
        browser, browser_name = pick_browser()
        code, out = run_bsk(["session", "start", "--browser", browser,
                             "--agent-name", "GLaDOS自动签到", "--json"])
        if code != 0:
            raise RuntimeError(f"启动会话失败: {out[:200]}")
        m = re.search(r"\{.*\}", out, re.S)
        session = json.loads(m.group(0))["session_id"]
        log(f"会话 {session} 已启动（{browser_name} {browser}）")

        def coop():
            """return (context_id, observation_text) —— ref 必须来自同一次 sync。"""
            code, out = run_bsk(["cooperate", "--session", session, "sync", "--json"])
            m = re.search(r"\{.*\}", out, re.S)
            if not m:
                raise RuntimeError(f"cooperate sync 失败: {out[:200]}")
            d = json.loads(m.group(0))
            text = (d.get("observation") or {}).get("text") or ""
            return d.get("context_id"), text

        def click(ref, ctx):
            code, out = run_bsk(
                ["click", "--session", session, "--context", ctx, "--ref", ref], timeout=45
            )
            if code != 0:
                log(f"点击 {ref} 失败: {out[:160]}")
            return code == 0

        def observe():
            """只读全量观察（cooperate 返回的 text 会被截断，读数据不够用）。"""
            code, out = run_bsk(["observe", "--session", session, "--max-tokens", "4000"])
            return out

        run_bsk(["navigate", "--session", session, URL])
        # 等页面真正渲染出来（Chrome 刚被拉起时，navigate 返回 load 不代表
        # Vue 已经渲染完，过早 observe 会拿到空页面，POINTS 解析成 None）
        loaded = False
        for _ in range(12):
            time.sleep(2)
            t = observe()
            if "POINTS" in t and "Checkin" in t:
                loaded = True
                break
        if not loaded:
            log("页面 24 秒内仍未渲染出 POINTS，继续尝试")
        else:
            log("页面已渲染")

        text, ok = solve_turnstile(session, coop, click)
        if not ok and "Verify you are human" in text:
            raise RuntimeError("Turnstile 未能通过")

        # 点 Checkin：只匹配纯 "Checkin"，不碰 "Checkin! Got N Points"
        # cooperate 的 context 会过期（报 control context no longer belongs），
        # 点击失败必须重新 sync —— 每次 sync 的 ref 都不一样，不能复用。
        clicked = False
        for attempt in range(4):
            ctx, text = coop()
            btn = re.search(r'(@e\d+) button "Checkin"', text)
            if btn:
                log(f"点击 Checkin 按钮 {btn.group(1)}（第 {attempt + 1} 次尝试）")
                if click(btn.group(1), ctx):
                    clicked = True
                    break
                log("点击失败，重新同步后重试")
                time.sleep(2)
                continue
            if "Checkin! Got" in text:
                break
            log(f"还没看到 Checkin 按钮（第 {attempt + 1} 次），2 秒后重看")
            time.sleep(2)

            # 点完之后 Turnstile 可能是异步加载的（实测 2026-11-16：
            # 页面加载 15 秒都没有验证框，点 Checkin 后才出现）。
            # 轮询观察，最多 30 秒，边等边处理。
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(3)
                text = observe()
                if "Checkin! Got" in text:
                    log("已拿到签到结果")
                    break
                ctx2, ctext = coop()
                box = re.search(r'(@e\d+) checkbox "Verify you are human"', ctext)
                if box:
                    log(f"检测到验证框 {box.group(1)}，点击")
                    click(box.group(1), ctx2)
                    time.sleep(8)
                    ctx2, ctext = coop()
                    if "Success!" in ctext or "Verify you are human" not in ctext:
                        log("验证通过，重新点 Checkin")
                        again = re.search(r'(@e\d+) button "Checkin"', ctext)
                        if again:
                            click(again.group(1), ctx2)
                    continue

        # 统一用全量 observe 读结果（cooperate 的 text 会被截断）
        text = observe()
        got = re.search(r'Checkin! Got ([\d.]+) Points', text)
        points = grab_number(text, "POINTS")
        streak = grab_number(text, "STREAK")
        days = grab_number(text, "DAYS LEFT")
        log(f"结果: got={got.group(1) if got else '-'} POINTS={points} STREAK={streak} DAYS={days}")

        state = load_state()
        prev_points = state.get("points")
        delta = ""
        try:
            if prev_points is not None and points is not None:
                delta = f"（较上次 {int(points) - int(prev_points):+d} 分）"
        except (TypeError, ValueError):
            pass

        # 只看积分有没有涨来兜底判断：页面上的 "Checkin! Got N Points" 可能是
        # 昨天留下的状态（2026-11-24 就是因为 "[disabled]" 被当成已签到而误报成功）。
        gained = None
        try:
            if prev_points is not None and points is not None:
                gained = int(float(points)) - int(prev_points)
        except (TypeError, ValueError):
            pass

        if got:
            title = f"GLaDOS 签到成功 ✅ +{got.group(1)} 分（共 {points}）"
            body = (f"本次入账：+{got.group(1)} 分\n"
                    f"当前积分：{points}{delta}\n"
                    f"连续签到：{streak} 天\n剩余天数：{days} 天")
            rc = 0
        elif gained is not None and gained > 0:
            title = f"GLaDOS 今天已签到（{points} 分）"
            body = (f"当前积分：{points}{delta}\n连续签到：{streak} 天\n剩余天数：{days} 天\n"
                    f"积分比上次多了 {gained}，确认签到已生效。")
            rc = 1
        else:
            title = "GLaDOS 签到未确认 ⚠️"
            body = (f"没有读到 \"Checkin! Got N Points\"，积分也没有变化。\n"
                    f"当前积分：{points}\n连续签到：{streak} 天\n"
                    f"请打开页面手动确认一下。")
            rc = 1

        if api_note in ("cookie", "missing"):
            note = try_refresh_cookie()
            if note:
                body += "\n\n" + note

        push(sendkey, title, body)
        save_state({
            "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
            "points": int(float(points)) if points else prev_points,
            "streak": int(float(streak)) if streak else state.get("streak"),
        })
        return rc

    except Exception as e:  # noqa: BLE001
        log(f"失败: {type(e).__name__}: {e}")
        push(sendkey, "GLaDOS 自动签到失败", f"{type(e).__name__}: {e}")
        return 2
    finally:
        if session:
            try:
                run_bsk(["session", "stop", session], timeout=30)
                log("会话已关闭")
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
