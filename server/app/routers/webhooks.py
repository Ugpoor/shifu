"""Webhook 事件网关：接收工作AI代理回调事件（事件2 + 配套事件）。

支持 human_breakpoint / task_finish / task_error，全部幂等处理。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from .. import db, events, memory_extractor
from ..models import (HeavyBreakpointEvent, HeavyErrorEvent, HeavyFinishEvent,
                     HeavyProgressEvent, HeavyRollbackEvent)

router = APIRouter(prefix="/internal/webhooks", tags=["webhooks"])


@router.post("/agent-events")
async def agent_events(payload: dict):
    """工作AI代理 → 调度服务 事件回调统一入口。"""
    event_type = payload.get("event_type")
    try:
        if event_type == "human_breakpoint":
            return await _on_breakpoint(HeavyBreakpointEvent(**payload))
        if event_type == "task_finish":
            return await _on_finish(HeavyFinishEvent(**payload))
        if event_type == "task_error":
            return await _on_error(HeavyErrorEvent(**payload))
        if event_type == "task_progress":
            return await _on_progress(HeavyProgressEvent(**payload))
        if event_type == "task_rollback":
            return await _on_rollback(HeavyRollbackEvent(**payload))
        raise HTTPException(400, f"未知事件类型: {event_type}")
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc


async def _on_breakpoint(ev: HeavyBreakpointEvent) -> dict:
    task = db.get_task(ev.task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {ev.task_id}")
    if task["status"] in ("finished", "canceled", "failed", "closed"):
        return {"ok": True, "ignored": f"任务已是终态 {task['status']}"}  # 幂等

    bp = db.create_breakpoint(
        task_id=ev.task_id, room_id=task["room_id"], reason=ev.reason or "",
        form_schema=ev.form_schema.model_dump(),
        continue_endpoint=ev.meta.continue_endpoint,
        cancel_endpoint=ev.meta.cancel_endpoint,
        breakpoint_id=ev.breakpoint_id,
    )
    if bp is None:
        return {"ok": True, "ignored": "duplicate breakpoint"}  # 幂等
    db.update_task_status(ev.task_id, "waiting_human")
    # 轻量通知：IM 只传 id 信号，客户端收到后主动拉取完整表单数据
    events.publish_business_notify(task["room_id"], "human_breakpoint",
                                   {"taskId": ev.task_id, "breakpointId": bp["breakpoint_id"]})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": ev.task_id, "status": "waiting_human"})
    return {"ok": True, "breakpoint_id": bp["breakpoint_id"]}


async def _on_finish(ev: HeavyFinishEvent) -> dict:
    task = db.get_task(ev.task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {ev.task_id}")
    if task["status"] in ("finished", "closed"):
        return {"ok": True, "ignored": "duplicate finish"}  # 幂等
    if task["status"] in ("canceled", "failed"):
        return {"ok": True, "ignored": f"任务已是终态 {task['status']}"}

    # 收尾：取消仍挂着的断点
    for bp in db.list_breakpoints(task["room_id"]):
        if bp["task_id"] == ev.task_id and bp["status"] == "pending":
            db.cancel_breakpoint(bp["breakpoint_id"])

    db.update_task_status(ev.task_id, "finished",
                          result_summary=ev.summary,
                          result_full=ev.result,
                          attachments=ev.attachments or [],
                          result_meta=ev.meta if ev.meta else None)
    db.add_task_log(ev.task_id, task["room_id"], "task_finished",
                    {"summary": ev.summary, "version_meta": ev.meta})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": ev.task_id, "status": "finished"})
    # 摘要推送聊天框：由会议AI代理（meeting-ai）转述工作AI代理的推演结果。
    # 工作AI代理仅经 Webhook 单通道与会议AI代理通讯，不以独立身份出现在用户会话。
    db.add_message(task["room_id"], "meeting-ai", "agent",
                   f"✅ 工作AI代理已完成推演：{ev.summary}（完整结果见任务看板）",
                   msg_type="agent_summary",
                   meta={"task_id": ev.task_id, "kind": "task_finish",
                         "via": "work-ai", "relay": "meeting-ai"})
    events.publish_business_notify(task["room_id"], "message_new", {"taskId": ev.task_id})
    memory_extractor.extract_and_snapshot(task["room_id"])
    return {"ok": True}


async def _on_error(ev: HeavyErrorEvent) -> dict:
    task = db.get_task(ev.task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {ev.task_id}")
    if task["status"] in ("finished", "canceled", "failed", "closed"):
        return {"ok": True, "ignored": f"任务已是终态 {task['status']}"}
    db.update_task_status(ev.task_id, "failed", error_message=ev.error_message)
    db.add_task_log(ev.task_id, task["room_id"], "task_error",
                    {"error_message": ev.error_message})
    events.publish_business_notify(task["room_id"], "task_status_changed",
                                   {"taskId": ev.task_id, "status": "failed"})
    return {"ok": True}


async def _on_progress(ev: HeavyProgressEvent) -> dict:
    """事件4：工作AI 推演过程进度回传。

    把思维链（thinking）与实际回复（content）分别落库为两条进度记录（默认未读），
    供面板「详情（思维链）/ 摘要（实际回复）」分区展示与未读红点提示。
    """
    task = db.get_task(ev.task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {ev.task_id}")
    if not ev.thinking and not ev.content:
        return {"ok": True, "ignored": "empty progress"}
    added = 0
    if ev.thinking:
        db.add_task_progress(ev.task_id, task["room_id"], "thinking", ev.thinking)
        added += 1
    if ev.content:
        db.add_task_progress(ev.task_id, task["room_id"], "content", ev.content)
        added += 1
    if added:
        events.publish_business_notify(task["room_id"], "task_progress",
                                       {"taskId": ev.task_id, "unread": added,
                                        "step": ev.step or ""})
    return {"ok": True, "added": added}


async def _on_rollback(ev: HeavyRollbackEvent) -> dict:
    """代码修订回滚事件：标记任务已回滚，并记录事实日志（非状态机迁移）。"""
    import time as _time
    task = db.get_task(ev.task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {ev.task_id}")
    db.mark_task_rolled_back(ev.task_id,
                            {"rolled_back_by": ev.by, "rolled_back_note": ev.note})
    db.add_task_log(ev.task_id, task["room_id"], "task_rolled_back",
                    {"by": ev.by, "note": ev.note, "at": _time.time()})
    events.publish_business_notify(task["room_id"], "task_rolled_back",
                                   {"taskId": ev.task_id, "by": ev.by})
    return {"ok": True}
