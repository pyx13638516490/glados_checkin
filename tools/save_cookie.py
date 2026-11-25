#!/usr/bin/env python3
"""更新 ~/.glados/cookie.txt —— 从浏览器粘一次就完事。

为什么不能全自动：
  * GLaDOS 没有登录/密码 API，上游 checkin.py 也只认 cookie；
  * Chrome 的 Cookies 数据库被进程独占锁定，读不出文件；
  * Chrome 127+ 的 cookie 值还是 App-Bound 加密，脚本解不开；
  * bsk 只支持点击/输入，不能执行 JS 去读 document.cookie。

所以 cookie 到期后只能人工给一次。这个脚本把这件事压到 30 秒：

    python tools/save_cookie.py

然后在已登录的浏览器里 F12 → Network → 点任意一个 glados 请求
→ 复制整个 `Cookie:` 请求头 → 粘进窗口 → 空行结束。

脚本会：规范化格式 → 校验字段 → 请求 /api/user/status 验证登录态
→ 备份旧文件 → 写入。验证不过就一个字节都不写。

也支持非交互：
    echo "<cookie>" | python tools/save_cookie.py
    python tools/save_cookie.py --from-file cookie.txt
    python tools/save_cookie.py --from-file x.txt --no-verify   # 离线
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checkin import extract_cookie, get_cookie_names, get_session_cookie_kind  # noqa: E402

CONF_DIR = Path.home() / ".glados"
COOKIE_FILE = CONF_DIR / "cookie.txt"

API_BASES = ["https://glados.rocks", "https://glados.cloud"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36 Edg/158.0.0.0"
)


def read_input(from_file):
    if from_file:
        p = Path(from_file)
        if not p.exists():
            print(f"✗ 文件不存在: {p}")
            raise SystemExit(2)
        return p.read_text(encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("把浏览器复制的 Cookie 粘进来，输入空行结束：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def verify(cookie):
    """用 status 接口确认这份 cookie 真的能登录。返回 (ok, 说明)。"""
    for base in API_BASES:
        try:
            r = requests.get(
                f"{base}/api/user/status",
                headers={
                    "User-Agent": UA,
                    "Cookie": cookie,
                    "Referer": f"{base}/console/checkin",
                },
                timeout=20,
            )
            if r.status_code in (401, 403):
                return False, f"{base} HTTP {r.status_code}（未登录）"
            data = r.json()
            if data.get("code") == 0 and data.get("data"):
                left = (data["data"] or {}).get("leftDays")
                try:
                    left = int(float(left))
                except (TypeError, ValueError):
                    left = "?"
                return True, f"登录态有效（套餐剩余 {left} 天）"
            return False, f"{base} 返回 {str(data)[:100]}"
        except Exception as e:  # noqa: BLE001
            print(f"  {base} 请求异常: {type(e).__name__}")
            continue
    return False, "两个域名都没验证成功"


def main():
    ap = argparse.ArgumentParser(description="更新 GLaDOS cookie.txt")
    ap.add_argument("--from-file", help="从文件读取，而不是交互粘贴")
    ap.add_argument("--no-verify", action="store_true", help="跳过在线校验")
    args = ap.parse_args()

    raw = read_input(args.from_file)
    cookie = extract_cookie(raw)
    if not cookie:
        print("✗ 没解析出 cookie（内容为空或格式不认识）")
        return 2

    names = sorted(get_cookie_names(cookie))
    kind = get_session_cookie_kind(cookie)
    print(f"解析到 {len(names)} 个字段: {', '.join(names)}")
    if not kind:
        print("✗ 会话字段不全：需要 gld:sess + gld:sess.sig（新版）"
              "或 koa:sess + koa:sess.sig（旧版）")
        return 2
    print(f"会话类型: {kind}")

    if not args.no_verify:
        ok, msg = verify(cookie)
        print(("✓ " if ok else "✗ ") + msg)
        if not ok:
            print("没有写入。确认是从已登录的浏览器里复制的，再试一次。")
            return 2

    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if COOKIE_FILE.exists():
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
        bak = COOKIE_FILE.with_name(f"cookie.txt.bak-{stamp}")
        shutil.copy2(COOKIE_FILE, bak)
        print(f"旧文件已备份: {bak.name}")
    COOKIE_FILE.write_text(cookie, encoding="utf-8")
    print(f"✓ 已写入 {COOKIE_FILE}（{len(cookie)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
