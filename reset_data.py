"""清空所有业务数据，保留表结构（可重复运行）。

用途：把演示/种子数据彻底清空，得到一个干净、真实的起点。
- 不会删除 app.db 文件（保留 schema 与迁移）；
- 关闭外键约束后按依赖顺序清空所有业务表；
- 重置 sqlite_sequence，使自增 id 归零。

运行：python3 reset_data.py  （服务是否运行均可；建议先停服避免写锁竞争）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "server" / "data" / "app.db"

# 业务表（清空顺序不强制，因为已关闭外键约束）
TABLES = [
    "event_log",
    "memory_snapshots",
    "task_logs",
    "breakpoints",
    "tasks",
    "messages",
    "meeting_state",
    "members",
    "rooms",
    "sessions",
    "accounts",
]


def main() -> None:
    if not DB.exists():
        print(f"⚠️  数据库不存在：{DB}")
        return
    con = sqlite3.connect(str(DB))
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN")
        total = 0
        for t in TABLES:
            try:
                n = con.execute(f'DELETE FROM "{t}"').rowcount
            except sqlite3.OperationalError as e:
                print(f"  · 跳过 {t}：{e}")
                continue
            total += n
            print(f"  · 已清空 {t:16s} {n:>5d} 行")
        # 重置自增序列（若存在）
        try:
            con.execute("DELETE FROM sqlite_sequence")
        except sqlite3.OperationalError:
            pass
        con.execute("COMMIT")
        print(f"\n✅ 清空完成，共移除 {total} 行业务数据。数据库文件保留：{DB}")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
