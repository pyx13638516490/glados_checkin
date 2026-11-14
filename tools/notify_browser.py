#!/usr/bin/env python3
"""浏览器版签到结果通知：用 bsk 读 GLaDOS 签到页，不依赖静态 cookie。

和 notify_checkin.py 的区别：
  notify_checkin.py  用 %USERPROFILE%\\.glados\\cookie.txt 调 API，cookie 30 天会过期
  本脚本              用 bsk 打开页面读文字，cookie 由浏览器自己维护，永不失效

判断「今天签没签到」的方式：
  页面只给 POINTS / STREAK 两个数字，没有"今日已签"的字样，
  所以用 STREAK 和上一次运行时的记录比较：
      streak 变大  -> 今天签上了
      streak 没变  -> 今天还没签
  记录写在 %USERPROFILE%\\.glados\\state.json，配合每天跑一次的计划任务足够准确。

依赖：bsk daemon 在跑、Chrome 里装了 browser-skill 扩展并已登录 glados.rocks。

退出码：0 已签到 / 1 未签到 / 2 读取失败
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONF_DIR = Path(os.path.expanduser("~")) / ".glados"
STATE_FILE = CONF_DIR / "state.json"
SENDKEY_FILE = CONF_DIR / "sendkey.txt"

URL = "https://glados.rocks/console/checkin"
BROWSER_NAME = "chrome"

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
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


def ensure_daemon():
    """bsk daemon 不在就尝试拉起来（机器重启后它不会自启）。"""
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
        log(f"启动 daemon 失败: {type(e).__name__}")
        return False
    import time
    for _ in range(12):
        time.sleep(1)
        code, out = run_bsk(["status"], timeout=10)
        if code == 0 and "pid" in out:
            log("bsk daemon 已启动")
            return True
    return False


def pick_browser():
    code, out = run_bsk(["browsers", "--json"])
    if code != 0:
        raise RuntimeError(f"bsk browsers 失败: {out[:200]}")
    # 输出里第一段是 JSON 数组，后面可能跟日志
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        raise RuntimeError("无法解析 bsk browsers 输出")
    data = json.loads(m.group(0))
    for b in data:
        if b.get("browser_name") == BROWSER_NAME and not b.get("unresponsive"):
            return b["instance_id"]
    raise RuntimeError(f"没有可用的 {BROWSER_NAME} 实例")


def parse_numbers(text):
    """从 VOM 文本里抓 POINTS / STREAK。"""
    def grab(label):
        m = re.search(
            rf'"{label}"\s*\n\s*(?:StaticText\s+)?"?([\d.]+)"?',
            text,
        )
        return m.group(1) if m else None

    points = grab("POINTS")
    streak = grab("STREAK")
    days = grab("DAYS LEFT")

    # 退路：main 摘要行 "POINTS 329 STREAK 4 DAYS LEFT 477"
    if points is None:
        m = re.search(r"POINTS\s+([\d.]+)\s+STREAK\s+([\d.]+)", text)
        if m:
            points, streak = m.group(1), m.group(2)
    if days is None:
        m = re.search(r"DAYS LEFT\s+([\d.]+)", text)
        if m:
            days = m.group(1)

    def to_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return to_int(points), to_int(streak), to_int(days)


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
            log("SendKey 格式不正确")
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


def main():
    sendkey = load_sendkey()
    session = None
    try:
        if not ensure_daemon():
            raise RuntimeError("bsk daemon 无法启动")
        browser = pick_browser()
        code, out = run_bsk(["session", "start", "--browser", browser,
                             "--agent-name", "GLaDOS通知", "--json"])
        if code != 0:
            raise RuntimeError(f"启动会话失败: {out[:200]}")
        m = re.search(r"\{.*\}", out, re.S)
        session = json.loads(m.group(0))["session_id"]
        log(f"会话 {session} 已启动（{browser}）")

        run_bsk(["navigate", "--session", session, URL])
        # 等 ScriptCat 在这个标签页里执行完（点击 + Turnstile），再读结果
        import time
        time.sleep(18)

        code, text = run_bsk(["observe", "--session", session, "--max-tokens", "4000"])
        if code != 0 or "POINTS" not in text:
            raise RuntimeError(f"观察页面失败: {text[:200]}")

        points, streak, days = parse_numbers(text)
        log(f"页面读数: POINTS={points} STREAK={streak} DAYS={days}")
        if points is None or streak is None:
            raise RuntimeError("没能从页面解析出积分/连续天数")

        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        state = load_state()
        prev_streak = state.get("streak")
        prev_date = state.get("date")

        if prev_streak is None:
            signed = None
            note = "首次运行，暂无对比基准"
        elif prev_date == today:
            signed = True if state.get("signed") else None
            note = "今天已经检查过"
        else:
            signed = streak > prev_streak
            note = f"上次 {prev_date} 连续 {prev_streak} 天"

        delta = f"（较上次 {points - state['points']:+d} 分）" if state.get("points") is not None else ""

        if signed is True:
            title = f"GLaDOS 签到成功 ✅ 当前 {points} 分"
            body = (f"当前积分：{points}{delta}\n"
                    f"连续签到：{streak} 天\n"
                    f"剩余天数：{days} 天\n"
                    f"判断依据：{note}，连续天数已增加")
            rc = 0
        elif signed is False:
            title = "GLaDOS 今天还没签到 ⚠️"
            body = (f"当前积分：{points}{delta}\n"
                    f"连续签到：{streak} 天（没有增加）\n"
                    f"判断依据：{note}\n\n"
                    f"浏览器已打开签到页，但脚本猫可能没点成功。请手动点一下 Checkin。")
            rc = 1
        else:
            title = f"GLaDOS 签到状态已读取（{points} 分）"
            body = (f"当前积分：{points}{delta}\n"
                    f"连续签到：{streak} 天\n"
                    f"剩余天数：{days} 天\n{note}")
            rc = 1

        push(sendkey, title, body)
        save_state({"date": today, "points": points, "streak": streak,
                    "signed": bool(signed)})
        return rc

    except Exception as e:  # noqa: BLE001
        log(f"失败: {type(e).__name__}: {e}")
        push(sendkey, "GLaDOS 签到检查失败", f"浏览器读取失败：{type(e).__name__}: {e}")
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
