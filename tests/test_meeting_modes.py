"""会议发言规则验收：轮询所有人 / 只回应主持人 / 只回应嘉宾 / 罗伯特议事法则。

覆盖：
1. 四种模式的触发门禁（谁发言 Agent 才回应）
2. 罗伯特法则：一轮每人一次、全员说完 Agent 统一汇总回应、自动进入下一轮
3. 撤回窗口内重说（前一条标记已撤回）
4. 撤回窗口外补言被拒（系统提示入历史）
5. 主持人动议直通（不受轮次限制）
6. 权限：仅主持人可改会议流程
"""
from __future__ import annotations

import json
import sys

import httpx

SCHED = "http://127.0.0.1:8300"
PASS, FAIL = 0, 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def sse(room: dict, member: str, text: str) -> list[dict]:
    """发一条消息，收集完整 SSE 事件序列。"""
    evs = []
    with httpx.stream("POST", f"{SCHED}/rooms/{room['room_id']}/chat",
                      json={"member_id": member, "content": text}, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("data: "):
                evs.append(json.loads(line[6:]))
    return evs


def types(evs: list[dict]) -> list[str]:
    return [e["type"] for e in evs]


def has(evs: list[dict], t: str) -> bool:
    return any(e["type"] == t for e in evs)


def agent_text(evs: list[dict]) -> str:
    for e in evs:
        if e["type"] == "agent_message":
            return e["message"]["content"]
    return ""


def set_flow(room: dict, host: str, **kw) -> dict:
    r = httpx.post(f"{SCHED}/rooms/{room['room_id']}/flow-settings", json=kw,
                   headers={"X-Member-Id": host})
    r.raise_for_status()
    return r.json()


def new_room(title: str) -> dict:
    r = httpx.post(f"{SCHED}/rooms", json={"title": title,
                                           "host_name": "王咨询师", "expert_name": "李专家"})
    r.raise_for_status()
    d = r.json()
    return {"room_id": d["room_id"], "host": d["host"]["member_id"],
            "expert": d["expert"]["member_id"]}


def main() -> int:
    room = new_room("会议发言规则验收")
    host, expert = room["host"], room["expert"]
    rid = room["room_id"]

    print("\n[A] 默认规则：轮询所有人")
    flow = httpx.get(f"{SCHED}/rooms/{rid}/flow-settings",
                     headers={"X-Member-Id": host}).json()
    ok("A1 默认发言规则为 round_robin", flow["response_mode"] == "round_robin")
    ok("A2 返回四种可选规则", len(flow["response_mode_options"]) == 4,
       str([o["value"] for o in flow["response_mode_options"]]))
    evs = sse(room, host, "我们先把项目目标对齐一下")
    ok("A3 主持人发言 → Agent 回应", has(evs, "agent_message"))
    evs = sse(room, expert, "我补充一个假设：政策环境稳定")
    ok("A4 专家发言 → Agent 也回应", has(evs, "agent_message"))

    print("\n[B] 只回应主持人")
    set_flow(room, host, response_mode="host_only")
    evs = sse(room, expert, "专家说一句：渠道成本偏高")
    ok("B1 专家发言不触发 Agent", has(evs, "gated") and not has(evs, "agent_message"),
       str(types(evs)))
    ok("B2 门禁原因标记 host_only",
       any(e.get("reason") == "host_only" for e in evs if e["type"] == "gated"))
    evs = sse(room, host, "收到，我记录一下")
    ok("B3 主持人发言仍触发 Agent", has(evs, "agent_message"))

    print("\n[C] 只回应嘉宾")
    set_flow(room, host, response_mode="guest_only")
    evs = sse(room, host, "主持人自言自语：先看看资料")
    ok("C1 主持人发言不触发 Agent", has(evs, "gated") and not has(evs, "agent_message"))
    evs = sse(room, expert, "我的判断是下行风险更大")
    ok("C2 嘉宾发言触发 Agent", has(evs, "agent_message"))

    print("\n[D] 罗伯特议事法则：一轮一次，全员说完统一回应")
    set_flow(room, host, response_mode="robert_rules", recall_minutes=2)
    httpx.post(f"{SCHED}/rooms/{rid}/meeting-state/reset", headers={"X-Member-Id": host})
    evs = sse(room, host, "第1轮我先陈述：目标是测算项目IRR，约束是不能用高杠杆")
    ok("D1 首位发言仅登记，Agent 暂不回应", has(evs, "round_waiting") and not has(evs, "agent_message"),
       str(types(evs)))
    ok("D2 等待事件里点名待发言成员",
       "李专家" in json.dumps(evs, ensure_ascii=False))
    ms = httpx.get(f"{SCHED}/rooms/{rid}/meeting-state", headers={"X-Member-Id": host}).json()
    ok("D3 轮次状态：第1轮、已发言1人、待发言李专家",
       ms["round_no"] == 1 and ms["pending_names"] == ["李专家"], str(ms["pending_names"]))
    evs = sse(room, expert, "我补充：假设未来三年政策环境稳定，贴现率按8%")
    text = agent_text(evs)
    ok("D4 全员发言完毕 → Agent 统一回应", has(evs, "agent_message"))
    ok("D5 统一回应含轮次汇总抬头", "第1轮" in text and "全员发言完毕" in text, text[:80])
    ok("D6 汇总里归集了双方发言", "目标" in text and "贴现率" in text, text[:160])
    ok("D7 回合结束事件", has(evs, "round_closed"))
    ms = httpx.get(f"{SCHED}/rooms/{rid}/meeting-state", headers={"X-Member-Id": host}).json()
    ok("D8 轮次清空，下一位发言进入新一轮", ms["participants"] and not ms["all_spoken"])

    print("\n[E] 撤回窗口内重说")
    httpx.post(f"{SCHED}/rooms/{rid}/meeting-state/reset", headers={"X-Member-Id": host})
    evs = sse(room, host, "我的第一版表述：优先看基准情景")
    first_id = next(e["message"]["msg_id"] for e in evs if e["type"] == "user_message")
    evs = sse(room, host, "更正：优先看下行保守情景，备注控制下行风险")
    ok("E1 窗口内重说不算二次发言", has(evs, "message_withdrawn") and not has(evs, "round_blocked"),
       str(types(evs)))
    ok("E2 撤回事件指向原消息", any(e["msg_id"] == first_id for e in evs if e["type"] == "message_withdrawn"))
    msgs = httpx.get(f"{SCHED}/rooms/{rid}/messages", headers={"X-Member-Id": host}).json()
    old = [m for m in msgs if m["msg_id"] == first_id][0]
    ok("E3 原消息标记为已撤回", old.get("withdrawn") == 1)

    print("\n[F] 撤回窗口外补言被拒")
    set_flow(room, host, recall_minutes=0)  # 窗口=0 → 立即不可撤回
    evs = sse(room, host, "我再补一句：其实还想加一个敏感性分析")
    ok("F1 本轮二次发言被拒", has(evs, "round_blocked") and not has(evs, "agent_message"),
       str(types(evs)))
    ok("F2 拒绝提示进入历史记录（系统消息）", has(evs, "round_blocked") and "已发言" in evs[-2]["message"])

    print("\n[G] 主持人动议直通（不受轮次限制）")
    set_flow(room, host, recall_minutes=2, procedural_bypass=True)
    httpx.post(f"{SCHED}/rooms/{rid}/meeting-state/reset", headers={"X-Member-Id": host})
    sse(room, host, "第X轮：先对齐目标")
    evs = sse(room, host, "提交任务，开始推演")
    ok("G1 主持人动议在轮次内可直接下发",
       has(evs, "tool_call") and has(evs, "agent_message"), str(types(evs)))
    ok("G2 动议未打断轮次登记",
       httpx.get(f"{SCHED}/rooms/{rid}/meeting-state",
                 headers={"X-Member-Id": host}).json()["round_no"] >= 1)

    print("\n[H] 权限：仅主持人可改会议流程")
    r = httpx.post(f"{SCHED}/rooms/{rid}/flow-settings",
                   json={"response_mode": "round_robin"}, headers={"X-Member-Id": expert})
    ok("H1 专家改流程被拒 403", r.status_code == 403, str(r.status_code))
    r = httpx.post(f"{SCHED}/rooms/{rid}/flow-settings",
                   json={"response_mode": "bad_mode"}, headers={"X-Member-Id": host})
    ok("H2 非法规则被拒 400", r.status_code == 400, str(r.status_code))
    r = httpx.post(f"{SCHED}/rooms/{rid}/flow-settings",
                   json={"recall_minutes": 999}, headers={"X-Member-Id": host})
    ok("H3 撤回窗口被夹到 0-60", r.json()["recall_minutes"] == 60, str(r.json()))

    print(f"\n{'='*54}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*54}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
