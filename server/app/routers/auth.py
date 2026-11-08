"""账号与登录态（MVP 轻量登录）。

设计（演示用，无短信网关）：
- 以 11 位手机号作为账号唯一标识；首次登录即自动注册并赠送 100 积分。
- 支持微信社会化登录（OAuth2）：授权后获取昵称与头像，按 openid 复用账号。
- 登录态为服务端签发的会话 token，通过 `Authorization: Bearer <token>` 传递。
- 受保护接口用 `require_auth` 依赖解析账号；其余房间/任务接口维持原 `X-Member-Id` 隔离，不受影响。
"""
from __future__ import annotations

import base64
import re
import time
import uuid
import urllib.parse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import config, db
from ..models import LoginReq, UpdateProfileReq, SmsSendReq, SmsVerifyReq

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_TTL = 60 * 60 * 24 * 30  # 会话有效期：30 天
WELCOME_POINTS = 100            # 注册欢迎积分
SIGNIN_POINTS = 5               # 每日签到积分


def _pub(acc: dict) -> dict:
    return {
        "account_id": acc["account_id"],
        "phone": acc["phone"],
        "nickname": acc["nickname"],
        "avatar": acc["avatar"],
        "avatar_color": acc["avatar_color"],
        "avatar_url": acc.get("avatar_url") or None,
        "wechat_bound": bool(acc.get("wechat_openid")),
        "points": acc["points"],
    }


