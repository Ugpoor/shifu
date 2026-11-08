"""会议AI代理 的 Function Call（自研核心，PRD 模块1）。

1. submit_heavy_task：把房间对齐后的共识打包成结构化任务包，Webhook 下发工作AI代理；
2. fill_breakpoint_form：对话驱动填表工具——校验待处理断点 → 读取 form_schema →
   LLM/规则映射为合法 form_data → 校验必填项与枚举 → 提交恢复接口 →
   聊天框输出确认 → 写入资产日志。模糊无法匹配时返回失败并引导去看板手动填写。
3. append_task_note：直接把沟通记录/结论写入任务面板（任务看板），补充任务执行过程中的
   进展、决议或结果摘要；已结案（只读）任务拒绝追加。
"""
from __future__ import annotations

import json

from . import config, db, dispatcher, events, model_tier
from .models import FillResult

VALID_FIELD_TYPES = {"radio", "checkbox", "number", "text", "textarea"}


# ════════════════════════════════════════════════════════════════
# submit_heavy_task
# ════════════════════════════════════════════════════════════════
async def submit_heavy_task(args: dict) -> dict:
    """打包共识并下发任务。返回 {"ok": bool, "task_id": ..., "message": ...}。

    记忆隔离：会议AI 按主题（房间）共享上下文；工作AI 按任务卡隔离——每张任务卡携带
    自己的 context_package（由会议AI生成的任务描述决定互通范围），底座之间彼此独立。
    房间为软件修订类目时，底座默认代码修订 Agent（可回滚）。
    """
    room_id = args["room_id"]
    room = db.get_room(room_id)
    if not room:
        return {"ok": False, "message": f"房间不存在: {room_id}"}

    context_package = {
        "discussion_history": args.get("discussion_history") or db.recent_history_text(room_id),
        "constraints": args.get("constraints") or [],
        "deliver_requirement": args.get("deliver_requirement", ""),
        "summary": args.get("summary", ""),
    }
    # 底座与模板：软件修订类目 → 代码修订 Agent + software_revision 模板
    template = room.get("task_template")
    agent_kind = ("code" if (template == db.TEMPLATE_SOFTWARE
                            or room.get("category") == db.ROOM_CATEGORY_SOFTWARE)
                  else "generic")
    # 软件修订模板：由会议AI（Agent）发起，自动把「需求/交付要求」填入任务卡，
    # 默认阶段=需求整理。人工不再手动建卡（面板卡 = Agent 填写的任务模板）。
    requirement = (args.get("deliver_requirement") or args.get("requirement")
                   or args.get("summary", "")) if agent_kind == "code" else None
    task = db.create_task(room_id, args.get("summary", "重型推演任务"), context_package,
                          agent_kind=agent_kind, template=template,
                          requirement=requirement, stage="需求整理")
    events.publish_business_notify(room_id, "task_created",
                                   {"taskId": task["task_id"], "status": task["status"]})

    ok, msg = await dispatcher.dispatch_task(task)
    if ok:
        db.update_task_status(task["task_id"], "running")
        events.publish_business_notify(room_id, "task_status_changed",
                                       {"taskId": task["task_id"], "status": "running"})
        return {"ok": True, "task_id": task["task_id"],
                "message": f"任务已发布工作AI代理（{task['task_id']}），推演开始。"}
    db.update_task_status(task["task_id"], "failed", error_message=f"下发失败: {msg}")
    events.publish_business_notify(room_id, "task_status_changed",
                                   {"taskId": task["task_id"], "status": "failed"})
    return {"ok": False, "task_id": task["task_id"], "message": f"任务下发失败：{msg}"}


# ════════════════════════════════════════════════════════════════
# fill_breakpoint_form
# ════════════════════════════════════════════════════════════════
# 选项标签中的通用词，剥离后再做关键词匹配，避免「情景」等词造成多选歧义
_GENERIC_LABEL_WORDS = ("情景", "方案", "选项", "模式", "类型")


def _label_keywords(label: str) -> list[str]:
    """选项标签 → 关键词列表：剥离通用词后按2字步长切块。

    「下行保守情景」→ 剥离「情景」→「下行保守」→ [下行, 保守]
    """
    for w in _GENERIC_LABEL_WORDS:
        label = label.replace(w, " ")
    kws = []
    for seg in label.split():
        kws.extend(seg[i:i + 2] for i in range(0, len(seg), 2))
    return kws


