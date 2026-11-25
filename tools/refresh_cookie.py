#!/usr/bin/env python3
"""用 Chrome DevTools Protocol 从浏览器读 GLaDOS cookie，更新 cookie.txt。

## 为什么能读到（和之前失败的区别）

Chrome 157 的 cookie 是 App-Bound 加密，密钥只在**正常实例**（有窗口、
elevation service 可用）里存在。所以：

  * `--headless=new` 启动 → 解不开 → Chrome 把这些 cookie 当垃圾清掉（踩过）
  * **带窗口启动** → 能解密 → `Network.getAllCookies` 直接拿到明文值 ✓

另一个坑：Chrome 157 拒绝在默认 profile 目录上开调试端口
（"DevTools remote debugging requires a non-default data directory"）。
用目录联接（junction）指向原 profile 可以绕过：路径看起来不同，实际读写
仍是原 profile，所以不用复制几 GB 的数据。

## 安全措施

  * 读之前先备份整个 Cookies 库到临时目录
  * 读完检查原库条目数，变少就自动从备份恢复
  * cookie.txt 只在「与浏览器里的不一致」且「在线验证通过」时才覆盖，覆盖前再备份

## 用法

    python tools/refresh_cookie.py --read            # 从浏览器更新 cookie.txt
    python tools/refresh_cookie.py --read --dry-run  # 只对比，不写
    python tools/refresh_cookie.py --read --kill-chrome   # 自动关 Chrome
    python tools/refresh_cookie.py --restore         # 反方向：cookie.txt 写回浏览器
    python tools/refresh_cookie.py --self-test       # 验证 CDP 读写链路（临时 profile）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
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

from save_cookie import COOKIE_FILE, verify, write_cookie  # noqa: E402

WANTED = ["gld:accts", "gld:accts.sig", "gld:sess", "gld:sess.sig",
          "koa:sess", "koa:sess.sig"]

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


def count_glados(db_path):
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("select count(*) from cookies where host_key like '%glados%'")
        n = cur.fetchone()[0]
        con.close()
        return n
    except Exception:  # noqa: BLE001
        return -1


def backup_cookies(cookie_dir):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = Path(tempfile.gettempdir()) / f"ck-backup-{stamp}"
    bak.mkdir(parents=True, exist_ok=True)
    for f in cookie_dir.glob("Cookies*"):
        shutil.copy2(f, bak / f.name)
    return bak


def restore_cookies(bak, cookie_dir):
    for f in bak.iterdir():
        shutil.copy2(f, cookie_dir / f.name)


def launch(chrome, port, user_data_dir, profile_name="Default", headless=False):
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
    ]
    if headless:
        args.insert(1, "--headless=new")
        args.append("about:blank")
    else:
        args += ["--new-window", "about:blank"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def endpoints(port, timeout=60):
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


async def cdp_seq(ws_url, commands, timeout=30):
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


def make_junction(name):
    junc = Path(tempfile.gettempdir()) / name
    if junc.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)
    return junc


def drop_junction(junc):
    if junc and junc.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)


def parse_cookie_file():
    if not COOKIE_FILE.exists():
        return {}
    pairs = {}
    for item in COOKIE_FILE.read_text(encoding="utf-8").strip().split(";"):
        name, _, value = item.strip().partition("=")
        if name and value:
            pairs[name] = value
    return pairs


def self_test(chrome, port):
    tmp = tempfile.mkdtemp(prefix="ck-selftest-")
    print(f"临时 profile: {tmp}")
    proc = launch(chrome, port + 1, tmp, "Default", headless=True)
    try:
        page_ws, _ = endpoints(port + 1, timeout=30)
        _, get_res = asyncio.run(cdp_seq(page_ws, [
            ("Network.setCookie", {"name": "gld:sess", "value": "TEST-VALUE-123",
                                   "domain": ".glados.rocks", "path": "/",
                                   "httpOnly": True, "secure": True}),
            ("Network.getAllCookies", {}),
        ]))
        got = [c for c in get_res.get("cookies", [])
               if c.get("name") == "gld:sess" and "glados" in (c.get("domain") or "")]
        if got and got[0].get("value") == "TEST-VALUE-123":
            print("✓ 自测通过：CDP 读写 HttpOnly cookie 正常")
            return 0
        print(f"✗ 自测失败: {got}")
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


def read_from_browser(chrome, port, user_data, profile_name, dry_run):
    ckdir = Path(user_data) / profile_name / "Network"
    if not ckdir.exists():
        ckdir = Path(user_data) / profile_name
    db = ckdir / "Cookies"
    if not db.exists():
        print(f"✗ 找不到 cookie 库: {db}")
        return 2

    bak = backup_cookies(ckdir)
    before = count_glados(bak / "Cookies")
    print(f"已备份到 {bak}（glados 条目 {before}）")

    junc = make_junction("ck-read-junction")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(user_data)],
                   capture_output=True, text=True, check=True)
    print("启动带窗口 Chrome（会闪一下窗口，读完自动关）...")
    proc = launch(chrome, port, junc, profile_name, headless=False)
    cookie = ""
    try:
        page_ws, browser_ws = endpoints(port)
        time.sleep(3)  # 让 CookieMonster 把库里的 cookie 加载进内存
        res = asyncio.run(cdp_seq(page_ws, [("Network.getAllCookies", {})]))[0]
        cookies = res.get("cookies", [])
        picked = {}
        for c in cookies:
            name = c.get("name")
            if name in WANTED and "glados" in (c.get("domain") or ""):
                picked[name] = c.get("value")
        print(f"CDP 拿到 glados 字段: {', '.join(picked) if picked else '无'}")
        if not picked:
            print("✗ 浏览器里没有 glados cookie（可能没登录，或 profile 选错了）")
            return 2
        cookie = "; ".join(f"{n}={picked[n]}" for n in WANTED if n in picked)

        # 优雅关闭，避免 Chrome 来不及 flush
        try:
            asyncio.run(cdp_seq(browser_ws, [("Browser.close", {})], timeout=10))
        except Exception:  # noqa: BLE001
            pass
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
        drop_junction(junc)
        after = count_glados(db)
        print(f"原库 glados 条目: {after}（读前 {before}）")
        if after < before:
            print("库被削了，从备份恢复 ...")
            restore_cookies(bak, ckdir)
            print(f"恢复后: {count_glados(db)}")
        elif after == before:
            print("原库完好")

    old = parse_cookie_file()
    if old == picked:
        print("✓ 浏览器里的 cookie 和 cookie.txt 完全一致，不用更新")
        return 0
    if not old:
        print("cookie.txt 里没有字段，直接写入")
    else:
        changed = [n for n in picked if old.get(n) != picked[n]]
        print(f"差异字段: {', '.join(changed) if changed else '（仅顺序/多余字段不同）'}")

    if dry_run:
        print(f"[dry-run] 不写文件。读取长度 {len(cookie)}")
        return 0

    ok, msg = verify(cookie)
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        print("不写入。浏览器里这份 cookie 服务端已不认，需要重新登录。")
        return 2
    write_cookie(cookie)
    return 0


def restore_to_browser(chrome, port, user_data, profile_name):
    pairs = parse_cookie_file()
    if not pairs:
        print("✗ cookie.txt 是空的，没东西可恢复")
        return 2
    print(f"cookie.txt 有 {len(pairs)} 个字段")
    ok, msg = verify("; ".join(f"{k}={v}" for k, v in pairs.items()))
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        print("不写回：这份 cookie 服务端已失效，先重新登录再更新 cookie.txt。")
        return 2

    ckdir = Path(user_data) / profile_name / "Network"
    if not ckdir.exists():
        ckdir = Path(user_data) / profile_name
    bak = backup_cookies(ckdir)
    print(f"已备份到 {bak}")

    junc = make_junction("ck-restore-junction")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(user_data)],
                   capture_output=True, text=True, check=True)
    proc = launch(chrome, port, junc, profile_name, headless=False)
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
        time.sleep(3)
        try:
            asyncio.run(cdp_seq(browser_ws, [("Browser.close", {})], timeout=10))
        except Exception:  # noqa: BLE001
            pass
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        print("已写回浏览器")
        return 0 if okn == len(cmds) else 2
    except Exception as e:  # noqa: BLE001
        print(f"✗ 失败: {type(e).__name__}: {e}")
        return 2
    finally:
        if proc.poll() is None:
            proc.terminate()
        drop_junction(junc)


def main():
    ap = argparse.ArgumentParser(
        description="从 Chrome 读 cookie 更新 cookie.txt（或反向写回）")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--profile", default=os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data"))
    ap.add_argument("--profile-name", default="Default")
    ap.add_argument("--kill-chrome", action="store_true",
                    help="先结束 chrome.exe（未保存页面会丢）")
    ap.add_argument("--read", action="store_true", help="从浏览器读并更新 cookie.txt")
    ap.add_argument("--restore", action="store_true", help="把 cookie.txt 写回浏览器")
    ap.add_argument("--dry-run", action="store_true", help="只对比不写文件")
    ap.add_argument("--self-test", action="store_true", help="验证 CDP 读写链路")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("✗ 找不到 chrome.exe")
        return 2

    if args.self_test:
        return self_test(chrome, args.port)

    if not (args.read or args.restore):
        print("用法：\n"
              "  从浏览器更新 cookie.txt   → --read\n"
              "  把 cookie.txt 写回浏览器   → --restore\n"
              "  验证 CDP 链路             → --self-test")
        return 1

    if chrome_running():
        if not args.kill_chrome:
            print("✗ Chrome 正在运行，它的 user-data-dir 是独占的。\n"
                  "  先完全退出 Chrome，或加 --kill-chrome 让我关（未保存页面会丢）。")
            return 3
        print("关闭 Chrome ...")
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                       capture_output=True, text=True, timeout=60)
        time.sleep(3)
        if chrome_running():
            print("✗ 还有 chrome.exe 没退干净")
            return 2

    if args.read:
        return read_from_browser(chrome, args.port, args.profile,
                                 args.profile_name, args.dry_run)
    return restore_to_browser(chrome, args.port, args.profile, args.profile_name)


if __name__ == "__main__":
    raise SystemExit(main())
