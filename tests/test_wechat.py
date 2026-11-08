"""微信社会化登录（演示模式）验收。

运行前提：调度服务已在 http://127.0.0.1:8300 启动（未配置 WECHAT_APPID 即演示模式）。
覆盖：授权重定向 → 回调签发 token → /auth/me → 同 openid 幂等复用账号。
"""
import base64
import urllib.request
import json
import sys

BASE = "http://127.0.0.1:8300"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不跟随重定向，便于断言 307


_opener = urllib.request.build_opener(_NoRedirect)


def _get(path, accept="*/*", follow=False):
    req = urllib.request.Request(BASE + path, headers={"Accept": accept})
    try:
        if follow:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, r.read().decode(), dict(r.headers)
        else:
            with _opener.open(req, timeout=8) as r:
                return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def _decode_state(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s).decode()


def test_wechat_mock_login():
    # 1) 授权：演示模式应 307 重定向到 callback?code=mock
    st, _, hdrs = _get("/auth/wechat/authorize?redirect=" +
                       urllib.parse.quote(BASE + "/"))
    assert st in (301, 302, 307), f"authorize 期望重定向，实际 {st}"
    loc = hdrs.get("location", "")
    assert "code=mock" in loc, f"授权回跳应带 mock code，实际 {loc}"
    print("✓ 授权重定向（演示模式）")

    # 2) 回调（mock）：应签发 token 与微信账号
    st, body, _ = _get("/auth/wechat/callback?code=mock_abc123&state=" +
                       base64.urlsafe_b64encode(b"http://127.0.0.1:8300/").decode().rstrip("="),
                       accept="application/json")
    assert st == 200, f"callback 期望 200，实际 {st}: {body}"
    d = json.loads(body)
    assert d["ok"] and d.get("token"), "回调未返回 token"
    acc = d["account"]
    assert acc["wechat_bound"] is True, "账号应标记为已绑定微信"
    assert acc["avatar_url"], "应返回微信头像（演示 data URI）"
    assert acc["points"] == 100, "微信首次登录应赠送 100 积分"
    token = d["token"]
    openid_like = acc["phone"].startswith("wx_")
    print(f"✓ 回调签发 token + 微信账号（{acc['nickname']}，积分 {acc['points']}）")

    # 3) /auth/me 用 token 可取到账号
    st, body, _ = _get("/auth/me", accept="application/json")
    # 不带 token 应 401；这里单独构造带 token 的请求
    req = urllib.request.Request(BASE + "/auth/me",
                                 headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        me = json.loads(r.read().decode())
    assert me["account_id"] == acc["account_id"], "me 与回调账号不一致"
    print("✓ /auth/me 鉴权通过")

    # 4) 同一 code（同 openid）再次登录应复用同一账号（幂等），且不重复赠分
    st, body, _ = _get("/auth/wechat/callback?code=mock_abc123&state=" +
                       base64.urlsafe_b64encode(b"http://127.0.0.1:8300/").decode().rstrip("="),
                       accept="application/json")
    d2 = json.loads(body)
    assert d2["account"]["account_id"] == acc["account_id"], "同 openid 应复用账号"
    assert d2["account"]["points"] == 100, "重复登录不应再赠分"
    print("✓ 同 code 幂等复用账号（不重复赠分）")
    return True


if __name__ == "__main__":
    try:
        test_wechat_mock_login()
        print("\n微信登录验收全部通过 ✅")
    except AssertionError as e:
        print("❌ 失败：", e)
        sys.exit(1)
    except Exception as e:
        print("⚠️  跳过/异常（确认服务已启动于", BASE, "）：", e)
        sys.exit(2)