def _match_options(statement: str, options: list[dict]) -> list[str]:
    """自然语言 → 选项值匹配：整标签命中优先；否则标签关键词（2字滑窗）全部命中。"""
    hits = []
    # 第一轮：完整标签命中（最强信号）
    for opt in options:
        label = opt.get("label", "")
        if label and label in statement:
            hits.append(opt.get("value", ""))
    if hits:
        return hits
    # 第二轮：标签关键词全部命中（如「下行」「保守」都在表述中出现）
    for opt in options:
        label = opt.get("label", "")
        value = opt.get("value", "")
        kws = _label_keywords(label)
        if kws and all(k in statement for k in kws) and value not in hits:
            hits.append(value)
    return hits


def _map_statement_to_form(statement: str, form_schema: dict) -> tuple[dict | None, str]:
    """规则映射引擎（Mock模式）：把自然语言映射为合法 form_data。

    返回 (form_data | None, 失败原因)。
    """
    form_data: dict = {}
    for field in form_schema.get("fields", []):
        ftype = field["type"]
        key = field["field_key"]
        if ftype in ("radio",) and field.get("options"):
            hits = _match_options(statement, field["options"])
            if len(hits) == 1:
                form_data[key] = hits[0]
            elif len(hits) > 1:
                return None, f"字段「{field['label']}」命中多个选项，表述存在歧义"
            elif field.get("required"):
                return None, f"未能从表述中识别必填字段「{field['label']}」"
        elif ftype == "checkbox" and field.get("options"):
            hits = _match_options(statement, field["options"])
            if hits:
                form_data[key] = hits
            elif field.get("required"):
                return None, f"未能识别必填字段「{field['label']}」"
        elif ftype in ("number", "text", "textarea"):
            # 备注类字段：捕获「备注：xxx」「理由是xxx」；非必填则可缺省
            import re
            m = re.search(rf"{re.escape(field['label'])}[：:为]?(.+)", statement)
            if m:
                form_data[key] = m.group(1).strip()
            elif field.get("required"):
                return None, f"缺少必填字段「{field['label']}」"
    # 枚举合法性校验
    for field in form_schema.get("fields", []):
        v = form_data.get(field["field_key"])
        if v is None or not field.get("options"):
            continue
        legal = {o["value"] for o in field["options"]}
        vs = v if isinstance(v, list) else [v]
        if any(x not in legal for x in vs):
            return None, f"字段「{field['label']}」取值不合法"
    return form_data, ""


async def _map_statement_to_form_llm(statement: str, form_schema: dict) -> tuple[dict | None, str]:
    """LLM 映射引擎（配置 LLM_API_KEY 时启用），失败降级规则引擎。

    注意：必须在事件循环内以异步方式调用，避免阻塞 uvicorn 的事件循环。
    """
    import httpx

    sys_prompt = (
        "你是表单填充器。把用户自然语言映射为表单数据。表单Schema：\n"
        + json.dumps(form_schema, ensure_ascii=False)
        + "\n只输出 JSON 对象（键为 field_key）。无法确定必填项时输出 {\"__fail__\": \"原因\"}"
    )
    try:
        model_id, api_key, base_url = model_tier.active_llm()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model_id, "temperature": 0,
                      "messages": [{"role": "system", "content": sys_prompt},
                                   {"role": "user", "content": statement}],
                      "response_format": {"type": "json_object"}},
            )
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
        if "__fail__" in data:
            return None, data["__fail__"]
        return data, ""
    except Exception:  # noqa: BLE001
        return _map_statement_to_form(statement, form_schema)


def _validate(form_data: dict, form_schema: dict) -> str:
    """必填项与枚举校验，返回错误信息（空串=通过）。"""
    for field in form_schema.get("fields", []):
        v = form_data.get(field["field_key"])
        if field.get("required") and (v is None or v == "" or v == []):
            return f"缺少必填字段「{field['label']}」"
        if v is not None and field.get("options"):
            legal = {o["value"] for o in field["options"]}
            vs = v if isinstance(v, list) else [v]
            if any(x not in legal for x in vs):
                return f"字段「{field['label']}」取值不合法"
    return ""


