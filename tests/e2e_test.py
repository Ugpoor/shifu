"""端到端验收测试（覆盖 PRD「MVP上线验收标准」七条）。

兼容真实 LLM（火山引擎方舟 Doubao-Seed-2.1-pro）：
  - 放宽 SSE 读取与状态轮询超时（真实模型经沙箱代理偶发 >60s）；
  - 提示词改为明确、无歧义，确保稳定触发 submit_heavy_task / fill_breakpoint_form；
  - 断言基于「行为」而非具体中文措辞，避免真实 LLM 改写文案导致误判。

前置：调度服务 127.0.0.1:8300、重度Agent模拟器 127.0.0.1:8310 均已启动。
运行：python3 tests/e2e_test.py
"""
from __future__ import annotations

import json
import time

import httpx

SCHED = "http://127.0.0.1:8300"
HTTP_TIMEOUT = 240   # 真实 LLM 工具决策/流式偶发 >60s，SSE 读取需足够长
POLL_TIMEOUT = 120   # 任务状态轮询上限（waiting_human / finished）
PASS: list[tuple[str, bool]] = []


def ok(name: str, cond: bool, detail: str = ""):
    status = "✅" if cond else "❌"
    PASS.append((name, cond))
    print(f"{status} {name}" + (f"  ── {detail}" if detail else ""))
    if not cond:
        raise SystemExit(f"验收失败：{name} {detail}")


def read_sse(resp) -> list[dict]:
    events, buf = [], ""
    for chunk in resp.iter_text():
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))
    return events


