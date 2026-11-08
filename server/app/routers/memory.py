"""记忆资产接口：快照查询（主持人可见）、主动抽取、导出迁移。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel

from .. import db, memory_extractor
from .rooms import require_member, require_host

router = APIRouter(prefix="/rooms/{room_id}/memory", tags=["memory"])


@router.get("/snapshots")
def snapshots(room_id: str, x_member_id: str = Header(default="")):
    """查看全部记忆资产（仅主持人）。版本倒序。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    return db.list_memory_snapshots(room_id)


@router.post("/snapshots")
def extract_snapshot(room_id: str, x_member_id: str = Header(default="")):
    """主动触发一次结构化记忆抽取（定时/主动均可）。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    return memory_extractor.extract_and_snapshot(room_id)


@router.get("/export")
def export(room_id: str, x_member_id: str = Header(default="")):
    """导出记忆资产（JSON）：对话、任务、断点决策、记忆快照，可迁移到外部Agent。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    data = {
        "room": db.get_room(room_id),
        "members": db.list_members(room_id),
        "messages": db.list_messages(room_id),
        "tasks": db.list_tasks(room_id),
        "breakpoints": db.list_breakpoints(room_id),
        "memory_snapshots": db.list_memory_snapshots(room_id),
    }
    return Response(content=json.dumps(data, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="memory_{room_id}.json"'})
