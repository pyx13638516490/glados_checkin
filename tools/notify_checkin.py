#!/usr/bin/env python3
"""本地签到结果通知：查一次积分，判断今天签没签到，推 Server酱。

配合 Windows 计划任务使用：
  18:10 打开 Chrome 签到页 -> ScriptCat 脚本自动点击 -> 120 秒后本脚本查结果并推送

为什么需要它：GitHub Actions 那条路已被 Cloudflare Turnstile 堵死，
    签到改由浏览器完成，但浏览器签完没人知道结果，所以补一个本地查询+推送。

凭证放哪（都不进仓库）：
  %USERPROFILE%\\.glados\\cookie.txt      完整 Cookie 一行（rocks 域）
  %USERPROFILE%\\.glados\\sendkey.txt     Server酱 SendKey（SCT... 或 sctp...）
也可以改用环境变量 GLADOS_COOKIE / SERVERCHAN_SENDKEY。

退出码：
  0 今天已签到（含刚签上的）
  1 今天还没签到
  2 cookie 失效或查询失败
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://glados.rocks"          # 浏览器登录的是 rocks 域
CONF_DIR = Path(os.path.expanduser("~")) / ".glados"
COOKIE_FILE = CONF_DIR / "cookie.txt"
SENDKEY_FILE = CONF_DIR / "sendkey.txt"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36 Edg/158.0.0.0"
)


def beijing_now():
    return datetime.now(timezone(timedelta(hours=8)))


def log(msg):
    print(f"[{beijing_now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_cookie():
    raw = (os.getenv("GLADOS_COOKIE") or "").strip()
    if not raw and COOKIE_FILE.exists():
        raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    # 支持两种写法：整行 "k=v; k=v" 或 四段空格分隔
    if "=" in raw:
        return raw
    parts = raw.split()
    if len(parts) == 4:
        return (
            f"gld:accts={parts[0]}; gld:accts.sig={parts[1]}; "
            f"gld:sess={parts[2]}; gld:sess.sig={parts[3]}"
        )
    if len(parts) == 2:
        return f"gld:sess={parts[0]}; gld:sess.sig={parts[1]}"
    return raw


def load_sendkey():
    key = (os.getenv("SERVERCHAN_SENDKEY") or "").strip()
    if not key and SENDKEY_FILE.exists():
        key = SENDKEY_FILE.read_text(encoding="utf-8").strip()
    return key or None


def query_points(cookie):
    r = requests.get(
        BASE + "/api/user/points",
        headers={"User-Agent": UA, "Accept": "application/json", "Cookie": cookie},
        timeout=25,
    )
    return r.status_code, r.json()


def push(sendkey, title, body):
    """Server酱 Turbo / Server酱³ 都兼容。"""
    if not sendkey:
        log("没有 SendKey，跳过推送")
        return False
    if sendkey.startswith("sctp"):
        m = re.match(r"sctp([0-9]+)t", sendkey)
        if not m:
            log("SendKey 格式不正确（sctp 前缀）")
            return False
        url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(url, json={"title": title, "desp": body}, timeout=15)
        data = r.json()
        if isinstance(data, dict) and data.get("code") == 0:
            log("Server酱 推送成功")
            return True
        log(f"Server酱 推送失败: {json.dumps(data, ensure_ascii=False)[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"Server酱 推送异常: {type(e).__name__}")
    return False


def main():
    cookie = load_cookie()
    if not cookie:
        log(f"缺少 cookie：请放到 {COOKIE_FILE}（整行 Cookie）")
        return 2

    sendkey = load_sendkey()

    try:
        status, data = query_points(cookie)
    except Exception as e:  # noqa: BLE001
        log(f"查询失败: {type(e).__name__}")
        push(sendkey, "GLaDOS 签到检查失败", f"网络异常：{type(e).__name__}")
        return 2

    if status != 200 or not isinstance(data, dict):
        log(f"HTTP {status}")
        push(sendkey, "GLaDOS 签到检查失败", f"HTTP {status}")
        return 2

    if data.get("code") != 0:
        msg = str(data.get("message") or data.get("msg") or "")
        log(f"认证失败: code={data.get('code')} message={msg}")
        push(
            sendkey,
            "GLaDOS cookie 已失效",
            f"接口返回：{html.escape(msg)}\n请重新登录 {BASE} 并更新 cookie.txt",
        )
        return 2

    points = str(data.get("points", "?")).split(".")[0]
    streak = data.get("streak")
    history = data.get("history") or []
    today = beijing_now().strftime("%Y-%m-%d")
    signed_today = bool(history) and str(history[0].get("detail", "")).startswith(today)
    last = history[0] if history else {}
    last_line = f"{last.get('detail', '?')}  {last.get('change', '?')} 分"

    if signed_today:
        log(f"今天已签到：{last_line}")
        push(
            sendkey,
            f"GLaDOS 签到成功 ✅ 当前 {points} 分",
            f"今日入账：{last.get('change', '?')} 分\n"
            f"当前积分：{points}\n"
            f"连续签到：{streak} 天\n"
            f"最近一笔：{last_line}",
        )
        return 0

    log(f"今天还没签到，最近一笔是 {last_line}")
    push(
        sendkey,
        "GLaDOS 今天还没签到 ⚠️",
        f"浏览器已经打开签到页，但没有查到今天的签到记录。\n"
        f"当前积分：{points}\n"
        f"最近一笔：{last_line}\n"
        f"请手动点一下 Checkin。",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
