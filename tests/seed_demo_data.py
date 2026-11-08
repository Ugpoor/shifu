"""演示种子数据：造出带历史归档的面板数据。

- 房间A「某项目投资测算协同咨询」：当下活断点（待办）+ 刚完成任务（已办·30天内明细）
- 房间B「渠道扩张策略陪跑」：35天前的2条已完成任务（折叠为日条目）+
  往年12月的2条已完成任务（归档为年条目）

运行前提：调度服务 8300、重度Agent 8310 已启动。
"""
from __future__ import annotations

import json
import time

import httpx

SCHED = "http://127.0.0.1:8300"
DB = "/Users/manman/WorkBuddy/师傅来了/server/data/app.db"


def api(path, opt=None):
    r = httpx.post(f"{SCHED}{path}", json=opt or {}, timeout=30) if opt is not None \
        else httpx.get(f"{SCHED}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def chat_sse(room_id: str, member_id: str, content: str):
    with httpx.stream("POST", f"{SCHED}/rooms/{room_id}/chat",
                      json={"member_id": member_id, "content": content}, timeout=60) as r:
        for _ in r.iter_text():
            pass


def wait_status(task_id: str, status: str, timeout=25):
    for _ in range(int(timeout * 2)):
        t = api(f"/tasks/{task_id}")["task"]
        if t["status"] == status:
            return t
        time.sleep(0.5)
    raise SystemExit(f"任务 {task_id} 未到 {status}")


def backdate(task_id: str, epoch: float):
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE task_id=?",
                 (epoch, epoch, task_id))
    conn.execute("UPDATE task_logs SET created_at=? WHERE task_id=?", (epoch, task_id))
    conn.commit()
    conn.close()


def main():
    # ── 房间A：活断点 + 当下已完成 ──
    a = api("/rooms", {"title": "某项目投资测算协同咨询", "host_name": "王咨询师", "expert_name": "李专家"})
    ra, host, expert = a["room_id"], a["host"]["member_id"], a["expert"]["member_id"]
    chat_sse(ra, host, "我们这次要测算项目的IRR，约束是不能用高杠杆情景")
    chat_sse(ra, expert, "补充：假设未来三年政策环境保持稳定")
    chat_sse(ra, host, "共识已对齐，提交任务，开始推演")
    wait_status_by_poll(ra, "waiting_human")
    # 通路A手动填表 → 完成
    bp = api(f"/rooms/{ra}/breakpoints?status=pending", {})[0] if False else \
        httpx.get(f"{SCHED}/rooms/{ra}/breakpoints?status=pending",
                  headers={"X-Member-Id": host}).json()[0]
    api_ans = httpx.post(f"{SCHED}/breakpoints/{bp['breakpoint_id']}/answer",
                         json={"form_data": {"scenario": "bear", "note": "优先评估下行风险"},
                               "submit_source": "panel_human"},
                         headers={"X-Member-Id": host})
    api_ans.raise_for_status()
    wait_status_by_poll(ra, "finished_any")
    print(f"房间A就绪：{ra}")

    # ── 房间B：历史归档数据 ──
    b = api("/rooms", {"title": "渠道扩张策略陪跑", "host_name": "王咨询师", "expert_name": "陈顾问"})
    rb = b["room_id"]
    chat_sse(rb, b["host"]["member_id"], "我们讨论渠道扩张的优先级，约束是预算上限两千万")
    chat_sse(rb, b["expert"]["member_id"], "建议先做华东试点再复制")

    now = time.time()
    DAY = 86400
    cases = [
        # (标题, 绝对时间戳)
        ("华东试点渠道测算", now - (35 * DAY + 3600 * 10)),   # 35天前 → 日条目
        ("华南代理体系推演", now - (35 * DAY + 3600 * 4)),    # 同一天相邻 → 折叠进日条目
        ("2025年度渠道复盘推演", _last_dec(2025, 15)),         # 往年12月 → 年条目
        ("2025年双十二促销推演", _last_dec(2025, 3)),          # 同年12月 → 折叠进年条目
    ]
    for title, offset in cases:
        chat_sse(rb, b["host"]["member_id"], f"提交任务：{title}，开始推演")
        tasks = httpx.get(f"{SCHED}/rooms/{rb}/tasks",
                          headers={"X-Member-Id": b["host"]["member_id"]}).json()
        task = next(t for t in tasks if t["summary"] == "双人协同对齐后的重型推演任务"
                    and t["status"] in ("queued", "running", "waiting_human", "finished", "failed"))
        # 等它跑完
        for _ in range(50):
            t = httpx.get(f"{SCHED}/tasks/{task['task_id']}",
                          headers={"X-Member-Id": b["host"]["member_id"]}).json()["task"]
            if t["status"] in ("finished", "failed", "canceled"):
                break
            if t["status"] == "waiting_human":  # 中途断点：走通路A快速答复
                bps = httpx.get(f"{SCHED}/rooms/{rb}/breakpoints?status=pending",
                                headers={"X-Member-Id": b["host"]["member_id"]}).json()
                if bps:
                    httpx.post(f"{SCHED}/breakpoints/{bps[0]['breakpoint_id']}/answer",
                               json={"form_data": {"scenario": "base", "note": "基准情景"},
                                     "submit_source": "panel_human"},
                               headers={"X-Member-Id": b["host"]["member_id"]})
            time.sleep(0.5)
        # 改摘要并回溯时间
        import sqlite3
        conn = sqlite3.connect(DB)
        epoch = offset
        conn.execute("UPDATE tasks SET summary=?, created_at=?, updated_at=? WHERE task_id=?",
                     (title, epoch, epoch, task["task_id"]))
        conn.execute("UPDATE task_logs SET created_at=? WHERE task_id=?", (epoch, task["task_id"]))
        conn.commit()
        conn.close()
        print(f"  已归档任务：{title} @ {time.strftime('%Y-%m-%d', time.localtime(epoch))}")

    print("种子数据完成")


def _last_dec(year: int, day: int) -> float:
    return time.mktime((year, 12, day, 10, 0, 0, 0, 0, 0))


def wait_status_by_poll(room_id: str, target: str):
    host_tasks = None
    for _ in range(60):
        rooms = httpx.get(f"{SCHED}/rooms").json()
        time.sleep(0.5)
        break
    # 简化：由调用方后续等待
    time.sleep(3)


if __name__ == "__main__":
    main()
