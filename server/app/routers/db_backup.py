"""数据库备份与恢复接口（结构 + 全量）。

两类端点：
- 用户面 /admin/db-backups：需登录态（Bearer）。提供列表 / 手动备份 / 下载 / 恢复。
  恢复为高危操作，会覆盖当前实时数据库，仅对已登录账号开放并留痕。
- 内部面 /internal/db-backups：供代码修订 Agent 同机调用，无需鉴权（与 /internal/webhooks
  一致，仅本机可达）。用于「代码修订前自动快照数据库」与「回滚时恢复数据库」。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db
from ..routers.auth import require_auth

router_user = APIRouter(prefix="/admin/db-backups", tags=["db-backup"])
router_internal = APIRouter(prefix="/internal/db-backups", tags=["db-backup-internal"])


class BackupReq(BaseModel):
    label: str | None = None
    include_data: bool = True


class DbStarReq(BaseModel):
    pinned: bool = True


# ── 用户面（需登录态）─────────────────────────────────────────────
@router_user.get("")
def list_backups(acc: dict = Depends(require_auth)):
    """列出全部数据库备份（结构 + 全量），按时间倒序。"""
    return {"items": db.list_db_backups()}


@router_user.post("")
def create_backup(req: BackupReq, acc: dict = Depends(require_auth)):
    """手动触发一次数据库备份（默认全量）。"""
    info = db.backup_database(req.label, req.include_data)
    db.log_event(None, "db_backup_created",
                 {"name": info["name"], "by": acc["account_id"], "has_data": info["has_data"]})
    return info


@router_user.post("/{name}/restore")
def restore_backup(name: str, acc: dict = Depends(require_auth)):
    """从全量备份恢复当前数据库（覆盖式，高危）。"""
    try:
        res = db.restore_database(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    db.log_event(None, "db_backup_restored", {"name": name, "by": acc["account_id"]})
    return res


@router_user.get("/{name}/download")
def download_backup(name: str, acc: dict = Depends(require_auth)):
    """下载全量备份文件（.db）。"""
    f = db.get_db_backup_dir() / f"{name}.db"
    if not f.exists():
        raise HTTPException(404, "备份不存在")
    return FileResponse(str(f), filename=f"{name}.db", media_type="application/octet-stream")


@router_user.get("/{name}/schema")
def download_schema(name: str, acc: dict = Depends(require_auth)):
    """下载结构备份文件（.schema.sql，仅 DDL）。"""
    f = db.get_db_backup_dir() / f"{name}.schema.sql"
    if not f.exists():
        raise HTTPException(404, "结构文件不存在")
    return FileResponse(str(f), filename=f"{name}.schema.sql", media_type="text/plain")


@router_user.post("/{name}/star")
def star_backup(name: str, req: DbStarReq, acc: dict = Depends(require_auth)):
    """收藏/取消收藏一份数据库备份（收藏后永不被自动清理；支持全量与结构）。"""
    try:
        info = db.set_db_backup_pinned(name, req.pinned)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    db.log_event(None, "db_backup_starred",
                 {"name": name, "pinned": req.pinned, "by": acc["account_id"]})
    return info


# ── 内部面（代码修订 Agent 同机调用，无需鉴权）─────────────────────
@router_internal.post("")
async def internal_backup(req: BackupReq):
    """代码修订 Agent：在修订执行前自动快照数据库。"""
    return db.backup_database(req.label, req.include_data)


@router_internal.post("/{name}/restore")
async def internal_restore(name: str):
    """代码修订 Agent：回滚时恢复数据库到修订前快照。"""
    try:
        return db.restore_database(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
