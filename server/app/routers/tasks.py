"""任务看板接口：任务列表/详情、断点查询、通路A看板手动填表、取消任务。"""
from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel

from .. import config, db, dispatcher, events, memory_extractor
from ..models import BreakpointAnswerReq, CreateTaskReq, UpdateTaskReq
from ..functions import _validate
from .rooms import require_member, require_host

router = APIRouter(tags=["board"])


@router.get("/rooms/{room_id}/tasks")
def list_tasks(room_id: str, x_member_id: str = Header(default="")):
    """看板任务列表（按房间隔离）。状态：排队中/运行中/待人工确认/已完成/已失败/已取消。"""
    require_member(room_id, x_member_id)
    return db.list_tasks(room_id)


@router.post("/rooms/{room_id}/tasks")
def create_task_manual(room_id: str, req: CreateTaskReq,
                       x_member_id: str = Header(default="")):
    """主持人手动创建任务卡（软件修订类目：输入需求，按房间模板落卡）。

    房间为软件修订类目或其默认模板为 software_revision 时，底座默认代码修订 Agent（可回滚）。
    """
    member = require_member(room_id, x_member_id)
    require_host(member)
    room = db.get_room(room_id) or {}
    template = req.template or room.get("task_template")
    if req.agent_kind:
        agent_kind = req.agent_kind
    elif template == db.TEMPLATE_SOFTWARE or room.get("category") == db.ROOM_CATEGORY_SOFTWARE:
        agent_kind = "code"
    else:
        agent_kind = "generic"
    stage = req.stage or "需求整理"
    if template == db.TEMPLATE_SOFTWARE and stage not in db.SOFTWARE_STAGES:
        raise HTTPException(400, f"非法进程状态：{stage}")
    task = db.create_task(room_id, req.summary, {}, agent_kind=agent_kind,
                          template=template, stage=stage, requirement=req.requirement)
    events.publish_business_notify(room_id, "task_created",
                                   {"taskId": task["task_id"], "status": task["status"]})
    return task


@router.put("/tasks/{task_id}")
def update_task(task_id: str, req: UpdateTaskReq,
                x_member_id: str = Header(default="")):
    """更新任务卡（仅主持人）：推进进程状态 / 修订需求 / 改摘要。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)
    if req.stage is not None:
        if task.get("template") == db.TEMPLATE_SOFTWARE and req.stage not in db.SOFTWARE_STAGES:
            raise HTTPException(400, f"非法进程状态：{req.stage}")
        db.set_task_stage(task_id, req.stage)
    if req.requirement is not None:
        db.set_task_requirement(task_id, req.requirement)
    if req.summary is not None:
        db.set_task_summary(task_id, req.summary)
    memory_extractor.extract_and_snapshot(task["room_id"])
    return db.get_task(task_id)


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, x_member_id: str = Header(default="")):
    """标记任务卡完成（仅主持人）。对应软件修订模板的「是否完成」。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)
    if task["status"] in ("finished", "closed", "canceled", "failed"):
        raise HTTPException(409, f"任务已是终态（{task['status']}），无法再次完成")
    db.update_task_status(task_id, "finished")
    db.add_task_log(task_id, task["room_id"], "task_completed", {})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": task_id, "status": "finished"})
    return db.get_task(task_id)


@router.get("/rooms/{room_id}/breakpoints")
def list_breakpoints(room_id: str, status: str | None = None,
                     x_member_id: str = Header(default="")):
    require_member(room_id, x_member_id)
    return db.list_breakpoints(room_id, status)


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, x_member_id: str = Header(default="")):
    """任务详情：结果摘要、完整结果、附件、执行日志、断点列表。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    require_member(task["room_id"], x_member_id)
    return {
        "task": task,
        "logs": db.list_task_logs(task_id),
        "breakpoints": db.list_breakpoints(task["room_id"]),
    }


@router.get("/breakpoints/{breakpoint_id}")
def breakpoint_detail(breakpoint_id: str, x_member_id: str = Header(default="")):
    """断点详情（含 form_schema，供动态表单渲染器渲染）。"""
    bp = db.get_breakpoint(breakpoint_id)
    if not bp:
        raise HTTPException(404, "断点不存在")
    require_member(bp["room_id"], x_member_id)
    return bp


@router.get("/tasks/{task_id}/progress")
def task_progress(task_id: str, x_member_id: str = Header(default="")):
    """任务进度列表（工作AI 回传）：详情=思维链(thinking)、摘要=实际回复(content)，各带时间戳与已读标记。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    require_member(task["room_id"], x_member_id)
    items = db.list_task_progress(task_id)
    unread = sum(1 for i in items if not i.get("is_read"))
    return {"task_id": task_id, "items": items, "unread": unread}


