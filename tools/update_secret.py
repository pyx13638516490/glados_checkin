#!/usr/bin/env python3
"""一键把 GLaDOS cookie 写进仓库 Secret（GLADOS_COOKIE）。

省掉每月打开网页手动粘贴的步骤。cookie 只在本机内存里过一遍，
通过 GitHub 官方接口加密后提交，不落盘、不打印。

前置
----
    pip install pynacl

需要一个 fine-grained PAT：
    GitHub → Settings → Developer settings → Personal access tokens
    → Fine-grained tokens → Generate new token
      · Repository access: 只勾 pyx13638516490/glados_checkin
      · Permissions → Repository permissions → Secrets: Read and write

用法
----
PowerShell:

    $env:GH_PAT = 'github_pat_xxx'            # 你的 PAT，只在本进程有效
    python tools/update_secret.py

脚本会按以下顺序找 cookie：
    1. 环境变量 GLADOS_COOKIE
    2. 剪贴板（pyperclip 或 tkinter）
    3. 标准输入（脚本会提示你粘贴）

可选环境变量：
    REPO         默认 pyx13638516490/glados_checkin
    SECRET_NAME  默认 GLADOS_COOKIE
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "pyx13638516490/glados_checkin"
DEFAULT_SECRET = "GLADOS_COOKIE"
API = "https://api.github.com"


def read_cookie():
    """按 环境变量 → 剪贴板 → 标准输入 的顺序取 cookie。"""
    cookie = (os.getenv("GLADOS_COOKIE") or "").strip()
    if cookie:
        return cookie, "环境变量 GLADOS_COOKIE"

    try:
        import pyperclip

        cookie = (pyperclip.paste() or "").strip()
        if cookie:
            return cookie, "剪贴板"
    except Exception:  # noqa: BLE001 - 剪贴板不可用是正常分支
        pass

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            cookie = (root.clipboard_get() or "").strip()
        finally:
            root.destroy()
        if cookie:
            return cookie, "剪贴板（tkinter）"
    except Exception:  # noqa: BLE001
        pass

    print("没从环境变量或剪贴板拿到 cookie，请直接粘贴后回车：", flush=True)
    return sys.stdin.readline().strip(), "标准输入"


def github(method, path, token, body=None):
    """调 GitHub REST API，返回 (status, parsed_json)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "glados-checkin-secret-updater",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def main():
    repo = os.getenv("REPO", DEFAULT_REPO).strip()
    secret_name = os.getenv("SECRET_NAME", DEFAULT_SECRET).strip()

    token = (os.getenv("GH_PAT") or "").strip()
    if not token:
        print("错误：没有设置 GH_PAT 环境变量", file=sys.stderr)
        print('  PowerShell: $env:GH_PAT = "github_pat_xxx"', file=sys.stderr)
        return 1

    cookie, source = read_cookie()
    if not cookie:
        print("错误：没有拿到 cookie", file=sys.stderr)
        return 1

    if "koa:sess" not in cookie or "koa:sess.sig" not in cookie:
        print(
            f"警告：cookie 里没同时出现 koa:sess 和 koa:sess.sig，仍会写入",
            file=sys.stderr,
        )

    print(f"仓库      : {repo}")
    print(f"Secret 名 : {secret_name}")
    print(f"cookie 来源: {source}")
    print(f"cookie 长度: {len(cookie)}")

    # 1) 取仓库公钥
    _, key_data = github("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    key_id = key_data["key_id"]
    public_key_b64 = key_data["key"]

    # 2) libsodium sealed box 加密（GitHub 要求）
    from nacl import encoding, public

    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(cookie.encode("utf-8"))
    encrypted_value = base64.b64encode(sealed).decode("ascii")

    # 3) 写入 Secret（成功返回 201 或 204）
    status, _ = github(
        "PUT",
        f"/repos/{repo}/actions/secrets/{secret_name}",
        token,
        {"encrypted_value": encrypted_value, "key_id": key_id},
    )

    if status in (201, 204):
        print(f"\n完成：Secret {secret_name} 已更新（HTTP {status}）")
        print("可以去 Actions 页面跑一次 Run workflow 验证。")
        return 0

    print(f"未预期的响应状态：{status}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        print(f"\nGitHub API 失败 HTTP {exc.code}: {body}", file=sys.stderr)
        hints = {
            401: "PAT 无效或已过期",
            403: "PAT 缺少 Secrets 写权限",
            404: "仓库名/Secret 名不对，或 PAT 没勾选这个仓库",
        }
        hint = hints.get(exc.code)
        if hint:
            print(f"提示：{hint}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"\n失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
