"""话题类目管理（用户可新增 / 重命名 / 删除，按账号隔离，带排重）。

类目是「创建话题」时的可选分类维度，区别于房间自带的「主题(theme)」。
系统预置两类目：普通话题（code=''，默认）/ 软件修订（code='software_revision'，
绑定软件修订任务卡模板）。用户可自定义任意类目，并可重命名（含系统类目的显示名，
但 code 不变以保护模板逻辑）；重名将被排重拒绝（409）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..routers.auth import require_auth


class CategoryCreateReq(BaseModel):
    name: str


class CategoryRenameReq(BaseModel):
    code: str
    name: str


class CategoryDeleteReq(BaseModel):
    code: str


router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("")
def get_categories(acc: dict = Depends(require_auth)):
    """列出当前账号的全部类目（含系统预置）。"""
    return db.list_categories(acc["account_id"])


@router.post("")
def create_category(req: CategoryCreateReq, acc: dict = Depends(require_auth)):
    """新增用户类目（排重：同名已存在则返回 409）。"""
    try:
        return db.create_category(acc["account_id"], req.name)
    except ValueError as e:
        if str(e) == "EXISTS":
            raise HTTPException(409, "已存在同名类目")
        raise HTTPException(400, str(e))


@router.put("/rename")
def rename_category(req: CategoryRenameReq, acc: dict = Depends(require_auth)):
    """重命名类目（排重：目标名与其他类目冲突返回 409；系统类目仅改显示名，code 不变）。"""
    try:
        return db.rename_category(acc["account_id"], req.code, req.name)
    except ValueError as e:
        msg = str(e)
        if msg == "EXISTS":
            raise HTTPException(409, "已存在同名类目")
        if msg == "NOTFOUND":
            raise HTTPException(404, "类目不存在")
        raise HTTPException(400, msg)


@router.post("/delete")
def delete_category(req: CategoryDeleteReq, acc: dict = Depends(require_auth)):
    """删除用户类目（系统类目不可删，返回 400）：其下房间回退为默认类目（普通话题）。"""
    try:
        return db.delete_category(acc["account_id"], req.code)
    except ValueError as e:
        msg = str(e)
        if msg == "SYSTEM":
            raise HTTPException(400, "系统类目不可删除")
        if msg == "NOTFOUND":
            raise HTTPException(404, "类目不存在")
        raise HTTPException(400, msg)
