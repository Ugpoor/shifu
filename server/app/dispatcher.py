"""Webhook 下发器（调度服务 → 工作AI代理）。

非功能需求：失败重试 3 次，超时标记失败；task_cancel 发送至 cancel_endpoint。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from . import config, db


async def post_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """带重试的 Webhook POST。返回 (是否成功, 说明)。"""
    last_err = ""
    for attempt in range(1, config.WEBHOOK_RETRY_TIMES + 1):
        try:
            async with httpx.AsyncClient(timeout=config.WEBHOOK_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
                if 200 <= resp.status_code < 300:
                    return True, f"ok(attempt={attempt})"
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.3 * attempt)
    return False, last_err


async def dispatch_task(task: dict) -> tuple[bool, str]:
    """事件1：下发任务给工作AI代理（携带完整上下文包，杜绝执行偏差）。"""
    # 把话题主题（目录式一级目录）注入上下文包，使工作AI 能按主题聚类/推理
    context_package = task["context_package"]
    try:
        pkg = json.loads(context_package) if isinstance(context_package, str) else (context_package or {})
    except (TypeError, ValueError):
        pkg = {}
    pkg = dict(pkg or {})
    room = db.get_room(task["room_id"]) or {}
    if room.get("theme"):
        pkg["theme"] = room["theme"]
    if room.get("tags"):
        pkg["tags"] = room["tags"]
    context_package = json.dumps(pkg, ensure_ascii=False)
    payload = {
        "task_id": task["task_id"],
        "room_id": task["room_id"],
        "type": "normal_task",
        "context_package": context_package,
    }
    # 按母体对话主题路由：命中代码修订主题则发往代码修订 Agent（真实 CLI 调起 WorkBuddy agent）
    endpoint = config.HEAVY_AGENT_TASK_ENDPOINT
    routed = "default"
    if config.CODE_AGENT_THEME and room.get("theme") == config.CODE_AGENT_THEME:
        endpoint = config.CODE_AGENT_TASK_ENDPOINT
        routed = "code_agent"
    ok, msg = await post_webhook(endpoint, payload)
    db.add_task_log(task["task_id"], task["room_id"],
                    "task_dispatched" if ok else "task_dispatch_failed",
                    {"detail": msg, "routed": routed})
    # 路由命中代码修订底座时，标记任务类型，供面板展示版本/回滚控件
    if ok and routed == "code_agent":
        db.set_task_agent_kind(task["task_id"], "code")
    return ok, msg


async def send_resume(continue_endpoint: str, answer_payload: dict) -> tuple[bool, str]:
    """事件3：断点答复转发至工作AI代理 resume 接口，恢复执行。"""
    return await post_webhook(continue_endpoint, answer_payload)


async def send_cancel(cancel_endpoint: str, task_id: str, breakpoint_id: str | None) -> tuple[bool, str]:
    """task_cancel：取消任务指令，发送至 cancel_endpoint。"""
    return await post_webhook(cancel_endpoint, {"task_id": task_id, "breakpoint_id": breakpoint_id})
