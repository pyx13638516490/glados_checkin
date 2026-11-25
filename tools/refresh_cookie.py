#!/usr/bin/env python3
"""用 Chrome DevTools Protocol 直接从 Chrome 里读 GLaDOS cookie，写回 cookie.txt。

和 selenium 版的区别：不装 selenium、不装 chromedriver。CDP 是 Chrome 自带的
调试接口，用 --remote-debugging-port 打开，`Network.getAllCookies` 会返回浏览器
内存里的 cookie —— 包括 HttpOnly 的 gld:sess，也绕开了磁盘上的 App-Bound 加密。

前提：Chrome 必须完全退出。--user-data-dir 是独占的，Chrome 开着就起不来第二个
实例（会报 user data directory is already in use）。所以：
  * 不加参数时，如果 Chrome 在跑，脚本会提示你先关掉，然后退出；
  * 加 --kill-chrome 才会主动结束 chrome.exe（未保存的页面会丢，慎用）。

用法：
    python tools/refresh_cookie.py                 # 关掉 Chrome 后跑
    python tools/refresh_cookie.py --kill-chrome   # 让我帮你关
    python tools/refresh_cookie.py --dry-run       # 只读不写，看看能拿到什么
    python tools/refresh_cookie.py --self-test     # 用临时 profile 验证 CDP 链路
    python tools/refresh_cookie.py --windowed      # 不用无头模式（看得到窗口）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websockets

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from checkin import get_cookie_names, get_session_cookie_kind  # noqa: E402
from save_cookie import verify, write_cookie  # noqa: E402

WANTED = [
    "gld:accts", "gld:accts.sig", "gld:sess", "gld:sess.sig",
    "koa:sess", "koa:sess.sig",
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome():
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def chrome_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=20, errors="replace",
        ).stdout
        return "chrome.exe" in out.lower()
    except Exception:  # noqa: BLE001
        return False


def launch(chrome, port, user_data_dir, profile_name, headless=True):
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--remote-allow-origins=*",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def page_ws(port, timeout=40):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            lst = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
            for t in lst:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        time.sleep(0.5)
    raise RuntimeError(f"40 秒内拿不到调试目标（最后错误: {last}）")


async def cdp_seq(ws_url, commands, timeout=25):
    """在一条连接里按顺序发命令，返回每个命令的 result。"""
    results = []
    async with websockets.connect(
        ws_url, max_size=32 * 1024 * 1024, open_timeout=timeout
    ) as ws:
        for i, (method, params) in enumerate(commands, 1):
            await ws.send(json.dumps({"id": i, "method": method,
                                      "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if msg.get("id") == i:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    results.append(msg.get("result", {}))
                    break
    return results


def pick_glados_cookies(cookies):
    """从 CDP 返回的 cookie 里挑出 GLaDOS 的会话字段。"""
    picked = {}
    for c in cookies:
        name = c.get("name")
        if name in WANTED and "glados" in (c.get("domain") or ""):
            picked[name] = c.get("value")
    parts = [f"{n}={picked[n]}" for n in WANTED if n in picked]
    return "; ".join(parts), picked


def self_test(chrome, port):
    """用临时 profile 验证 CDP 能写能读 HttpOnly cookie，完全不碰你的登录态。"""
    port += 1
    tmp = tempfile.mkdtemp(prefix="ck-selftest-")
    print(f"自测：临时 profile {tmp}")
    proc = launch(chrome, port, tmp, "Default", headless=True)
    try:
        ws = page_ws(port, timeout=30)
        print("    调试目标已就绪")
        set_res, get_res = asyncio.run(cdp_seq(ws, [
            ("Network.setCookie", {
                "name": "gld:sess", "value": "TEST-VALUE-123",
                "domain": ".glados.rocks", "path": "/",
                "httpOnly": True, "secure": True,
            }),
            ("Network.getAllCookies", {}),
        ]))
        got = [c for c in get_res.get("cookies", [])
               if c.get("name") == "gld:sess" and "glados" in (c.get("domain") or "")]
        ok = bool(got) and got[0].get("value") == "TEST-VALUE-123"
        print(f"    setCookie 返回: {set_res}")
        if ok:
            print(f"✓ 自测通过：读到 HttpOnly cookie（httpOnly={got[0].get('httpOnly')}）")
            return 0
        print(f"✗ 自测失败，读回的是: {got}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"✗ 自测异常: {type(e).__name__}: {e}")
        return 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="用 CDP 从 Chrome 读 GLaDOS cookie")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--profile", default=os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data"))
    ap.add_argument("--profile-name", default="Default")
    ap.add_argument("--kill-chrome", action="store_true",
                    help="主动结束 chrome.exe（会丢失未保存页面）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--windowed", action="store_true", help="不用无头模式")
    ap.add_argument("--self-test", action="store_true", help="仅验证 CDP 链路")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("✗ 找不到 chrome.exe（试过的路径：\n  "
              + "\n  ".join(CHROME_CANDIDATES) + "）")
        return 2
    print(f"Chrome: {chrome}")

    if args.self_test:
        return self_test(chrome, args.port)

    if not Path(args.profile).exists():
        print(f"✗ profile 目录不存在: {args.profile}")
        return 2

    if chrome_running():
        if not args.kill_chrome:
            print("✗ Chrome 正在运行，它的 user-data-dir 是独占的，第二个实例起不来。\n"
                  "  请先完全退出 Chrome（右上角关闭 + 任务管理器里确认没有 chrome.exe），\n"
                  "  然后重跑；或者加 --kill-chrome 让我关掉它（未保存的页面会丢）。")
            return 3
        print("关闭 Chrome ...")
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                       capture_output=True, text=True, timeout=60)
        time.sleep(3)
        if chrome_running():
            print("✗ 还有 chrome.exe 没退干净")
            return 2

    proc = launch(chrome, args.port, args.profile, args.profile_name,
                  headless=not args.windowed)
    try:
        print("等待调试端口 ...")
        ws = page_ws(args.port)
        res = asyncio.run(cdp_seq(ws, [("Network.getAllCookies", {})]))[0]
        cookies = res.get("cookies", [])
        print(f"读到 {len(cookies)} 个 cookie（全部域名）")

        cookie, picked = pick_glados_cookies(cookies)
        if not picked:
            print("✗ 没找到 glados 的 cookie。这个 profile 里没有登录态，"
                  "确认 --profile-name 是不是你用 Chrome 登录的那个。")
            return 2
        print(f"GLaDOS 字段: {', '.join(picked)}")

        kind = get_session_cookie_kind(cookie)
        if not kind:
            print("✗ 会话字段不全（缺 sig），不做任何写入")
            return 2
        print(f"会话类型: {kind}")

        if args.dry_run:
            print(f"[dry-run] 不写文件。cookie 长度 {len(cookie)}，字段 "
                  f"{sorted(get_cookie_names(cookie))}")
            return 0

        ok, msg = verify(cookie)
        print(("✓ " if ok else "✗ ") + msg)
        if not ok:
            print("不写入。浏览器里这份 cookie 已经失效了，需要重新登录。")
            return 2

        write_cookie(cookie)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"✗ 失败: {type(e).__name__}: {e}")
        return 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("调试用的 Chrome 已关闭，需要浏览器时自己重新打开即可。")


if __name__ == "__main__":
    raise SystemExit(main())
