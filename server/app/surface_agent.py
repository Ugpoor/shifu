"""会议AI代理 会话服务：SSE 流式对话编排。

- 角色识别：主持人(host)/专家(expert)；主持人静默开关生效时，仅主持人发言触发会议AI代理；
- 双核心工具编排：submit_heavy_task(→工作AI代理，经 Webhook 单通道) / fill_breakpoint_form；
- @ 操作符：@会议AI代理 强制其在此时发言（聚焦被引用消息，仍保留全量上下文记忆）；
  工作AI代理仅经 Webhook 与会议AI代理单通道通讯（用户不可直接 @，由会议AI代理在共识达成后通过工具调用下发任务）；
- 流式打字机输出；对话全量持久化；每次应答后触发结构化记忆抽取；
- LLM 可插拔：配置 LLM_API_KEY 走 OpenAI 兼容接口（含 function call），
  默认内置确定性 Mock 引擎（零依赖可演示、可测试）。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from . import config, db, events, functions, memory_extractor, model_tier

DECISION_KEYWORDS = ("选", "选择", "选定", "决定", "确认", "同意", "采用", "走", "按", "方案", "情景")
TASK_KEYWORDS = ("提交任务", "下发任务", "开始推演", "启动推演", "提交推演", "开始测算")


def _breakpoint_context_text(room_id: str) -> str:
    """当前房间待处理断点的上下文（给 Mock 引擎与 LLM system prompt 用）。"""
    pending = db.list_breakpoints(room_id, status="pending")
    if not pending:
        return ""
    lines = ["【当前待确认断点】"]
    for bp in pending:
        fs = bp["form_schema"] or {}
        fields_desc = []
        for f in fs.get("fields", []):
            opt = "/".join(o["label"] for o in (f.get("options") or []))
            fields_desc.append(f"{f['label']}({f['type']}{':' + opt if opt else ''})")
        lines.append(f"- 断点 {bp['breakpoint_id']}「{fs.get('title','')}」："
                     f"{bp.get('reason','') or ''} 字段：{'；'.join(fields_desc)}")
    return "\n".join(lines)


def _round_header(round_ctx: dict | None) -> str:
    """罗伯特议事法则模式：一轮结束时的汇总抬头。"""
    if not round_ctx:
        return ""
    lines = [f"【第{round_ctx['round_no']}轮 · 全员发言完毕，统一回应】"]
    for st in round_ctx.get("statements", []):
        brief = st["content"]
        if len(brief) > 42:
            brief = brief[:42] + "…"
        lines.append(f"· {st['name']}：{brief}")
    return "\n".join(lines) + "\n\n"


async def _stream_mock_reply(room_id: str, statement: str, is_host: bool,
                             round_ctx: dict | None = None,
                             focus_text: str | None = None,
                             focus_author: str | None = None) -> AsyncGenerator[dict, None]:
    """Mock 会议AI代理：意图识别 → 工具调用 → 流式确认/回复。"""
    bp_ctx = _breakpoint_context_text(room_id)
    pending = db.list_breakpoints(room_id, status="pending")
    header = _round_header(round_ctx)

    # 意图1：决策类表述 + 存在待确认断点 → fill_breakpoint_form
    if pending and any(k in statement for k in DECISION_KEYWORDS):
        yield {"type": "tool_call", "name": "fill_breakpoint_form",
               "args": {"room_id": room_id, "user_statement": statement}}
        result = await functions.fill_breakpoint_form({"room_id": room_id, "user_statement": statement})
        if result.ok:
            text = (f"{header}✅ 已自动填写断点表单并提交（来源 chat_agent）。\n"
                f"断点：{result.form_data}\n工作AI代理 已恢复推演。")
        else:
            text = f"{header}⚠️ {result.message}"
        async for ev in _stream_text(text):
            yield ev
        yield {"type": "final", "content": text, "tool": "fill_breakpoint_form",
               "result": result.model_dump()}
        return

    # 意图2：主持人要求下发任务 → submit_heavy_task
    if is_host and any(k in statement for k in TASK_KEYWORDS):
        yield {"type": "tool_call", "name": "submit_heavy_task", "args": {"room_id": room_id}}
        result = await functions.submit_heavy_task({
            "room_id": room_id,
            "summary": "双人协同对齐后的重型推演任务",
            "constraints": [],
            "discussion_history": db.recent_history_text(room_id),
            "deliver_requirement": statement,
        })
        text = (f"{header}🚀 {result['message']}\n可在任务看板跟踪进度。"
                if result["ok"] else f"{header}⚠️ {result['message']}")
        async for ev in _stream_text(text):
            yield ev
        yield {"type": "final", "content": text, "tool": "submit_heavy_task", "result": result}
        return

    # 意图3：普通澄清对话
    hints = []
    if pending:
        hints.append("检测到任务看板有待人工确认的断点（红点），您可以直接在这里用自然语言表达选择，"
                     "例如「选保守情景，备注优先控制下行风险」，我会帮您自动填表提交；也可以前往任务看板手动填写。")
    elif not db.list_tasks(room_id):
        hints.append("需求对齐后，您（主持人）可以说「提交任务，开始推演」，我会把共识打包下发给工作AI代理。")
    body = ("收到，我们一起对齐关键信息。" + (" ".join(hints) if hints else
            "请继续补充约束与目标，我会协助沉淀共识并抽取结构化记忆。"))
    if round_ctx:
        body = ("本轮各方立场已归集，共识与分歧我记下了。" + (" ".join(hints) if hints else
                "可以继续下一轮议题。"))
    text = header + body
    async for ev in _stream_text(text):
        yield ev
    yield {"type": "final", "content": text, "tool": None}


async def _stream_text(text: str, chunk: int = 2) -> AsyncGenerator[dict, None]:
    for i in range(0, len(text), chunk):
        yield {"type": "token", "content": text[i:i + chunk]}
        await asyncio.sleep(config.SSE_TOKEN_INTERVAL)


async def _stream_llm_reply(room_id: str, statement: str, history: list[dict],
                            round_ctx: dict | None = None,
                            focus_text: str | None = None,
                            focus_author: str | None = None) -> AsyncGenerator[dict, None]:
    """OpenAI 兼容 LLM（含 function call）路径。"""
    import httpx

    tools = [
        {"type": "function", "function": {
            "name": "fill_breakpoint_form",
            "description": "对话驱动填表：把用户自然语言决策映射为断点表单数据并提交",
            "parameters": {"type": "object", "properties": {
                "room_id": {"type": "string"},
                "user_statement": {"type": "string"}},
                "required": ["room_id", "user_statement"]}}},
        {"type": "function", "function": {
            "name": "submit_heavy_task",
            "description": "把房间对齐后的共识打包下发工作AI代理异步推演",
            "parameters": {"type": "object", "properties": {
                "summary": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "deliver_requirement": {"type": "string"}},
                "required": ["summary"]}}},
        {"type": "function", "function": {
            "name": "append_task_note",
            "description": "直接把沟通记录/结论写入任务面板（任务看板）：进展记录或结论摘要。已结案的只读任务不可写入。",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string", "description": "目标任务卡 id"},
                "note": {"type": "string", "description": "要写入的沟通记录或结论文本"},
                "kind": {"type": "string", "enum": ["progress", "summary"],
                         "description": "progress=进展记录；summary=结论摘要（覆盖 result_summary）"}},
                "required": ["task_id", "note"]}}},
    ]
    sys_prompt = ("你是双人咨询房间里的会议AI代理，负责澄清需求、沉淀共识。"
                  + _breakpoint_context_text(room_id))
    if focus_text:
        sys_prompt += (f"\n【用户定向 @会议AI代理 的焦点问题】"
                       f"{('（来自「' + focus_author + '」的发言）') if focus_author else ''}"
                       f"请在下方完整对话上下文的基础上，重点、精准地回答这一条：\n"
                       f"「{focus_text}」\n"
                       f"注意：你的回答应主要解决上面这条被 @ 引用的内容，"
                       f"同时可结合全文上下文，不要忽略与之相关的其它信息。")
    if round_ctx:
        statements = "\n".join(f"- {s['name']}：{s['content']}"
                               for s in round_ctx.get("statements", []))
        sys_prompt += (f"\n【罗伯特议事法则】第{round_ctx['round_no']}轮全员发言已结束，"
                       f"请先归集各方立场与分歧，再给出统一回应：\n{statements}")
    msgs = [{"role": "system", "content": sys_prompt}]
    for m in history[-20:]:
        if m["role"] == "agent":
            msgs.append({"role": "assistant", "content": m["content"]})
        else:
            msgs.append({"role": "user", "content": f"{m['member_id']}: {m['content']}"})

    async with httpx.AsyncClient(timeout=180) as client:
        model_id, api_key, base_url = model_tier.active_llm()
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model_id, "messages": msgs, "tools": tools})
        choice = resp.json()["choices"][0]["message"]
        if choice.get("tool_calls"):
            called_tools = []
            # 首轮（非流式）也可能携带推理内容
            reasoning0 = choice.get("reasoning_content") or choice.get("reasoning") or ""
            if reasoning0:
                yield {"type": "thinking", "content": reasoning0}
            for tc in choice["tool_calls"]:
                fn, args = tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")
                yield {"type": "tool_call", "name": fn, "args": args}
                called_tools.append(fn)
                if fn == "fill_breakpoint_form":
                    # 始终以当前会话房间为准，不信任 LLM 传入的 room_id（安全 + 正确）
                    args["room_id"] = room_id
                    args.setdefault("user_statement", statement)
                    r = await functions.fill_breakpoint_form(args)
                    text = ("✅ 已自动填写断点表单并提交。" if r.ok else f"⚠️ {r.message}")
                elif fn == "submit_heavy_task":
                    args.setdefault("summary", "双人协同对齐后的重型推演任务")
                    args["room_id"] = room_id
                    r = await functions.submit_heavy_task(args)
                    text = r["message"]
                elif fn == "append_task_note":
                    # 始终以当前会话房间为准，不信任 LLM 传入的 room_id
                    args["room_id"] = room_id
                    r = await functions.append_task_note(args)
                    text = r["message"]
                else:
                    text = f"未知工具 {fn}"
                msgs.append(choice)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model_id, "messages": msgs, "stream": True})
            collected = ""
            reasoning = ""
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                delta = json.loads(line[6:])["choices"][0].get("delta", {})
                piece = delta.get("content") or ""
                if piece:
                    collected += piece
                    yield {"type": "token", "content": piece}
                # 推理思考过程（Doubao-Seed / DeepSeek 类 thinking 字段）
                rpiece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if rpiece:
                    reasoning += rpiece
                    yield {"type": "thinking", "content": rpiece}
            # 真实记录本次应答触发了哪个工具（用于消息留痕 / 资产溯源）
            yield {"type": "final", "content": collected,
                   "tool": "+".join(called_tools) if called_tools else None,
                   "reasoning": reasoning}
            return
        text = choice.get("content") or ""
        async for ev in _stream_text(text):
            yield ev
        yield {"type": "final", "content": text, "tool": None}


async def chat_stream(room_id: str, member_id: str, content: str,
                      reply_to: str | None = None, mentions: list | None = None,
                      topics: list | None = None) -> AsyncGenerator[dict, None]:
    """处理一条用户消息，产出 SSE 事件流。

    会议发言规则（flow_settings.response_mode）决定谁发言才触发会议AI代理：
    - round_robin：轮询所有人
    - host_only：只回应主持人
    - guest_only：只回应嘉宾
    - robert_rules：罗伯特议事法则（一轮每人一次，全员说完 Agent 统一回应，
      撤回窗口内可撤回重说）
    @ 操作符：@会议AI代理 强制其在此时发言（聚焦被引用消息，仍保留全量上下文记忆）；
    工作AI代理仅经 Webhook 与会议AI代理单通道通讯（用户不可直接 @，由会议AI代理在共识达成后通过工具调用下发任务）。
    """
    room = db.get_room(room_id)
    member = db.get_member(room_id, member_id)
    if not room or not member:
        yield {"type": "error", "message": "房间或成员不存在"}
        return

    flow = db.get_flow_settings(room_id)
    mode = flow.get("response_mode", "round_robin")
    recall_minutes = int(flow.get("recall_minutes") or 0)

    user_msg = db.add_message(room_id, member_id, member["role"], content,
                              reply_to=reply_to, mentions=mentions, topics=topics)
    yield {"type": "user_message", "message": user_msg}
    events.publish_business_notify(room_id, "message_new",
                                   {"msgId": user_msg["msg_id"], "memberId": member_id})

    is_host = member["role"] == "host"

    # @ 操作符：仅会议AI代理可被用户 @ 强制介入；工作AI代理只经 Webhook 与会议AI代理
    # 单通道通讯，用户不可直接 @（即使携带 work-ai 提及也会被忽略，不触发下发）。
    mention_agents = [m for m in (mentions or []) if m == "meeting-ai"]
    force = "meeting-ai" in mention_agents   # 覆盖静默/模式/轮次限制

    # 焦点问题：@ 引用回复时，把被引用消息作为重点回答对象（仍保留全量上下文作记忆）
    focus_text = None
    focus_author = None
    if reply_to:
        tgt = db.get_message(reply_to)
        if tgt:
            focus_text = tgt["content"]
            nm = {m["member_id"]: m["name"] for m in db.list_members(room_id)}
            focus_author = nm.get(tgt["member_id"], tgt["member_id"])

    # ① 兼容旧开关：Agent静默模式（等价于「只回应主持人」的历史配置）；@ 强制时覆盖
    if room["agent_silent"] and not is_host and not force:
        yield {"type": "silent_skipped", "reason": "silent",
               "message": "Agent静默模式：专家发言不触发会议AI代理"}
        yield {"type": "done"}
        return

    # ② 会议发言规则；@ 强制时覆盖
    if mode == "host_only" and not is_host and not force:
        yield {"type": "gated", "reason": "host_only",
               "message": "当前会议流程为「只回应主持人」，嘉宾发言已记录，会议AI代理 不打断"}
        yield {"type": "done"}
        return
    if mode == "guest_only" and is_host and not force:
        yield {"type": "gated", "reason": "guest_only",
               "message": "当前会议流程为「只回应嘉宾」，主持人发言已记录，会议AI代理 不打断"}
        yield {"type": "done"}
        return

    round_ctx = None
    if mode == "robert_rules" and not force:
        state = db.robert_register_speech(room_id, member_id, user_msg["msg_id"],
                                          member["name"], recall_minutes * 60)
        procedural = (is_host and flow.get("procedural_bypass", True)
                      and _has_procedural_intent(room_id, content))
        if state["action"] == "blocked":
            if not procedural:
                notice = (f"罗伯特议事法则：您在第{state['round_no']}轮已发言"
                          f"（撤回窗口 {recall_minutes} 分钟已过，不能再撤回或补言）。"
                          f"本轮待发言：{'、'.join(state['pending_names']) or '无'}，"
                          f"请等待本轮结束由 Agent 统一回应。")
                sys_msg = db.add_message(room_id, "system", "system", notice,
                                         msg_type="round_notice",
                                         meta={"round_no": state["round_no"],
                                               "blocked_msg_id": user_msg["msg_id"]})
                yield {"type": "round_blocked", "message": notice, "notice": sys_msg,
                       "round_no": state["round_no"],
                       "pending_names": state["pending_names"]}
                yield {"type": "done"}
                return
        elif state["action"] == "revise" and state.get("recalled_msg_id"):
            db.mark_message_withdrawn(state["recalled_msg_id"])
            yield {"type": "message_withdrawn",
                   "msg_id": state["recalled_msg_id"],
                   "message": f"撤回窗口内重说：已撤回上一条发言，以本条为准（第{state['round_no']}轮）"}
        if state["action"] == "all_spoken":
            round_ctx = {
                "round_no": state["round_no"],
                "statements": db.round_statements(state["spoken"]),
            }
        elif state["action"] != "blocked" and not procedural:
            yield {"type": "round_waiting",
                   "round_no": state["round_no"],
                   "pending_names": state["pending_names"],
                   "message": f"第{state['round_no']}轮已记录您的发言，"
                              f"等待 {'、'.join(state['pending_names'])} 发言后再由 Agent 统一回应"}
            yield {"type": "done"}
            return

    # 注：用户不可直接 @工作AI代理；深层推演任务仅由会议AI代理在共识达成后
    # 通过 submit_heavy_task 工具经 Webhook 单通道下发给工作AI代理。

    history = db.list_messages(room_id)
    final_text, tool_name, tool_result, reasoning = "", None, None, ""
    stream = (_stream_llm_reply(room_id, content, history, round_ctx, focus_text, focus_author)
              if config.LLM_API_KEY
              else _stream_mock_reply(room_id, content, is_host, round_ctx, focus_text, focus_author))
    async for ev in stream:
        if ev["type"] == "final":
            final_text, tool_name = ev["content"], ev.get("tool")
            tool_result = ev.get("result")
            reasoning = ev.get("reasoning", "")
        yield ev

    if final_text:
        meta = {"tool": tool_name} if tool_name else {}
        if reasoning:
            meta["reasoning"] = reasoning
        if round_ctx:
            meta = {**(meta or {}), "round_no": round_ctx["round_no"], "round_summary": True}
        if force:
            meta = {**(meta or {}), "focused": True, "reply_to": reply_to}
        agent_msg = db.add_message(room_id, "meeting-ai", "agent", final_text,
                                   msg_type="agent_confirm" if tool_name else "text",
                                   reply_to=reply_to, meta=meta)
        yield {"type": "agent_message", "message": agent_msg}
        events.publish_business_notify(room_id, "message_new",
                                       {"msgId": agent_msg["msg_id"], "memberId": "meeting-ai"})
        if round_ctx:
            closed = db.close_round(room_id, agent_msg["msg_id"])
            yield {"type": "round_closed", "round_no": closed["round_no"]}
        # 主动触发结构化记忆抽取（PRD：定时/主动触发）
        snap = await memory_extractor.extract_and_snapshot(room_id)
        yield {"type": "memory_snapshot", "version": snap["version"]}
    yield {"type": "done"}


def _has_procedural_intent(room_id: str, statement: str) -> bool:
    """是否是程序性动议：下发重任务 / 断点填表决策。"""
    if any(k in statement for k in TASK_KEYWORDS):
        return True
    if db.list_breakpoints(room_id, status="pending") and any(k in statement for k in DECISION_KEYWORDS):
        return True
    return False
