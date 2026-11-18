#!/usr/bin/env python3
"""GLaDOS 全自动签到（浏览器版）：勾 Turnstile → 点 Checkin → 推送结果。

2026-11-14 起签到要求 Cloudflare Turnstile。实测（2026-11-15）：bsk 能看到
页面上的复选框 `checkbox "Verify you are human"`，点一下就变成 `Success!`，
之后再点 Checkin 就能正常拿到积分。所以纯 HTTP 做不到的事，用真浏览器反而能做。

流程：
  1. 确保 bsk daemon 在跑（机器重启后它不自启）
  2. 连 Chrome 开会话，打开 glados.rocks/console/checkin
  3. 等页面渲染，observe 找 Turnstile 复选框；有就点，等它变成 Success!
  4. observe 找纯 "Checkin" 按钮（不带 Got 字样）；有就点
  5. 再 observe 读结果：`Checkin! Got N Points` 表示签到成功
  6. Server酱 推送

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


def main():
    sendkey = load_sendkey()
    session = None
    try:
        if not ensure_daemon():
            raise RuntimeError("bsk daemon 无法启动")
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
        time.sleep(5)

        text, ok = solve_turnstile(session, coop, click)
        if not ok and "Verify you are human" in text:
            raise RuntimeError("Turnstile 未能通过")

        # 点 Checkin：只匹配纯 "Checkin"，不碰 "Checkin! Got N Points"
        # cooperate 返回的文本是截断的，按钮不一定第一次就在里面，
        # 所以多 sync 几次（每次都会重新编号 ref，必须用当次的结果）。
        btn = None
        for _ in range(5):
            ctx, text = coop()
            btn = re.search(r'(@e\d+) button "Checkin"', text)
            if btn or "Checkin! Got" in text:
                break
            time.sleep(1.5)

        if not btn:
            log("没有可点的 Checkin 按钮（今天可能已签到）")
        else:
            log(f"点击 Checkin 按钮 {btn.group(1)}")
            click(btn.group(1), ctx)

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

        if got:
            title = f"GLaDOS 签到成功 ✅ +{got.group(1)} 分（共 {points}）"
            body = (f"本次入账：+{got.group(1)} 分\n"
                    f"当前积分：{points}{delta}\n"
                    f"连续签到：{streak} 天\n剩余天数：{days} 天")
            rc = 0
        elif "Checkin! Got" in text or "[disabled]" in text:
            title = f"GLaDOS 今天已签到（{points} 分）"
            body = (f"当前积分：{points}{delta}\n连续签到：{streak} 天\n剩余天数：{days} 天\n"
                    f"页面上已经是签到后的状态，未重复提交。")
            rc = 1
        else:
            title = "GLaDOS 签到未确认 ⚠️"
            body = (f"没有读到 \"Checkin! Got N Points\"。\n"
                    f"当前积分：{points}\n连续签到：{streak} 天\n"
                    f"请打开页面手动确认一下。")
            rc = 1

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
