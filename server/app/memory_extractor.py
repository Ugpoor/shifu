"""结构化记忆抽取（PRD 模块4：核心壁垒）。

从对话中提取：约束 / 假设 / 共识 / 验收标准 / 风险，生成版本化记忆快照。
默认内置规则引擎（零外部依赖）；配置 LLM_API_KEY 后可切换 LLM 抽取。
"""
from __future__ import annotations

import re

from . import config, db, model_tier

# 形如「约束：xxx」「共识：xxx」的显式标记，优先级最高
_MARKERS = {
    "constraints": ("约束", "限制", "红线"),
    "assumptions": ("假设", "前提"),
    "consensus": ("共识", "对齐", "确认"),
    "acceptance": ("验收", "交付标准", "交付要求"),
    "risks": ("风险", "隐患"),
}


def _extract_by_rules(messages: list[dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {k: [] for k in _MARKERS}
    for m in messages:
        if m["role"] == "agent":
            continue
        for sentence in re.split(r"[。；;\n]", m["content"]):
            s = sentence.strip()
            if not s:
                continue
            for key, words in _MARKERS.items():
                if any(w in s for w in words) and s not in result[key]:
                    result[key].append(s)
    return result


async def _extract_by_llm(messages: list[dict]) -> dict[str, list[str]]:
    """OpenAI 兼容接口抽取（配置了 LLM_API_KEY 时启用）。异步，避免阻塞事件循环。"""
    import httpx

    sys_prompt = (
        "你是咨询对话记忆抽取器。从对话中提取五类结构化记忆："
        "constraints(约束)、assumptions(假设)、consensus(共识)、acceptance(验收标准)、risks(风险)。"
        '只输出 JSON：{"constraints":[],"assumptions":[],"consensus":[],"acceptance":[],"risks":[]}'
    )
    text = "\n".join(f"{m['role']}: {m['content']}" for m in messages if m["role"] != "agent")
    try:
        import json
        model_id, api_key, base_url = model_tier.active_llm()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model_id, "temperature": 0,
                      "messages": [{"role": "system", "content": sys_prompt},
                                   {"role": "user", "content": text[-6000:]}],
                      "response_format": {"type": "json_object"}},
            )
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001  LLM 抽取失败静默降级到规则引擎
        return _extract_by_rules(messages)


async def extract_and_snapshot(room_id: str) -> dict:
    messages = db.list_messages(room_id)
    content = (await _extract_by_llm(messages) if config.LLM_API_KEY
               else _extract_by_rules(messages))
    empty = all(not v for v in content.values())
    if empty:  # 无可提取内容时记录空快照，保证版本链完整
        content = {"constraints": [], "assumptions": [], "consensus": [],
                   "acceptance": [], "risks": []}
    # 把话题主题前置到快照，使记忆可按主题聚类（Agent 检索时按主题归并）
    room = db.get_room(room_id) or {}
    if room.get("theme"):
        content = {"theme": room["theme"], **content}
    return db.save_memory_snapshot(room_id, content, source="auto")