class ProgressReadReq(BaseModel):
    progress_id: str | None = None


@router.post("/tasks/{task_id}/progress/read")
def task_progress_read(task_id: str, req: ProgressReadReq = ProgressReadReq(),
                       x_member_id: str = Header(default="")):
    """标记任务进度为已读（清除未读红点）。不传 progress_id 则清空该任务全部未读。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    require_member(task["room_id"], x_member_id)
    remaining = db.mark_progress_read(task_id, req.progress_id)
    events.publish_business_notify(task["room_id"], "task_progress_read",
                                   {"taskId": task_id, "unread": remaining})
    return {"ok": True, "task_id": task_id, "unread_remaining": remaining}


class AnswerOut(BaseModel):
    form_data: dict
    submit_source: str = "panel_human"


@router.post("/breakpoints/{breakpoint_id}/answer")
async def answer_breakpoint(breakpoint_id: str, req: BreakpointAnswerReq,
                            background: BackgroundTasks,
                            x_member_id: str = Header(default="")):
    """通路A：看板手动填写提交（submit_source=panel_human）。

    幂等：仅 pending 断点可答复，重复提交返回 409。
    """
    bp = db.get_breakpoint(breakpoint_id)
    if not bp:
        raise HTTPException(404, "断点不存在")
    member = require_member(bp["room_id"], x_member_id)

    if bp["status"] != "pending":
        raise HTTPException(409, f"断点已被处理（{bp['status']}），禁止重复提交")

    err = _validate(req.form_data, bp["form_schema"])
    if err:
        raise HTTPException(422, f"表单校验失败：{err}")

    answered = db.answer_breakpoint(breakpoint_id, req.form_data,
                                    submit_source=req.submit_source or "panel_human",
                                    answered_by=member["member_id"])
    if not answered:
        raise HTTPException(409, "断点已被处理，禁止重复提交")

    answer_payload = {
        "task_id": bp["task_id"],
        "breakpoint_id": breakpoint_id,
        "form_data": req.form_data,
        "submit_source": req.submit_source or "panel_human",
    }

    async def resume():
        ok, msg = await dispatcher.send_resume(bp["continue_endpoint"], answer_payload)
        task = db.get_task(bp["task_id"])
        if ok and task and task["status"] == "waiting_human":
            db.update_task_status(bp["task_id"], "running")
            db.add_task_log(bp["task_id"], bp["room_id"], "task_resumed",
                            {"breakpoint_id": breakpoint_id, "via": "panel_human"})
        events.publish_business_notify(bp["room_id"], "task_status_changed",
                                       {"taskId": bp["task_id"],
                                        "status": "running" if ok else (task or {}).get("status")})
        memory_extractor.extract_and_snapshot(bp["room_id"])

    background.add_task(asyncio.run, resume())
    events.publish_business_notify(bp["room_id"], "breakpoint_answered",
                                   {"taskId": bp["task_id"], "breakpointId": breakpoint_id,
                                    "submitSource": "panel_human"})
    return {"ok": True, "breakpoint_id": breakpoint_id,
            "message": "已提交，工作AI代理恢复推演"}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, background: BackgroundTasks,
                      x_member_id: str = Header(default="")):
    """取消任务（仅主持人）。同步取消关联 pending 断点。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)

    if task["status"] in ("finished", "canceled"):
        raise HTTPException(409, f"任务已是终态（{task['status']}）")

    for bp in db.list_breakpoints(task["room_id"]):
        if bp["task_id"] == task_id and bp["status"] == "pending":
            db.cancel_breakpoint(bp["breakpoint_id"])

    prev = task["status"]
    db.update_task_status(task_id, "canceled")
    db.add_task_log(task_id, task["room_id"], "task_canceled", {"from": prev})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": task_id, "status": "canceled"})

    async def notify_agent():
        # 找最近断点的 cancel_endpoint；无断点则跳过（任务可能尚未产生断点）
        for bp in db.list_breakpoints(task["room_id"]):
            if bp["task_id"] == task_id and bp.get("cancel_endpoint"):
                await dispatcher.send_cancel(bp["cancel_endpoint"], task_id, bp["breakpoint_id"])
                break

    background.add_task(asyncio.run, notify_agent())
    memory_extractor.extract_and_snapshot(task["room_id"])
    return {"ok": True, "task_id": task_id, "status": "canceled"}


