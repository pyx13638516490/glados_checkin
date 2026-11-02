#!/usr/bin/env python3
"""服务端故障期间守着签到：一旦认证接口恢复，立刻补签。

2026-11-01 的故障表现：无 cookie 的 /api/user/points 秒回，
带 cookie 的同接口 20 秒后 502 或超时（源站认证链路挂）。
这种情况下 checkin.py 会以 exit 1 结束，看着像 cookie 过期，实际不是。

用法
----
    python tools/wait_and_checkin.py                # 默认每 5 分钟探一次，最多守 6 小时
    python tools/wait_and_checkin.py --interval 120 --timeout 3600

cookie 来源（按顺序）：
    1. 环境变量 GLADOS_COOKIE
    2. %TEMP%\\glados_cookie.txt（四段值，空格分隔）
    3. 脚本会在缺 cookie 时报错退出

恢复判据（两个条件同时满足才补签）：
    · 不带 cookie 的 /api/user/points 在 6 秒内返回 200
    · 带 cookie 的 /api/user/points 在 15 秒内返回 200

只做 GET，探测期间不会改动任何服务端状态；真正签到交给 checkin.py。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

BASE = "https://glados.cloud"
PROBE = "/api/user/points"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def load_cookie() -> dict[str, str]:
    """返回 cookie dict，同时兼容四段空格格式和 name=value; 格式。"""
    env = (os.getenv("GLADOS_COOKIE") or "").strip()
    raw = env
    if not raw:
        path = Path(tempfile.gettempdir()) / "glados_cookie.txt"
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        sys.exit("找不到 cookie：请设置 GLADOS_COOKIE，或写到 %TEMP%\\glados_cookie.txt")

    if "=" in raw:
        jar = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()
        return jar

    parts = raw.split()
    if len(parts) != 4:
        sys.exit(f"cookie 应是 4 段（空格分隔），实际 {len(parts)} 段")
    return {
        "gld:sess": parts[0],
        "gld:sess.sig": parts[1],
        "koa:sess": parts[2],
        "koa:sess.sig": parts[3],
    }


def probe(cookies: dict[str, str] | None, timeout: float = 20) -> tuple[int | None, float]:
    """返回 (状态码, 耗时秒)；超时返回 (None, 耗时)。"""
    started = time.time()
    try:
        r = requests.get(BASE + PROBE, cookies=cookies, headers=HEADERS, timeout=timeout)
        return r.status_code, time.time() - started
    except Exception:  # noqa: BLE001 - 任何网络异常都算不可用
        return None, time.time() - started


def healthy(jar: dict[str, str]) -> bool:
    """无 cookie 快速正常 + 带 cookie 正常，才认为认证链路恢复。"""
    anon, anon_s = probe(None, timeout=8)
    if anon != 200 or anon_s > 6:
        print(f"  基线未恢复（无 cookie：{anon}，{anon_s:.1f}s）")
        return False
    auth, auth_s = probe(jar, timeout=15)
    if auth != 200:
        print(f"  基线正常，认证仍异常（带 cookie：{auth}，{auth_s:.1f}s）")
        return False
    return True


def run_checkin(jar: dict[str, str]) -> int:
    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
    env = {**os.environ, "GLADOS_COOKIE": cookie_str}
    print("\n恢复确认，开始补签 ...")
    return subprocess.call([sys.executable, "checkin.py"], env=env)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="探测间隔秒数，默认 300")
    ap.add_argument("--timeout", type=int, default=21600, help="最长守候秒数，默认 6 小时")
    args = ap.parse_args()

    jar = load_cookie()
    deadline = time.time() + args.timeout
    round_no = 0
    while time.time() < deadline:
        round_no += 1
        print(f"[{time.strftime('%H:%M:%S')}] 第 {round_no} 轮探测")
        if healthy(jar):
            return run_checkin(jar)
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        wait = min(args.interval, remaining)
        print(f"  服务端未恢复，{wait} 秒后再试")
        time.sleep(wait)

    print("\n守候超时，服务端仍未恢复；checkin.py 未执行。")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