def require_auth(authorization: str = Header(default="")) -> dict:
    """解析 Bearer token，返回当前登录账号；失败抛 401。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录或登录态已失效")
    token = authorization[7:].strip()
    acc_id = db.get_session(token)
    if not acc_id:
        raise HTTPException(401, "未登录或登录态已失效")
    acc = db.get_account(acc_id)
    if not acc:
        raise HTTPException(401, "账号不存在")
    return acc


_PHONE_RE = re.compile(r"^1\d{10}$")


@router.post("/login")
def login(req: LoginReq):
    """登录 / 注册：手机号已存在则登录（并按需更新昵称/头像），否则新建账号。"""
    phone = re.sub(r"\s", "", req.phone or "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "请输入有效的 11 位手机号")

    acc = db.get_account_by_phone(phone)
    if acc:
        patch: dict = {}
        if req.nickname and req.nickname.strip():
            patch["nickname"] = req.nickname.strip()
        if req.avatar:
            patch["avatar"] = req.avatar
        if req.avatar_color:
            patch["avatar_color"] = req.avatar_color
        if patch:
            acc = db.update_account(acc["account_id"], **patch)
    else:
        nickname = req.nickname.strip() if req.nickname and req.nickname.strip() else f"用户{phone[-4:]}"
        acc = db.create_account(phone, nickname, req.avatar or "🙂", req.avatar_color or "#07c160", WELCOME_POINTS)

    token = db.create_session(acc["account_id"], SESSION_TTL)
    return {"token": token, "account": _pub(acc)}


@router.get("/me")
def me(acc: dict = Depends(require_auth)):
    """当前登录账号信息。"""
    return _pub(acc)


@router.post("/logout")
def logout(authorization: str = Header(default="")):
    """退出登录：吊销当前会话 token。"""
    if authorization.startswith("Bearer "):
        db.delete_session(authorization[7:].strip())
    return {"ok": True}


@router.put("/profile")
def update_profile(req: UpdateProfileReq, acc: dict = Depends(require_auth)):
    """修改昵称 / 头像 / 手机号。"""
    patch: dict = {}
    if req.nickname is not None:
        patch["nickname"] = req.nickname.strip() or acc["nickname"]
    if req.avatar is not None:
        patch["avatar"] = req.avatar
    if req.avatar_color is not None:
        patch["avatar_color"] = req.avatar_color
    if req.phone is not None:
        p = re.sub(r"\s", "", req.phone)
        if not _PHONE_RE.match(p):
            raise HTTPException(400, "手机号格式不正确")
        if p != acc["phone"] and db.get_account_by_phone(p):
            raise HTTPException(400, "该手机号已被其他账号使用")
        patch["phone"] = p
    acc = db.update_account(acc["account_id"], **patch)
    return _pub(acc)


@router.post("/points/signin")
def signin(acc: dict = Depends(require_auth)):
    """每日签到：每天首次签到 +5 积分。"""
    today = time.strftime("%Y-%m-%d")
    last = acc.get("last_signin") or 0
    last_day = time.strftime("%Y-%m-%d", time.localtime(last)) if last else ""
    if last_day == today:
        return {"already": True, "points": acc["points"], "gained": 0}
    db.update_account(acc["account_id"], last_signin=_now())
    acc = db.add_points(acc["account_id"], SIGNIN_POINTS)
    return {"already": False, "points": acc["points"], "gained": SIGNIN_POINTS}


# ─────────────────────────────────────────────────────────────────────
# 手机验证码（演示用，无真实短信网关）
# 内存存储：phone -> (code, expiry_ts)。生产环境应替换为 Redis + 真实短信通道。
# 演示模式（SMS_DEV_MODE）下 send 接口会回显 dev_code，便于本地联调。
# ─────────────────────────────────────────────────────────────────────
_SMS_STORE: dict[str, tuple[str, float]] = {}
SMS_TTL = 5 * 60  # 验证码有效期：5 分钟


@router.post("/sms/send")
def sms_send(req: SmsSendReq):
    """发送手机验证码；演示模式回显 dev_code。"""
    phone = re.sub(r"\s", "", req.phone or "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "请输入有效的 11 位手机号")
    code = f"{uuid.uuid4().int % 1000000:06d}"
    _SMS_STORE[phone] = (code, _now() + SMS_TTL)
    resp: dict = {"ok": True}
    if config.SMS_DEV_MODE:
        resp["dev_code"] = code
    return resp


@router.post("/sms/verify")
def sms_verify(req: SmsVerifyReq):
    """校验手机验证码；通过后即视为该手机号验证有效（用于绑定/改绑）。"""
    phone = re.sub(r"\s", "", req.phone or "")
    code = re.sub(r"\s", "", req.code or "")
    pair = _SMS_STORE.get(phone)
    if not pair:
        raise HTTPException(400, "请先获取验证码")
    stored_code, expiry = pair
    if _now() > expiry:
        _SMS_STORE.pop(phone, None)
        raise HTTPException(400, "验证码已过期，请重新获取")
    if code != stored_code:
        raise HTTPException(400, "验证码不正确")
    _SMS_STORE.pop(phone, None)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# 微信社会化登录（OAuth2）
# 流程：前端打开 /auth/wechat/authorize → 微信授权页 → 回跳 /auth/wechat/callback
#       → 后端用 code 换 access_token + 用户信息 → 落库并签发会话 token
#       → popup 场景用 postMessage 回传 token；整页回跳场景用 location.hash 回传
# 演示模式（未配置 WECHAT_APPID）：authorize 直接回跳 callback?code=mock，
#       由 callback 用内置演示账号完成登录，全流程可联调。
# ─────────────────────────────────────────────────────────────────────
def _b64_encode_url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64_decode_url(s: str) -> str:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()
    except Exception:
        return ""


def _wechat_demo_avatar(seed: str) -> str:
    """演示用头像（离线 data URI，避免依赖外网图片）。"""
    hue = sum(ord(c) for c in seed) % 360
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
        f'<rect width="96" height="96" rx="14" fill="hsl({hue},42%,42%)"/>'
        f'<g fill="#fff" opacity="0.95">'
        f'<ellipse cx="36" cy="40" rx="13" ry="11"/>'
        f'<ellipse cx="64" cy="40" rx="13" ry="11"/>'
        f'<path d="M26 64 q22 -16 44 0 q-22 14 -44 0Z"/></g></svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


async def _wechat_http(url: str, params: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=6) as client:
        if "access_token" in params:  # GET with query
            r = await client.get(url, params=params)
        else:  # GET with query (token 交换同样是 GET)
            r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


@router.get("/wechat/authorize")
def wechat_authorize(redirect: str = "", state: str = ""):
    """发起微信授权。redirect 为授权成功后回跳的前端地址（编码进 state）。"""
    cb = config.WECHAT_REDIRECT_URI
    if config.WECHAT_MOCK or not config.WECHAT_APPID:
        # 演示模式：直接回跳到服务端回调，带上 mock code
        mock_code = "mock_" + uuid.uuid4().hex[:12]
        sep = "&" if "?" in cb else "?"
        target = f"{cb}{sep}code={mock_code}"
        if redirect:
            target += f"&state={_b64_encode_url(redirect)}"
        return RedirectResponse(target)
    # 真实模式：跳到微信授权页
    auth_base = ("https://open.weixin.qq.com/connect/qrconnect"
                 if config.WECHAT_SCOPE == "snsapi_login"
                 else "https://open.weixin.qq.com/connect/oauth2/authorize")
    params = {
        "appid": config.WECHAT_APPID,
        "redirect_uri": cb,
        "response_type": "code",
        "scope": config.WECHAT_SCOPE,
        "state": _b64_encode_url(redirect) if redirect else "splash",
    }
    return RedirectResponse(f"{auth_base}?{'&'.join(f'{k}={v}' for k, v in params.items())}#wechat_redirect")


@router.get("/wechat/callback")
async def wechat_callback(request: Request, code: str = "", state: str = "", mock: int = 0):
    """微信授权回调：用 code 换用户信息并签发会话；返回 popup 脚本 / 整页回跳 / JSON。"""
    # 1) 获取微信用户信息
    if (mock or code.startswith("mock")) and (config.WECHAT_MOCK or not config.WECHAT_APPID):
        # 演示模式：使用「固定」openid，使同一演示微信用户幂等复用同一账号，
        # 避免每次授权都生成新账号 → 反复要求绑定手机号、且回填已有手机号被「已被占用」拦截。
        import hashlib

        openid = "mock_demo_wechat"
        unionid = None
        nickname = "微信用户Demo"
        avatar_url = _wechat_demo_avatar(openid)
    elif code:
        try:
            tok = await _wechat_http(
                "https://api.weixin.qq.com/sns/oauth2/access_token",
                {"appid": config.WECHAT_APPID, "secret": config.WECHAT_APPSECRET,
                 "code": code, "grant_type": "authorization_code"})
            openid = tok.get("openid")
            unionid = tok.get("unionid")
            if not openid:
                raise ValueError(tok.get("errmsg") or "微信授权失败")
            ui = await _wechat_http(
                "https://api.weixin.qq.com/sns/userinfo",
                {"access_token": tok.get("access_token"), "openid": openid, "lang": "zh_CN"})
            nickname = ui.get("nickname") or "微信用户"
            avatar_url = ui.get("headimgurl")
        except Exception as e:  # noqa: BLE001
            return _wechat_result(request, None, error=str(e)[:120])
    else:
        return _wechat_result(request, None, error="缺少 code 参数")

    # 2) 落库并签发会话
    acc = db.upsert_wechat_account(openid, unionid, nickname, avatar_url)
    token = db.create_session(acc["account_id"], SESSION_TTL)
    # 登录成功即保证该账号拥有「软件修订」类目房间 + 模板任务卡（幂等，前端列表据此自动展示）
    try:
        db.ensure_software_revision_room(acc["account_id"], acc["nickname"])
    except Exception:  # noqa: BLE001 — 预建失败不应阻断登录
        pass
    return _wechat_result(request, token, account=_pub(acc))


def _wechat_result(request: Request, token: str | None, account: dict | None = None,
                   error: str | None = None):
    """按请求类型返回：popup 脚本(postMessage) / 整页回跳(hash) / JSON。"""
    app_redirect = _b64_decode_url(state_from_request(request))
    if error:
        payload = {"ok": False, "error": error}
    else:
        payload = {"ok": True, "token": token, "account": account}
    # JSON 请求（便于自动化测试）
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(payload)
    # 整页回跳场景：把 token 放进 hash 回传前端
    if app_redirect and token:
        sep = "#" if "#" not in app_redirect else "&"
        return RedirectResponse(f"{app_redirect}{sep}wechat_token={token}")
    # popup 场景：postMessage 给 opener 后自动关闭
    data = (token and f"{{type:'wechat_login', token:'{token}'}}") or f"{{type:'wechat_error', error:'{error or ''}'}}"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><title>微信登录</title></head>"
        f"<body style='font-family:sans-serif;color:#555;text-align:center;padding-top:40px'>"
        f"{'登录成功，正在返回…' if token else '微信登录失败：' + (error or '')}<script>"
        f"try{{window.opener&&window.opener.postMessage({data},'*');}}catch(e){{}}"
        f"setTimeout(function(){{try{{window.close();}}catch(e){{}}}}, 600);"
        "</script></body></html>"
    )
    return HTMLResponse(html)


def state_from_request(request: Request) -> str:
    return request.query_params.get("state", "")


def _now() -> float:
    return time.time()
