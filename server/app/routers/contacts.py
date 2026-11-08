"""通讯录与好友关系（App 自建，微信仅作身份锚点）。

- 搜索已注册账号（手机号/昵称）→ 发送好友请求 → 对方接受 → 双向互加。
- 不镜像微信好友：微信开放平台不提供好友列表接口，通讯录必须 App 自建。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from .auth import require_auth

router = APIRouter(prefix="/contacts", tags=["contacts"])


class SearchReq(BaseModel):
    q: str


class RequestReq(BaseModel):
    friend_account_id: str


class AcceptReq(BaseModel):
    request_id: str


@router.post("/search")
def search(req: SearchReq, acc: dict = Depends(require_auth)):
    """按手机号或昵称搜索已注册账号（排除自己）。"""
    return db.search_accounts(req.q.strip(), acc["account_id"])


@router.post("/request")
def request_friend(req: RequestReq, acc: dict = Depends(require_auth)):
    """向某账号发送好友请求（已为好友或已存在 pending 请求则返回 already）。"""
    if not db.get_account(req.friend_account_id):
        raise HTTPException(404, "目标账号不存在")
    if req.friend_account_id == acc["account_id"]:
        raise HTTPException(400, "不能添加自己为好友")
    r = db.create_friend_request(acc["account_id"], req.friend_account_id)
    if r is None:
        return {"ok": True, "already": True}
    return {"ok": True, "request": r}


@router.get("/requests")
def incoming_requests(acc: dict = Depends(require_auth)):
    """我收到的待处理好友请求。"""
    return db.list_friend_requests(acc["account_id"])


@router.post("/requests/accept")
def accept_request(req: AcceptReq, acc: dict = Depends(require_auth)):
    """接受一条好友请求 → 双向互加通讯录。"""
    a = db.accept_friend_request(req.request_id, acc["account_id"])
    if not a:
        raise HTTPException(404, "请求不存在或已处理")
    return {"ok": True, "friend": db._pub_contact(a)}


@router.get("")
def my_contacts(acc: dict = Depends(require_auth)):
    """我的通讯录好友列表。"""
    return db.list_contacts(acc["account_id"])


@router.delete("/{account_id}")
def remove_contact(account_id: str, acc: dict = Depends(require_auth)):
    """删除一位好友（双向移除）。"""
    db.remove_contact(acc["account_id"], account_id)
    return {"ok": True}
