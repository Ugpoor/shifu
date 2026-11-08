#!/usr/bin/env python3
"""在 git push 时运行：把当前 commit + push 时间写入 server/version.json。

设置页据此展示「提交版本号 + 版本更新时间（git push 时间）」，
并带 开发预览 / 正式发布 徽标以区分构建类型。

用法：
    python3 scripts/stamp_version.py            # 默认 开发预览
    python3 scripts/stamp_version.py release    # 正式发布（写「正式发布」徽标）
"""
import datetime
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> None:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    build = "正式发布" if arg in ("release", "rel", "prod", "正式发布") else "开发预览"

    data = {
        "commit": git("rev-parse", "--short", "HEAD") or "unknown",
        "commit_full": git("rev-parse", "HEAD") or "",
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "master",
        "commit_time": git("log", "-1", "--format=%cI") or "",
        "pushed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "build": build,
    }

    out = os.path.join(SERVER, "version.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"wrote {out}  (commit={data['commit']} build={build})")


if __name__ == "__main__":
    main()
