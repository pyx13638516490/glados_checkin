"""checkin.py 自测。不联网，全部走 mock。

运行：
    python test_checkin.py
"""

import json
import os
from unittest import mock

import checkin as c


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise c.requests.HTTPError(f"HTTP {self.status_code}")


def run_main(routes, status_code=200):
    """把 HTTP 层换成假实现跑一遍 main()，返回 (退出码, 实际请求过的 URL)。"""
    seen = []

    def fake_request(method, url, timeout=None, headers=None, data=None):
        seen.append(url)
        for fragment, payload in routes:
            if fragment in url:
                return FakeResponse(status_code, payload)
        raise AssertionError(f"未预期的请求: {url}")

    env = {
        "GLADOS_COOKIE": "koa:sess=abc; koa:sess.sig=def",
        "GLADOS_CHECKIN_TOKEN": "",
        "GLADOS_BASE_URL": "https://glados.cloud",
        "GLADOS_DOMAIN_FALLBACK": "0",
    }
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(c.requests.Session, "request", side_effect=fake_request), \
            mock.patch.object(c.time, "sleep", return_value=None):
        try:
            code = c.main()
        except c.RetryableError:
            code = c.EXIT_RETRYABLE
        except c.FatalError:
            code = c.EXIT_FATAL
    return code, seen


# --- 单元：响应分类 -------------------------------------------------------

def test_classify():
    cases = [
        ("旧 token 的响应", {"code": 1, "message": "please checkin via https://glados.cloud"}, "token_error"),
        ("正常签到", {"code": 0, "message": "Checkin! Got 12 Points"}, "success"),
        ("重复签到算成功", {"code": 1, "message": "Checkin Repeats! Please Try Tomorrow"}, "success"),
        ("新版文案", {"code": 1, "message": "Today's observation logged"}, "success"),
        ("code=0 无文案", {"code": 0, "message": ""}, "success"),
        ("cookie 失效(英文)", {"code": -1, "message": "Not logged in"}, "auth_error"),
        ("cookie 失效(中文-没有权限)", {"code": -2, "message": "没有权限"}, "auth_error"),
        ("cookie 失效(中文-未登录)", {"code": -2, "message": "未登录"}, "auth_error"),
        ("不认识的响应", {"code": 9, "message": "Something new"}, "unknown"),
        ("非 JSON 响应", {"code": None, "message": "Non-JSON response", "text": "<html>"}, "unknown"),
    ]
    for name, payload, want in cases:
        got = c.classify_checkin(payload)
        assert got == want, f"{name}: got {got!r}, want {want!r}"
        print(f"  PASS 分类: {name} -> {got}")


def test_get_env():
    os.environ["T_EMPTY"] = "   "
    os.environ["T_VAL"] = " x "
    assert c.get_env("T_EMPTY", "fallback") == "fallback", "空串应回退默认值"
    assert c.get_env("T_VAL", "fallback") == "x", "应去掉首尾空格"
    assert c.get_env("T_NOPE", "fallback") == "fallback", "未设置应用默认值"
    del os.environ["T_EMPTY"], os.environ["T_VAL"]
    print("  PASS 配置读取: 空串/空格/未设置")


def test_resolve_token():
    os.environ["GLADOS_CHECKIN_TOKEN"] = "glados.one"
    assert c.resolve_token() == "glados.cloud", "旧 token 应被自动纠正"
    os.environ["GLADOS_CHECKIN_TOKEN"] = ""
    assert c.resolve_token() == "glados.cloud", "空 token 应回退默认值"
    os.environ["GLADOS_CHECKIN_TOKEN"] = "custom.token"
    assert c.resolve_token() == "custom.token", "自定义 token 应保留"
    del os.environ["GLADOS_CHECKIN_TOKEN"]
    assert c.resolve_token() == "glados.cloud"
    print("  PASS token 解析: 旧值纠正/空值回退/自定义保留")


