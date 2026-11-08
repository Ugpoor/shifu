"""客户端业务事件广播（MVP 通信通道）。

技术文档规定 IM 只承载轻量业务通知（eventType/taskId/breakpointId/roomId），
完整数据由客户端主动 HTTP 拉取。MVP 阶段无私有化 OpenIM Server 可依赖，
本模块用「每房间一条 SSE 事件流」承载与 OpenIM 自定义消息完全同构的轻量通知，
字段结构与技术文档 OpenIM 自定义消息规范一致，后续接入 OpenIM 时仅替换传输实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from sse_starlette.sse import ServerSentEvent

from . import db

# room_id -> [asyncio.Queue, ...]
_subscribers: dict[str, list[asyncio.Queue]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def publish_business_notify(room_id: str, event_type: str, payload: dict) -> None:
    """轻量业务通知（与 OpenIM 自定义消息同构的载荷）。

    严禁放大体积 JSON：只放 id 类轻量信号，完整数据客户端主动拉取。
    """
    light = {"eventType": event_type, **payload, "roomId": room_id}
    db.log_event(room_id, f"notify:{event_type}", light)
    if _loop is None:
        return
    for q in list(_subscribers.get(room_id, [])):
        _loop.call_soon_threadsafe(q.put_nowait, light)


async def subscribe(room_id: str) -> AsyncGenerator[ServerSentEvent, None]:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(room_id, []).append(q)
    try:
        yield ServerSentEvent(data=json.dumps({"eventType": "connected", "roomId": room_id},
                                              ensure_ascii=False))
        while True:
            event = await q.get()
            yield ServerSentEvent(data=json.dumps(event, ensure_ascii=False))
    finally:
        _subscribers[room_id].remove(q)