async def fill_breakpoint_form(args: dict) -> FillResult:
    """对话驱动填表：识别 → 映射 → 校验 → 提交 → 确认 → 资产留痕。"""
    room_id = args["room_id"]
    statement = args["user_statement"]

    # 1. 校验待处理断点
    if args.get("breakpoint_id"):
        bp = db.get_breakpoint(args["breakpoint_id"])
        if not bp or bp["room_id"] != room_id:
            return FillResult(ok=False, message="断点不存在或不属于当前房间")
    else:
        pending = db.list_breakpoints(room_id, status="pending")
        if not pending:
            return FillResult(ok=False, message="当前房间没有待确认断点")
        if len(pending) > 1:
            return FillResult(ok=False,
                              message=f"存在 {len(pending)} 个待确认断点，请指明 breakpoint_id 或前往看板操作")
        bp = pending[0]
    if bp["status"] != "pending":
        return FillResult(ok=False, message="该断点已被处理，请勿重复提交")

    form_schema = bp["form_schema"]

    # 2. 映射 form_data
    if config.LLM_API_KEY:
        form_data, err = await _map_statement_to_form_llm(statement, form_schema)
        if form_data is None:
            return FillResult(ok=False, breakpoint_id=bp["breakpoint_id"], message=err)
    else:
        form_data, err = _map_statement_to_form(statement, form_schema)
    if form_data is None:
        return FillResult(ok=False, breakpoint_id=bp["breakpoint_id"],
                          message=f"自然语言模糊，无法匹配表单：{err}。请前往任务看板手动填写表单。")

    # 3. 校验必填项与枚举
    err = _validate(form_data, form_schema)
    if err:
        return FillResult(ok=False, breakpoint_id=bp["breakpoint_id"],
                          message=f"表单校验失败：{err}。请前往任务看板手动填写表单。")

    # 4. 提交恢复接口（submit_source = chat_agent，资产留痕）
    answered = db.answer_breakpoint(bp["breakpoint_id"], form_data,
                                    submit_source="chat_agent",
                                    answered_by="surface_agent")
    if not answered:  # 幂等保护
        return FillResult(ok=False, breakpoint_id=bp["breakpoint_id"], message="断点已被处理")

    answer_payload = {
        "task_id": bp["task_id"],
        "breakpoint_id": bp["breakpoint_id"],
        "form_data": form_data,
        "submit_source": "chat_agent",
    }
    ok, msg = await dispatcher.send_resume(bp["continue_endpoint"], answer_payload)
    task = db.get_task(bp["task_id"])
    if ok and task and task["status"] == "waiting_human":
        db.update_task_status(bp["task_id"], "running")
        db.add_task_log(bp["task_id"], room_id, "task_resumed",
                        {"breakpoint_id": bp["breakpoint_id"], "via": "chat_agent"})
    events.publish_business_notify(room_id, "breakpoint_answered",
                                   {"taskId": bp["task_id"], "breakpointId": bp["breakpoint_id"],
                                    "submitSource": "chat_agent"})
    events.publish_business_notify(room_id, "task_status_changed",
                                   {"taskId": bp["task_id"],
                                    "status": "running" if ok else task["status"] if task else "unknown"})

    title = (form_schema or {}).get("title", "")
    return FillResult(ok=ok, breakpoint_id=bp["breakpoint_id"], form_data=form_data,
                      message=(f"已根据您的表述自动填写断点「{title}」并提交：{json.dumps(form_data, ensure_ascii=False)}"
                               if ok else f"表单已提交但恢复工作AI代理失败：{msg}"))


# ════════════════════════════════════════════════════════════════
# append_task_note —— 会议AI代理 直接写任务面板
# ════════════════════════════════════════════════════════════════
async def append_task_note(args: dict) -> dict:
    """直接把沟通记录/结论写入任务面板（任务看板）。

    用于会议AI代理在任务执行过程中，把进展、决议或结果摘要追加进任务卡，
    构成「会议AI发起任务 → 与工作AI沟通的所有结果 → 结案」闭环中的沟通留痕。
    已结案（只读）任务拒绝追加。
    """
    room_id = args["room_id"]
    task_id = args.get("task_id")
    note = (args.get("note") or "").strip()
    kind = args.get("kind", "progress")  # progress（进展记录）| summary（结论摘要）

    task = db.get_task(task_id) if task_id else None
    if not task:
        return {"ok": False, "message": f"任务不存在: {task_id}"}
    if task["status"] == "closed":
        return {"ok": False, "message": "任务已结案（只读），无法追加记录"}
    if not note:
        return {"ok": False, "message": "note 不能为空"}

    db.add_task_log(task_id, room_id, "agent_note", {"kind": kind, "note": note})
    if kind == "summary":
        # 结论摘要：覆盖写入 result_summary（不改动任务状态）
        db.update_task_status(task_id, task["status"], result_summary=note)
    events.publish_business_notify(room_id, "task_status_changed",
                                   {"taskId": task_id, "status": task["status"]})
    return {"ok": True,
            "message": f"已写入任务面板（{('结论摘要' if kind == 'summary' else '进展记录')}）：{note[:60]}"}
