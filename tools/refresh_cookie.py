#!/usr/bin/env python3
"""把 cookie.txt 写回 Chrome，或验证 CDP 链路本身。

## 为什么这个脚本只有 --restore，没有"自动读取"

试过 CDP 路线，结论是当前 Chrome 157 上读不出来，而且有破坏性：

  * Chrome 157 拒绝在默认 profile 目录上开 --remote-debugging-port
    （"DevTools remote debugging requires a non-default data directory"）
  * 用目录联接（junction）指向原 profile 可以绕过这个检查，端口能起来，
    Network.getAllCookies 也能调通
  * 但 App-Bound Encryption 的密钥只在 Chrome 的正常实例里可用；headless
    实例解不开原库的 cookie → 它把这些"无效"条目当成垃圾清掉了
    （实测：8 条 glados cookie 被清空，事后用 cookie.txt 才恢复回来）

所以"自动读 cookie"这条路在当前版本上不成立。要更新 cookie.txt，用
`tools/save_cookie.py` 粘贴一次（30 秒），那是安全的。

这个脚本留下来做两件仍然有价值的事：
  * --self-test  用临时 profile 验证 CDP 能读写 HttpOnly cookie（不碰你的登录态）
  * --restore    把 cookie.txt 里的凭据写回 Chrome —— 浏览器掉登录态时能救回来
                 （换电脑、清过缓存、或者被别的工具弄丢过，都用这个）

用法：
    python tools/refresh_cookie.py --self-test
    python tools/refresh_cookie.py --restore          # 需要先完全退出 Chrome
    python tools/refresh_cookie.py --restore --kill-chrome
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

from save_cookie import COOKIE_FILE, verify  # noqa: E402

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


def launch(chrome, port, user_data_dir, profile_name="Default", headless=True):
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


def endpoints(port, timeout=40):
    """等端口就绪，返回 (page_ws, browser_ws)。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
            browser_ws = v.get("webSocketDebuggerUrl")
            lst = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
            page_ws = next((t["webSocketDebuggerUrl"] for t in lst
                            if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
                           None)
            if browser_ws and page_ws:
                return page_ws, browser_ws
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        time.sleep(0.5)
    raise RuntimeError(f"{timeout} 秒内拿不到调试目标（最后错误: {last}）")


async def cdp_seq(ws_url, commands, timeout=25):
    out = []
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
                    out.append(msg.get("result", {}))
                    break
    return out


def parse_cookie_file():
    raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    pairs = {}
    for item in raw.split(";"):
        name, _, value = item.strip().partition("=")
        if name and value:
            pairs[name] = value
    return pairs


def self_test(chrome, port):
    """临时 profile：写一个 HttpOnly cookie 再读回来，完全不碰你的登录态。"""
    port += 1
    tmp = tempfile.mkdtemp(prefix="ck-selftest-")
    print(f"临时 profile: {tmp}")
    proc = launch(chrome, port, tmp, "Default", headless=True)
    try:
        page_ws, _ = endpoints(port, timeout=30)
        set_res, get_res = asyncio.run(cdp_seq(page_ws, [
            ("Network.setCookie", {
                "name": "gld:sess", "value": "TEST-VALUE-123",
                "domain": ".glados.rocks", "path": "/",
                "httpOnly": True, "secure": True,
            }),
            ("Network.getAllCookies", {}),
        ]))
        got = [c for c in get_res.get("cookies", [])
               if c.get("name") == "gld:sess" and "glados" in (c.get("domain") or "")]
        if got and got[0].get("value") == "TEST-VALUE-123":
            print(f"✓ 自测通过：读写 HttpOnly cookie 正常（httpOnly={got[0].get('httpOnly')}）")
            return 0
        print(f"✗ 自测失败，读回: {got}")
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


def restore(chrome, port, user_data, profile_name):
    """把 cookie.txt 写回 Chrome 的 cookie 库（Chrome 自己加密，所以能落盘）。"""
    if chrome_running():
        print("✗ Chrome 正在运行，user-data-dir 被独占。先完全退出 Chrome。")
        return 3
    if not COOKIE_FILE.exists():
        print(f"✗ 没有 {COOKIE_FILE}，没东西可恢复")
        return 2

    pairs = parse_cookie_file()
    if not pairs:
        print("✗ cookie.txt 是空的")
        return 2
    print(f"cookie.txt 有 {len(pairs)} 个字段: {', '.join(pairs)}")

    ok, msg = verify("; ".join(f"{k}={v}" for k, v in pairs.items()))
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        print("不写回。这份 cookie 在服务端已经失效，先重新登录再更新 cookie.txt。")
        return 2

    # Chrome 157 只肯在"非默认目录"上开调试端口；junction 让路径看起来不同，
    # 实际读写仍是原 profile（所以 cookie 会落到真正的库里）。
    junc = Path(tempfile.gettempdir()) / "ck-restore-junction"
    if junc.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(user_data)],
                   capture_output=True, text=True, check=True)

    proc = launch(chrome, port, junc, profile_name, headless=True)
    try:
        page_ws, browser_ws = endpoints(port)
        expires = int(time.time()) + 30 * 86400
        cmds = []
        for domain in ("glados.rocks", "glados.cloud"):
            for name, value in pairs.items():
                cmds.append(("Network.setCookie", {
                    "name": name, "value": value, "domain": domain, "path": "/",
                    "httpOnly": True, "secure": True, "expires": expires,
                }))
        res = asyncio.run(cdp_seq(page_ws, cmds))
        okn = sum(1 for r in res if not r.get("error"))
        print(f"setCookie: {okn}/{len(cmds)} 成功")
        for r in res:
            if r.get("error"):
                print("  失败:", r["error"])

        # 必须优雅关闭，否则 Chrome 不 flush 到磁盘（强制结束会丢）
        time.sleep(3)
        try:
            asyncio.run(cdp_seq(browser_ws, [("Browser.close", {})], timeout=10))
        except Exception:  # noqa: BLE001
            pass  # 连接被关闭是预期行为
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        print("Chrome 已退出，cookie 已落盘")
        return 0 if okn == len(cmds) else 2
    except Exception as e:  # noqa: BLE001
        print(f"✗ 恢复失败: {type(e).__name__}: {e}")
        return 2
    finally:
        if proc.poll() is None:
            proc.terminate()
        if junc.exists():
            subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)
        print("junction 已清理")


def main():
    ap = argparse.ArgumentParser(description="CDP 工具：恢复 cookie 到 Chrome / 自测")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--profile", default=os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data"))
    ap.add_argument("--profile-name", default="Default")
    ap.add_argument("--kill-chrome", action="store_true",
                    help="先结束 chrome.exe（未保存页面会丢）")
    ap.add_argument("--self-test", action="store_true", help="验证 CDP 读写链路")
    ap.add_argument("--restore", action="store_true",
                    help="把 cookie.txt 写回 Chrome（浏览器掉登录态时用）")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("✗ 找不到 chrome.exe")
        return 2

    if args.self_test:
        return self_test(chrome, args.port)

    if not args.restore:
        print("这个工具不做'自动读取 cookie'，原因见文件头注释。\n"
              "  更新 cookie.txt → python tools/save_cookie.py\n"
              "  把 cookie.txt 写回浏览器 → python tools/refresh_cookie.py --restore\n"
              "  只想验证 CDP 是否可用 → --self-test")
        return 1

    if args.kill_chrome and chrome_running():
        print("关闭 Chrome ...")
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                       capture_output=True, text=True, timeout=60)
        time.sleep(3)

    return restore(chrome, args.port, args.profile, args.profile_name)


if __name__ == "__main__":
    raise SystemExit(main())