@router.post("/tasks/{task_id}/close")
async def close_task(task_id: str, x_member_id: str = Header(default="")):
    """结案（仅主持人）。终态只读：禁止再取消/编辑/状态变更。

    适用工作流：会议AI发起任务 → 工作AI推演（含断点 HITL）→ 完成 →
    主持人核实结果后结案，任务卡进入只读归档状态。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)

    if task["status"] in ("canceled", "failed", "closed"):
        raise HTTPException(409, f"任务已是终态（{task['status']}），无法结案")
    if task["status"] != "finished":
        raise HTTPException(409, f"仅「已完成」任务可结案，当前状态：{task['status']}")

    db.update_task_status(task_id, "closed", closed_by=member["member_id"], closed_at=_now())
    db.add_task_log(task_id, task["room_id"], "task_closed", {"by": member["member_id"]})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": task_id, "status": "closed"})
    return {"ok": True, "task_id": task_id, "status": "closed"}


def _now() -> float:
    import time
    return time.time()


class RollbackReq(BaseModel):
    by: str | None = None
    note: str | None = None


@router.post("/tasks/{task_id}/rollback")
async def rollback_task(task_id: str, req: RollbackReq = RollbackReq(),
                        x_member_id: str = Header(default="")):
    """回滚代码修订（仅主持人；仅限代码修订底座且已完成的任务）。

    调度服务转发回滚指令到代码修订 Agent（8312），由其执行 git reset / 文件快照还原，
    并回传 task_rollback 事件标记任务为已回滚。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)

    if task.get("agent_kind") != "code":
        raise HTTPException(400, "该任务非代码修订任务，无回滚能力")
    if task.get("is_rolled_back"):
        raise HTTPException(409, "该修订已回滚")
    if task["status"] != "finished":
        raise HTTPException(409, f"仅「已完成」任务可回滚，当前状态：{task['status']}")

    url = config.CODE_AGENT_ROLLBACK_ENDPOINT.format(task_id=task_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json={"by": req.by or member["member_id"],
                                               "note": req.note or "人工回滚"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"无法联系代码修订 Agent：{exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"代码修订 Agent 回滚失败：{resp.text[:200]}")
    db.add_task_log(task_id, task["room_id"], "task_rollback_requested",
                    {"by": req.by or member["member_id"]})
    return {"ok": True, "task_id": task_id, "agent_response": resp.json()}


class PinReq(BaseModel):
    pinned: bool = True


@router.post("/tasks/{task_id}/pin")
async def pin_task(task_id: str, req: PinReq = PinReq(),
                   x_member_id: str = Header(default="")):
    """人工收藏/取消收藏某个关键版本（代码修订任务的 git 版本，仅主持人）。

    收藏后该版本永不被代码修订 Agent 的快照清理策略删除（保留基线以供回滚）。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    member = require_member(task["room_id"], x_member_id)
    require_host(member)
    db.set_task_pinned(task_id, req.pinned)
    # 同步到代码修订 Agent 侧的仓库快照清单（best-effort）
    url = config.CODE_AGENT_BASE_URL.rstrip("/") + f"/agent/tasks/{task_id}/pin"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(url, json={"pinned": req.pinned})
    except Exception as exc:  # noqa: BLE001
        print(f"[tasks] 通知代码修订 Agent 置顶失败: {exc}")
    return {"ok": True, "task_id": task_id, "pinned": req.pinned}


@router.post("/internal/tasks/{task_id}/pin")
async def internal_pin_task(task_id: str, req: PinReq = PinReq()):
    """内部面：代码修订 Agent 直接置顶任务版本（无需鉴权，仅本机可达）。"""
    db.set_task_pinned(task_id, req.pinned)
    return {"ok": True, "task_id": task_id, "pinned": req.pinned}