def test_candidate_urls():
    os.environ["GLADOS_BASE_URL"] = "https://glados.cloud"
    os.environ["GLADOS_DOMAIN_FALLBACK"] = ""
    assert c.candidate_base_urls() == [
        "https://glados.cloud",
        "https://glados.rocks",
        "https://glados.network",
    ]
    os.environ["GLADOS_DOMAIN_FALLBACK"] = "0"
    assert c.candidate_base_urls() == ["https://glados.cloud"]
    del os.environ["GLADOS_BASE_URL"], os.environ["GLADOS_DOMAIN_FALLBACK"]
    print("  PASS 域名候选: 回退顺序与关闭开关")


def test_explain_mentions_real_cause():
    text = c.explain_failure(
        "token_error",
        {"message": "please checkin via https://glados.cloud"},
        "glados.one",
        "https://glados.cloud",
    )
    assert "glados.cloud" in text and "token" in text, "应给出 token 修复方式"
    assert "不是反脚本拦截" in text, "应点明这不是反脚本拦截"
    print("  PASS 失败说明: 指出真正原因与修复方式")


# --- 端到端：退出码语义 ---------------------------------------------------

def test_token_error_exits_fatal_without_reading_status():
    code, seen = run_main([
        ("/api/user/checkin", {"code": 1, "message": "please checkin via https://glados.cloud"}),
    ])
    assert code == c.EXIT_FATAL, f"token 错误应为不可重试失败, got {code}"
    assert not any("/status" in url for url in seen), "token 错误时不应继续读 status"
    print("  PASS 流程: token 错误 -> 退出码 1，且不误报成功")


def test_successful_checkin_exits_ok():
    code, seen = run_main([
        ("/api/user/checkin", {"code": 0, "message": "Checkin! Got 12 Points"}),
        ("/api/user/status", {"code": 0, "data": {"leftDays": 353}}),
        ("/api/user/points", {"code": 0, "points": 46, "history": [{"change": 20}]}),
    ])
    assert code == c.EXIT_OK, f"正常签到应成功退出, got {code}"
    assert any("/points" in url for url in seen), "应读取积分用于对照"
    print("  PASS 流程: 正常签到 -> 退出码 0")


def test_unknown_response_is_not_reported_as_success():
    code, _ = run_main([
        ("/api/user/checkin", {"code": 9, "message": "???"}),
        ("/api/user/status", {"code": 0, "data": {"leftDays": 1}}),
        ("/api/user/points", {"code": 0, "points": 1, "history": []}),
    ])
    assert code == c.EXIT_FATAL, f"无法确认的响应不能算成功, got {code}"
    print("  PASS 流程: 不认识的响应 -> 报失败而不是谎报成功")


def test_auth_error_stops_immediately():
    code, seen = run_main([
        ("/api/user/checkin", {"code": -1, "message": "Not logged in"}),
    ])
    assert code == c.EXIT_FATAL, code
    assert len(seen) == 1, f"认证失败不该重试, 实际请求 {seen}"
    print("  PASS 流程: cookie 失效 -> 立即失败，不做无意义重试")


def test_server_error_is_marked_retryable():
    code, seen = run_main(
        [("/api/user/checkin", {"code": 0, "message": "boom"})],
        status_code=503,
    )
    assert code == c.EXIT_RETRYABLE, f"5xx 应标记为可重试, got {code}"
    assert len(seen) == c.MAX_ATTEMPTS, f"应重试 {c.MAX_ATTEMPTS} 次, 实际 {len(seen)}"
    print("  PASS 流程: 服务端 5xx -> 退出码 2（workflow 会重试）")


def main():
    print("单元测试:")
    test_classify()
    test_get_env()
    test_resolve_token()
    test_candidate_urls()
    test_explain_mentions_real_cause()
    print("流程测试:")
    test_token_error_exits_fatal_without_reading_status()
    test_successful_checkin_exits_ok()
    test_unknown_response_is_not_reported_as_success()
    test_auth_error_stops_immediately()
    test_server_error_is_marked_retryable()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
