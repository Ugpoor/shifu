"""本地重度Agent（可替换算力底座）· MVP 模拟器。

严格实现双向 Webhook 协议（与任意Agent底座可对接）：
1. 接收任务下发（POST /agent/tasks，携带完整 context_package）；
2. 模拟多步骤推演，中途触发 human_breakpoint（携带完整 form_schema + resume/cancel 端点）；
3. 收到断点答复（resume）后挂起续跑，最终回传 task_finish / task_error；
4. 支持任务取消（cancel）、超时终止。

内部推理草稿、临时日志不属于产品资产，仅本地留存。
启动：python run.py   （默认 127.0.0.1:8310）
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

SCHEDULER_EVENTS_URL = os.environ.get(
    "SCHEDULER_EVENTS_URL", "http://127.0.0.1:8300/internal/webhooks/agent-events")
SELF_BASE_URL = os.environ.get("HEAVY_AGENT_BASE_URL", "http://127.0.0.1:8310")

app = FastAPI(title="重度Agent（可替换底座）· MVP模拟器", version="0.1.0")

# task_id -> 执行状态（内存态，属于Agent内部数据，不进入产品资产）
_tasks: dict[str, dict] = {}
_asyncio_tasks: dict[str, asyncio.Task] = {}


class TaskIn(BaseModel):
    task_id: str
    room_id: str
    type: str = "normal_task"
    context_package: dict


async def _post_event(payload: dict) -> None:
    """事件回调（带轻量重试）。"""
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(SCHEDULER_EVENTS_URL, json=payload)
                if 200 <= resp.status_code < 300:
                    return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3 * attempt)


async def _post_progress(task_id: str, thinking: str | None = None,
                         content: str | None = None, step: str | None = None) -> None:
    """事件4：推演过程进度回传（思维链 thinking + 实际回复 content）。"""
    if not thinking and not content:
        return
    await _post_event({"event_type": "task_progress", "task_id": task_id,
                       "thinking": thinking, "content": content, "step": step})


async def _run_pipeline(task_id: str, room_id: str, context_package: dict) -> None:
    """模拟推演流水线：运行 → 断点挂起 → （等待resume）→ 续跑 → 完成。"""
    state = _tasks[task_id]
    try:
        await asyncio.sleep(2.0)  # 阶段1：模拟RAG检索与初步测算
        if state["status"] != "running":
            return
        # 进度回传①：思维链 + 实际回复（详情/摘要分区）
        await _post_progress(
            task_id,
            thinking="阶段1 检索行业数据库与历史案例，抽取基准假设（WACC≈9.2%、增长率≈6%）。"
                     "识别到现金流在乐观/基准/下行三档假设下差异显著，需结构化拆解。",
            content="已完成 RAG 检索与初步测算，获得基准参数集，待进入情景拆解。",
            step="阶段1·检索测算")

        # 阶段2：抛出人工断点（PRD 事件2 报文，一字不差）
        state["status"] = "waiting_human"
        state["breakpoint_id"] = f"bp-{uuid.uuid4().hex[:8]}"
        await _post_progress(
            task_id,
            thinking="情景假设存在分叉，自动判定会导致重大偏差；按协议需挂起并请求专家裁决。",
            content="分支假设无法自动判定，进入人工确认（断点）。",
            step="阶段2·人工断点")
        await _post_event({
            "event_type": "human_breakpoint",
            "task_id": task_id,
            "breakpoint_id": state["breakpoint_id"],
            "reason": "分支假设无法自动判定，需要专家选择",
            "form_schema": {
                "title": "现金流测算情景选择",
                "description": "不同假设下IRR差异较大，请选定基准",
                "fields": [
                    {"field_key": "scenario", "label": "情景选择", "type": "radio",
                     "required": True,
                     "options": [{"value": "base", "label": "基准情景"},
                                 {"value": "bear", "label": "下行保守情景"},
                                 {"value": "bull", "label": "乐观弹性情景"}]},
                    {"field_key": "note", "label": "备注说明", "type": "text",
                     "required": False, "placeholder": "写下选择理由"},
                ],
            },
            "meta": {
                "continue_endpoint": f"{SELF_BASE_URL}/agent/tasks/{task_id}/resume",
                "cancel_endpoint": f"{SELF_BASE_URL}/agent/tasks/{task_id}/cancel",
            },
        })

        # 阶段3：挂起，等待断点答复（调度服务回传后由 resume 接口唤醒）
        state["resume_event"] = asyncio.Event()
        try:
            await asyncio.wait_for(state["resume_event"].wait(), timeout=600)
        except asyncio.TimeoutError:
            state["status"] = "failed"
            await _post_event({"event_type": "task_error", "task_id": task_id,
                               "error_message": "断点等待超时（600s），任务终止"})
            return
        if state["status"] == "canceled":
            return

        # 阶段4：续跑（使用答复的情景假设继续测算）
        scenario = (state["answer"] or {}).get("form_data", {}).get("scenario", "base")
        await asyncio.sleep(1.5)
        if state["status"] == "canceled":
            return
        state["status"] = "finished"
        irr = {"base": "12.6%", "bear": "7.8%", "bull": "18.3%"}.get(scenario, "12.6%")
        # 进度回传②：采纳情景假设后的重算思维链 + 结论摘要
        await _post_progress(
            task_id,
            thinking=f"已采纳「{scenario}」情景假设，重算 5 年现金流：初期投入分摊、"
                     f"经营净流入折现、回收期测算；并对关键变量做±20%敏感性扫描。",
            content=f"基于 {scenario} 情景完成测算：IRR={irr}，回收期 3.8 年，敏感性分析已生成。",
            step="阶段4·情景测算")
        await _post_event({
            "event_type": "task_finish",
            "task_id": task_id,
            "summary": f"IRR测算完成（{scenario}情景）",
            "result": (f"基于{scenario}情景假设完成5年现金流测算：IRR={irr}，"
                       f"回收期3.8年，敏感性分析见附件。"),
            "attachments": [{"name": "IRR测算表.xlsx", "url": f"{SELF_BASE_URL}/agent/artifacts/{task_id}/irr.xlsx"},
                            {"name": "敏感性分析.pdf", "url": f"{SELF_BASE_URL}/agent/artifacts/{task_id}/sensitivity.pdf"}],
        })
    except asyncio.CancelledError:
        pass  # 取消由 cancel 接口统一处理
    except Exception as exc:  # noqa: BLE001
        state["status"] = "failed"
        await _post_event({"event_type": "task_error", "task_id": task_id,
                           "error_message": f"{type(exc).__name__}: {exc}"})


@app.post("/agent/tasks")
async def receive_task(req: TaskIn):
    """事件1：接收任务下发，启动异步推演。"""
    if req.task_id in _tasks and _tasks[req.task_id]["status"] not in ("finished", "failed", "canceled"):
        return {"ok": True, "ignored": "duplicate"}  # 幂等
    _tasks[req.task_id] = {"status": "running", "room_id": req.room_id,
                           "context_package": req.context_package,
                           "breakpoint_id": None, "answer": None,
                           "internal_draft": [],  # 内部推理草稿（不进入产品资产）
                           "created_at": time.time()}
    _asyncio_tasks[req.task_id] = asyncio.create_task(
        _run_pipeline(req.task_id, req.room_id, req.context_package))
    return {"ok": True, "task_id": req.task_id, "status": "running"}


class ResumeIn(BaseModel):
    task_id: str
    breakpoint_id: str | None = None
    form_data: dict = {}
    submit_source: str = "panel_human"


@app.post("/agent/tasks/{task_id}/resume")
async def resume_task(task_id: str, req: ResumeIn):
    """事件3：断点答复 → 唤醒挂起上下文，恢复推演。"""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if state["status"] != "waiting_human":
        return {"ok": False, "message": f"当前状态 {state['status']}，无法恢复"}
    if state.get("breakpoint_id") and req.breakpoint_id and req.breakpoint_id != state["breakpoint_id"]:
        return {"ok": False, "message": "breakpoint_id 不匹配"}
    state["answer"] = req.model_dump()
    state["status"] = "running"
    state["internal_draft"].append({"resume": req.model_dump()})
    ev: asyncio.Event = state.get("resume_event")
    if ev:
        ev.set()
    return {"ok": True, "message": "已恢复推演"}


@app.post("/agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: dict | None = None):
    """task_cancel：取消任务（含断点挂起中的任务）。"""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if state["status"] in ("finished", "canceled", "failed"):
        return {"ok": True, "ignored": f"已是终态 {state['status']}"}
    state["status"] = "canceled"
    t = _asyncio_tasks.pop(task_id, None)
    if t:
        t.cancel()
    ev = state.get("resume_event")
    if ev:
        ev.set()
    return {"ok": True, "task_id": task_id, "status": "canceled"}


@app.get("/agent/tasks/{task_id}")
def task_state(task_id: str):
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    return state


@app.get("/health")
def health():
    return {"status": "ok", "service": "heavy-agent", "running": len(
        [1 for s in _tasks.values() if s["status"] in ("running", "waiting_human")])}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8310")))
