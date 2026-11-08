"""房间与角色权限（PRD 模块5 权限控制表）。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel

from .. import db
from ..models import CreateRoomReq, RenameReq, RoomMetaReq, SilentReq
from ..routers.auth import require_auth


class AddMembersReq(BaseModel):
    account_ids: list[str]
    role: str = "guest"


class EnsureSoftwareReq(BaseModel):
    account_id: str
    nickname: str | None = None

router = APIRouter(prefix="/rooms", tags=["rooms"])


def require_member(room_id: str, member_id: str) -> dict:
    member = db.get_member(room_id, member_id) if member_id else None
    if not member:
        raise HTTPException(403, "成员不存在或不属于该房间")
    return member


def require_host(member: dict) -> None:
    if member["role"] != "host":
        raise HTTPException(403, "该操作仅主持人（咨询师）可执行")


@router.post("")
def create_room(req: CreateRoomReq):
    """创建双人房间：主持人（咨询师）+ 协作专家；可绑定登录账号、设置主题、主题标签与固定类目。"""
    return db.create_room(req.title, req.host_name, req.expert_name,
                          account_id=req.account_id, theme=req.theme, tags=req.tags,
                          category=req.category, task_template=req.task_template)


@router.post("/ensure-software-revision")
def ensure_software_revision(req: EnsureSoftwareReq):
    """登录后自动预建「软件修订」类目房间与模板任务卡（幂等：已存在则复用，无则新建）。"""
    r = db.ensure_software_revision_room(req.account_id, req.nickname)
    room = db.get_room(r["room_id"]) or {}
    room["members"] = db.list_members(r["room_id"])
    room["created"] = r["created"]
    room["task_created"] = r["task_created"]
    return room


@router.get("")
def list_rooms(acc: dict = Depends(require_auth)):
    """登录账号的「个人空间」：仅返回该账号所属房间（房间绑定或成员含该账号）。"""
    return db.list_rooms_for_account(acc["account_id"])


@router.get("/{room_id}")
def get_room(room_id: str):
    room = db.get_room(room_id)
    if not room:
        raise HTTPException(404, "房间不存在")
    room["members"] = db.list_members(room_id)
    return room


@router.post("/{room_id}/silent")
def set_silent(room_id: str, req: SilentReq, x_member_id: str = Header(default="")):
    """开关会议AI代理静默模式（仅主持人）。开启后专家发言不触发会议AI代理。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    db.set_agent_silent(room_id, req.silent)
    return {"room_id": room_id, "agent_silent": req.silent}


@router.put("/{room_id}")
def rename_room(room_id: str, req: RenameReq, x_member_id: str = Header(default="")):
    """修改话题名称（仅主持人）。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    db.rename_room(room_id, req.title)
    return {"room_id": room_id, "title": req.title}


@router.post("/{room_id}/meta")
def update_room_meta(room_id: str, req: RoomMetaReq, x_member_id: str = Header(default="")):
    """设置话题的「主题与主题标签」（仅主持人）：主题作为目录式分组一级目录，标签用于跨主题筛选。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    db.update_room_meta(room_id, req.theme, req.tags)
    return {"room_id": room_id, "theme": req.theme, "tags": req.tags}


@router.post("/{room_id}/leave")
def leave_room(room_id: str, x_member_id: str = Header(default="")):
    """退出话题（主持人不可退出，需先转移或删除话题）。"""
    member = require_member(room_id, x_member_id)
    if member["role"] == "host":
        raise HTTPException(400, "主持人不能退出话题，请先转移主持人或删除话题")
    db.remove_member(room_id, member["member_id"])
    return {"room_id": room_id, "left": True}


@router.post("/{room_id}/members")
def add_members(room_id: str, req: AddMembersReq, x_member_id: str = Header(default="")):
    """主持人把通讯录好友（account_id 列表）加为房间成员。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    added = db.add_room_members(room_id, req.account_ids, req.role)
    return {"room_id": room_id, "added": added,
            "members": db.list_members(room_id)}


@router.get("/{room_id}/flow-settings")
def get_flow_settings(room_id: str, x_member_id: str = Header(default="")):
    """会议流程设置（主持人/专家均可查看），附带可选发言规则说明。"""
    require_member(room_id, x_member_id)
    return {**db.get_flow_settings(room_id),
            "response_mode_options": [{"value": k, **v} for k, v in db.RESPONSE_MODES.items()]}


@router.post("/{room_id}/flow-settings")
def update_flow_settings(room_id: str, settings: dict,
                         x_member_id: str = Header(default="")):
    """更新会议流程设置（仅主持人）：Agent发言规则、撤回窗口、断点提醒、记忆抽取频率。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    mode = settings.get("response_mode")
    if mode is not None and mode not in db.RESPONSE_MODES:
        raise HTTPException(400, f"不支持的发言规则：{mode}")
    if "recall_minutes" in settings:
        try:
            settings["recall_minutes"] = max(0, min(60, int(settings["recall_minutes"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "撤回窗口必须是 0-60 的整数（分钟）")
    return db.save_flow_settings(room_id, settings)


@router.get("/{room_id}/meeting-state")
def meeting_state(room_id: str, x_member_id: str = Header(default="")):
    """会议轮次状态：第几轮、谁已发言 / 待发言、撤回窗口剩余秒数。"""
    require_member(room_id, x_member_id)
    return db.meeting_state_view(room_id)


@router.post("/{room_id}/meeting-state/reset")
def reset_meeting_round(room_id: str, x_member_id: str = Header(default="")):
    """结束当前轮次（主持人）：清空发言登记，下一位发言者开启新一轮。"""
    member = require_member(room_id, x_member_id)
    require_host(member)
    return db.close_round(room_id)
