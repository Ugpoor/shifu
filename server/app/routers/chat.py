"""对话接口：SSE 流式对话（会议AI代理）、历史消息、业务事件订阅。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db, events, surface_agent
from .rooms import require_member

router = APIRouter(tags=["chat"])


class ChatReq(BaseModel):
    member_id: str
    content: str
    reply_to: str | None = None          # @ 引用回复：针对某条消息定点提问
    mentions: list[str] | None = None    # @ 提及：member_id 或 "meeting-ai"/"work-ai" 代理标记
    topics: list[str] | None = None      # # 话题标签：用于结构化记忆归类


@router.post("/rooms/{room_id}/chat")
async def chat(room_id: str, req: ChatReq):
    """发送消息并获得会议AI代理 SSE 流式响应（逐字输出）。

    响应事件序列：user_message → [token]* → [tool_call] → [agent_message] →
    [memory_snapshot] → done / error / silent_skipped。
    """
    async def gen():
        async for ev in surface_agent.chat_stream(
                room_id, req.member_id, req.content,
                reply_to=req.reply_to, mentions=req.mentions, topics=req.topics):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/rooms/{room_id}/messages")
def messages(room_id: str, x_member_id: str = Header(default="")):
    """房间全量对话记录（持久化，含发送人、时间戳）。"""
    require_member(room_id, x_member_id)
    return db.list_messages(room_id)


@router.get("/rooms/{room_id}/events")
async def room_events(room_id: str):
    """轻量业务事件流（与 OpenIM 自定义消息同构的载荷）。

    只传 eventType/taskId/breakpointId 等轻量信号，客户端收到后主动拉取完整数据。
    """
    return StreamingResponse(events.subscribe(room_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