def wait_until(get_value, timeout: int = POLL_TIMEOUT, interval: float = 0.5):
    """轮询直到 get_value() 返回真值，返回该值；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = get_value()
        if val:
            return val
        time.sleep(interval)
    return None


def newest(items: list[dict], key: str = "created_at") -> dict:
    return sorted(items, key=lambda x: x.get(key) or "", reverse=True)[0]


def main():
    c = httpx.Client(timeout=HTTP_TIMEOUT)
    mid = lambda m: {"X-Member-Id": m}  # 便捷：注入当前操作成员身份

    # ── 验收1：创建房间，双人进入，流式对话、记忆快照、主持人静默开关 ──
    room = c.post(f"{SCHED}/rooms", json={"title": "尽调测算协同", "host_name": "王咨询师",
                                          "expert_name": "李专家"}).json()
    room_id, host, expert = room["room_id"], room["host"]["member_id"], room["expert"]["member_id"]
    print(f"\n房间：{room_id}")

    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": host,
                        "content": "我们这次要测算项目的IRR，约束是不能用高杠杆情景"}) as r:
        evs = read_sse(r)
    ok("1a 主持人消息触发流式SSE对话",
       any(e["type"] == "token" for e in evs) and any(e["type"] == "done" for e in evs))
    ok("1b 主动触发记忆快照", any(e["type"] == "memory_snapshot" for e in evs))

    c.post(f"{SCHED}/rooms/{room_id}/silent", json={"silent": True}, headers=mid(host))
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": expert, "content": "专家补充：假设无重大政策变化"}) as r:
        evs = read_sse(r)
    ok("1c 静默模式下专家发言不触发Agent", any(e["type"] == "silent_skipped" for e in evs))
    c.post(f"{SCHED}/rooms/{room_id}/silent", json={"silent": False}, headers=mid(host))
    resp = c.post(f"{SCHED}/rooms/{room_id}/silent", json={"silent": True}, headers=mid(expert))
    ok("1d 静默开关仅主持人可操作", resp.status_code == 403)

    # ── 验收2：提交任务包，重度Agent正常接收并启动推演 ──
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": host,
                        "content": "共识已对齐：本次测算项目IRR，约束不使用高杠杆情景。请提交任务开始推演。"}) as r:
        evs = read_sse(r)
    tool_ok = any(e["type"] == "tool_call" and e["name"] == "submit_heavy_task" for e in evs)
    ok("2a 主持人触发submit_heavy_task", tool_ok)

    # 取最新 pending 断点推导任务（不依赖列表顺序）
    pend = wait_until(lambda: c.get(f"{SCHED}/rooms/{room_id}/breakpoints?status=pending",
                                    headers=mid(host)).json() or None)
    ok("2b 重度Agent接收任务并抛出human_breakpoint", bool(pend),
       f"pending={len(pend) if pend else 0}")
    bp1 = newest(pend)
    task_id = bp1["task_id"]
    wh = wait_until(lambda: c.get(f"{SCHED}/tasks/{task_id}", headers=mid(host)).json()
                    ["task"]["status"] == "waiting_human")
    ok("2b 任务状态=waiting_human", bool(wh))

    # ── 验收3：断点表单Schema下发正常 ──
    bp1_fresh = c.get(f"{SCHED}/breakpoints/{bp1['breakpoint_id']}", headers=mid(host)).json()
    ok("3 断点form_schema携带完整字段",
       bool(bp1_fresh) and bp1_fresh["form_schema"]["fields"][0]["type"] == "radio",
       f"breakpoint={bp1['breakpoint_id']}")

    # ── 验收4（通路A）：看板手动填写提交，重度Agent接收答复继续执行 ──
    c.post(f"{SCHED}/breakpoints/{bp1['breakpoint_id']}/answer",
           json={"form_data": {"scenario": "bear", "note": "优先评估下行风险，符合本次尽调要求"},
                 "submit_source": "panel_human"}, headers=mid(expert))  # 专家也可填表
    # 幂等校验：重复提交应 409
    dup = c.post(f"{SCHED}/breakpoints/{bp1['breakpoint_id']}/answer",
                 json={"form_data": {"scenario": "base"}, "submit_source": "panel_human"},
                 headers=mid(host))
    ok("4a 断点重复提交幂等(409)", dup.status_code == 409)
    fin = wait_until(lambda: c.get(f"{SCHED}/tasks/{task_id}", headers=mid(host)).json()
                     ["task"]["status"] == "finished")
    ok("4b 通路A：手动填表→续跑→任务完成", bool(fin),
       c.get(f"{SCHED}/tasks/{task_id}", headers=mid(host)).json()["task"].get("result_summary", "")
       if fin else "timeout")

    # ── 验收5（通路B）：对话自然语言→AI自动填表（成功/失败分支）──
    # 先提交第二个任务，制造新的 pending 断点
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": host,
                        "content": "共识已对齐：请提交第二个任务，测算项目净现值NPV，采用基准情景，开始推演。"}) as r:
        read_sse(r)
    pend2 = wait_until(lambda: c.get(f"{SCHED}/rooms/{room_id}/breakpoints?status=pending",
                                     headers=mid(host)).json() or None)
    ok("5 提交第二任务并抛出新断点", bool(pend2))
    bp2 = newest(pend2)

    # 失败分支：用户明确要求改走「看板手动处理」→ 系统不应自动填表，
    # 断点保持 pending（核心保证：绝不误填），Agent 给出有效回应（不崩溃）。
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": expert,
                        "content": "这个断点我不通过对话填了，请提示我去任务看板手动处理。"}) as r:
        evs = read_sse(r)
    any_token = any(e["type"] == "token" for e in evs)
    bp_after = c.get(f"{SCHED}/breakpoints/{bp2['breakpoint_id']}", headers=mid(host)).json()
    ok("5a 通路B失败分支：明确改走看板→断点保持pending（不误填）",
       bp_after["status"] == "pending", f"status={bp_after['status']}")
    ok("5a 失败分支：Agent给出有效回应（未崩溃）", any_token)

    # 成功分支：明确表述情景与备注 → 触发 fill_breakpoint_form
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": expert,
                        "content": "我们选保守情景(bear)，备注：优先控制下行风险，请按此自动填写断点表单。"}) as r:
        evs = read_sse(r)
    filled = any(e["type"] == "tool_call" and e["name"] == "fill_breakpoint_form" for e in evs)
    final = next((e for e in evs if e["type"] == "final"), {})
    ok("5b 通路B成功分支：识别断点并调用fill_breakpoint_form", filled,
       f"final.tool={final.get('tool')}")
    time.sleep(1.5)
    bp_detail = c.get(f"{SCHED}/breakpoints/{bp2['breakpoint_id']}", headers=mid(host)).json()
    ok("5b-fail submit_source=chat_agent 溯源正确",
       bp_detail["answer_submit_source"] == "chat_agent",
       json.dumps(bp_detail.get("answer_form_data"), ensure_ascii=False))
    fin = wait_until(lambda: c.get(f"{SCHED}/tasks/{bp2['task_id']}", headers=mid(host)).json()
                     ["task"]["status"] == "finished")
    ok("5b-ok AI自动填表→重度Agent续跑完成", bool(fin))

    # ── 验收6：任务取消、异常事件回调 ──
    with c.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                  json={"member_id": host,
                        "content": "共识已对齐：请提交第三个任务，测算项目投资回收期Payback Period，开始推演。"}) as r:
        read_sse(r)
    time.sleep(3)
    tasks = c.get(f"{SCHED}/rooms/{room_id}/tasks", headers=mid(host)).json()
    t3 = newest([t for t in tasks if t["status"] in ("queued", "running", "waiting_human")])
    # 专家取消应被拒绝
    deny = c.post(f"{SCHED}/tasks/{t3['task_id']}/cancel", headers=mid(expert))
    ok("6a 取消任务仅主持人", deny.status_code == 403)
    c.post(f"{SCHED}/tasks/{t3['task_id']}/cancel", headers=mid(host))
    st = c.get(f"{SCHED}/tasks/{t3['task_id']}", headers=mid(host)).json()["task"]["status"]
    ok("6b 主持人取消任务生效", st == "canceled")
    # task_error 事件（直接模拟重度Agent异常回调）
    err = c.post(f"{SCHED}/internal/webhooks/agent-events",
                 json={"event_type": "task_error", "task_id": t3["task_id"],
                       "error_message": "测试异常"}).json()
    ok("6c task_error回调幂等（已终态忽略）", err.get("ok") and err.get("ignored"))

    # ── 验收7：对话、断点选择记录入库，可生成结构化记忆快照 ──
    msgs = c.get(f"{SCHED}/rooms/{room_id}/messages", headers=mid(host)).json()
    ok("7a 对话全量持久化", len(msgs) >= 8)
    snaps = c.get(f"{SCHED}/rooms/{room_id}/memory/snapshots", headers=mid(host)).json()
    ok("7b 结构化记忆快照已生成",
       len(snaps) >= 1 and "constraints" in snaps[0]["content"])
    guest = c.get(f"{SCHED}/rooms/{room_id}/memory/snapshots", headers=mid(expert))
    ok("7c 记忆资产仅主持人可见", guest.status_code == 403)
    detail = c.get(f"{SCHED}/tasks/{bp2['task_id']}", headers=mid(host)).json()
    kinds = {l["event"] for l in detail["logs"]}
    ok("7d 任务生命周期日志完整",
       {"task_created", "task_dispatched", "breakpoint_raised", "breakpoint_answered",
        "task_finished"} <= kinds, str(kinds))

    print(f"\n{'='*50}\n全部 {len(PASS)} 项验收通过 ✅")


if __name__ == "__main__":
    main()
