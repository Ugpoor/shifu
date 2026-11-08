"""数据库备份/恢复命令行工具（本地运维用，非服务进程）。

与运行中的服务共享同一份 db.py 逻辑（在线备份 / 恢复 / 列表 / 清理）。

用法：
  python server/db_backup_cli.py backup [--label L] [--structure-only]
  python server/db_backup_cli.py list
  python server/db_backup_cli.py restore <name>
  python server/db_backup_cli.py download <name> [--out PATH]
  python server/db_backup_cli.py schema <name> [--out PATH]
  python server/db_backup_cli.py prune [--keep N]
  python server/db_backup_cli.py star <name> [--unpin]

说明：
- backup：全量备份（结构+数据），可加 --structure-only 仅导出建表语句。
- restore：用全量备份覆盖当前实时数据库（服务需重启以重连，或在运行实例中通过接口恢复）。
  直接对运行中的 app.db 执行本命令会替换文件，请确保服务已停止或改用 /admin 接口。
- prune：仅保留最近 N 份（默认读 DB_BACKUP_KEEP）；被收藏(pinned)的备份永不被清理。
- star：收藏/取消收藏某份备份（收藏后免自动清理）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 让脚本可直接以 `python server/db_backup_cli.py` 运行（定位到 server/ 包）
_SERVER = Path(__file__).resolve().parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from app import config, db  # noqa: E402


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    return f"{n/1024/1024:.1f}MB"


def cmd_backup(args: argparse.Namespace) -> int:
    info = db.backup_database(args.label, include_data=not args.structure_only)
    print(f"✓ 已创建{'结构' if args.structure_only else '全量'}备份：{info['name']}")
    print(f"  结构文件：{info['schema_file']}")
    if not args.structure_only:
        print(f"  数据文件：{info['db_file']}（{_fmt_size(info['db_size'])}）")
    print(f"  表数量：{info['tables']}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    items = db.list_db_backups()
    if not items:
        print("（暂无备份）")
        return 0
    print(f"共 {len(items)} 份备份：")
    for b in items:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b["created_at"]))
        star = "⭐" if b.get("pinned") else "  "
        print(f"  {star} {b['name']}  [{'全量' if b['has_data'] else '仅结构'}]  表:{b['tables']}  "
              f"大小:{_fmt_size(b['db_size'])}  时间:{ts}")
    return 0


def cmd_star(args: argparse.Namespace) -> int:
    try:
        info = db.set_db_backup_pinned(args.name, not args.unpin)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ {('已收藏' if info.get('pinned') else '已取消收藏')}：{args.name}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    try:
        res = db.restore_database(args.name)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ 已恢复至备份：{res['name']}")
    print("  提示：若服务正在运行，请重启服务以重新加载数据库连接。")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    d = db.get_db_backup_dir()
    f = d / f"{args.name}.db"
    if not f.exists():
        print("✗ 备份不存在", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else Path(f"{args.name}.db")
    out.write_bytes(f.read_bytes())
    print(f"✓ 已下载到：{out}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    d = db.get_db_backup_dir()
    f = d / f"{args.name}.schema.sql"
    if not f.exists():
        print("✗ 结构文件不存在", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else Path(f"{args.name}.schema.sql")
    out.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"✓ 已导出结构到：{out}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    keep = args.keep if args.keep is not None else config.DB_BACKUP_KEEP
    db._prune_db_backups(db.get_db_backup_dir(), keep)
    print(f"✓ 已清理，仅保留最近 {keep} 份")
    return 0


def main() -> int:
    db.init_db()  # CLI 是独立进程，需先建立连接
    p = argparse.ArgumentParser(description="数据库备份/恢复工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="创建备份")
    b.add_argument("--label", default=None)
    b.add_argument("--structure-only", action="store_true", help="仅导出建表语句")
    b.set_defaults(func=cmd_backup)

    sub.add_parser("list", help="列出备份").set_defaults(func=cmd_list)

    r = sub.add_parser("restore", help="从全量备份恢复")
    r.add_argument("name")
    r.set_defaults(func=cmd_restore)

    dl = sub.add_parser("download", help="下载全量备份文件")
    dl.add_argument("name")
    dl.add_argument("--out", default=None)
    dl.set_defaults(func=cmd_download)

    s = sub.add_parser("schema", help="导出结构 SQL")
    s.add_argument("name")
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_schema)

    pr = sub.add_parser("prune", help="清理旧备份")
    pr.add_argument("--keep", type=int, default=None)
    pr.set_defaults(func=cmd_prune)

    st = sub.add_parser("star", help="收藏/取消收藏备份")
    st.add_argument("name")
    st.add_argument("--unpin", action="store_true", help="取消收藏")
    st.set_defaults(func=cmd_star)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
