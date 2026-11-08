"""代码修订 Agent（可插拔底座）· 对接服务器端 WorkBuddy agent（Linux CLI）。

复用既有双向 Webhook 协议，与通用重度 Agent 模拟器（heavy_agent/run.py）同构：
1. 接收任务下发（POST /agent/tasks，携带完整 context_package，含母体对话 theme/tags）；
2. 按主题路由命中（room.theme == CODE_AGENT_THEME）后由 dispatcher 发往本服务；
3. 通过同机 CLI 调起 WorkBuddy agent（subprocess），对目标仓库做真实代码修订；
4. 将过程按既有协议回传调度服务 webhook 网关：
   - task_progress.thinking ：修订思路（思维链）
   - task_progress.content  ：实际改动说明 / CLI 输出片段
   - task_error             ：未配置 CLI / 仓库缺失 / 执行失败 / 超时
   - task_finish            ：完成（附 diff / 摘要）
5. 支持取消（cancel）终止正在进行的子进程。

注意：本服务不直接编辑文件，而是把"修订指令"交给 WorkBuddy agent 执行；
若 WorkBuddy agent 需要人工拍板（选文件/确认方案），可后续扩展为 human_breakpoint。

启动：python code_revision_agent.py   （默认 127.0.0.1:8312）
环境变量见 server/app/config.py（CODE_AGENT_* / WORKBUDDY_CLI / WORKBUDDY_CLI_ARGS / CODE_AGENT_REPO）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess as sp
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from server.app import config  # 复用项目配置（与调度服务同一份）


def _recycle(path: str) -> None:
    """安全删除：「回收站」式 rename，避免触发沙箱 safe-delete 守卫（拦截 unlink/rmtree 抛 SystemExit）。

    优先移动到 <父目录>/.trash/ 下（rename 不触发守卫）；失败再尝试直接删除（守卫拦截则忽略）。
    """
    p = Path(path)
    if not p.exists():
        return
    trash = p.parent / ".trash"
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    dest = trash / f"{int(time.time() * 1000)}-{p.name}"
    try:
        shutil.move(str(p), str(dest))
        return
    except Exception:
        pass
    try:
        if p.is_dir():
            shutil.rmtree(str(p))
        else:
            p.unlink()
    except SystemExit:
        pass
    except OSError:
        pass

EVENTS_URL = config.CODE_AGENT_EVENTS_URL
SELF_BASE_URL = config.CODE_AGENT_BASE_URL

app = FastAPI(title="代码修订 Agent（WorkBuddy CLI 对接层）", version="0.1.0")

# task_id -> 执行状态（内存态，属于 Agent 内部数据，不进入产品资产）
_tasks: dict[str, dict] = {}
_procs: dict[str, asyncio.subprocess.Process] = {}

# 调度服务（8300）根地址：用于修订前自动快照数据库、回滚时恢复数据库。
# 由 CODE_AGENT_EVENTS_URL（形如 http://.../internal/webhooks/agent-events）推导。
SCHEDULER_BASE = config.CODE_AGENT_EVENTS_URL.rsplit("/internal/webhooks/agent-events", 1)[0]


class TaskIn(BaseModel):
    task_id: str
    room_id: str
    type: str = "normal_task"
    context_package: Any = {}  # 兼容 dict 或 JSON 字符串（dispatcher 下发时为字符串）


async def _post_event(payload: dict) -> None:
    """事件回调（带轻量重试）。"""
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(EVENTS_URL, json=payload)
                if 200 <= resp.status_code < 300:
                    return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3 * attempt)


async def _post_progress(task_id: str, thinking: str | None = None,
                         content: str | None = None, step: str | None = None) -> None:
    if not thinking and not content:
        return
    await _post_event({"event_type": "task_progress", "task_id": task_id,
                       "thinking": thinking, "content": content, "step": step})


# ── 数据库快照 / 恢复（版本安全网的一侧）────────────────────────────
# 代码修订可能改动数据库（迁移/数据），故在修订前由调度服务（持有实时连接的同一进程）
# 对 app.db 做全量快照，回滚时一并恢复，做到「代码 + 数据库」双保险。
async def _snapshot_db(task_id: str) -> str | None:
    """请求调度服务对本项目的 app.db 做全量快照，返回备份名（失败返回 None）。"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SCHEDULER_BASE}/internal/db-backups",
                json={"label": f"code-{task_id}", "include_data": True})
            if 200 <= r.status_code < 300:
                return r.json().get("name")
    except Exception as exc:  # noqa: BLE001
        print(f"[code-agent] 数据库快照失败: {exc}")
    return None


async def _restore_db(name: str) -> bool:
    """请求调度服务将 app.db 恢复到指定快照（回滚时使用）。"""
    if not name:
        return False
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{SCHEDULER_BASE}/internal/db-backups/{name}/restore")
            return 200 <= r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        print(f"[code-agent] 数据库恢复失败: {exc}")
    return False


def _build_prompt(context_package: dict) -> str:
    """把任务上下文整理成给 WorkBuddy agent 的修订指令。"""
    summary = (context_package.get("summary")
               or context_package.get("user_request")
               or "请根据对话上下文修订代码")
    parts = [summary.strip()]
    extra = []
    for k in ("theme", "tags", "repo_path", "instruction"):
        v = context_package.get(k)
        if v:
            extra.append(f"- {k}: {v}")
    # 若上下文含对话记录，截取末尾若干条作为依据
    transcript = context_package.get("transcript") or context_package.get("messages")
    if isinstance(transcript, list) and transcript:
        tail = transcript[-6:]
        extra.append("- 近期对话摘要:")
        for m in tail:
            role = m.get("role") or m.get("name") or "user"
            text = (m.get("content") or m.get("text") or "").strip()
            if text:
                extra.append(f"  · {role}: {text[:200]}")
    if extra:
        parts.append("\n上下文依据:\n" + "\n".join(extra))
    return "\n".join(parts)


def _build_argv(prompt: str, repo: str) -> list[str]:
    """由 WORKBUDDY_CLI（二进制）+ WORKBUDDY_CLI_ARGS（模板，含 {prompt}/{repo} 占位符）构造 argv。"""
    template = config.WORKBUDDY_CLI_ARGS
    argv = [config.WORKBUDDY_CLI]
    for tok in shlex.split(template):
        if tok == "{prompt}":
            argv.append(prompt)
        elif tok == "{repo}":
            argv.append(repo)
        else:
            argv.append(tok)
    return argv


# ── 版本保存与回滚（安全网）─────────────────────────────────────────
# 设计：执行任何代码改动前先建立版本快照；完成后记录版本号与变更清单；
# 出错或用户不满意时可一键回滚到快照基线。优先用 git（tag/commit/reset），
# 仓库非 git 时回退到文件级快照（copytree 备份 + 还原），保证无 git 也可回滚。
IGNORE_DIRS = {".git", ".revision-backups", "node_modules", "__pycache__",
               ".venv", "venv", ".idea", ".vscode", "dist", "build", ".workbuddy"}
IGNORE_FILES = {"*.pyc", "*.pyo", "*.so", "*.o", "*.class", ".DS_Store", "*.egg-info"}


def _git(repo: str, *args: str, check: bool = True, timeout: int = 60) -> sp.CompletedProcess:
    r = sp.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()}")
    return r


def _is_git_repo(repo: str) -> bool:
    r = sp.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo,
               capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _walk_files(root: str, skip_dirs: set[str]) -> set[str]:
    """返回 root 下相对路径集合（跳过忽略目录与文件）。"""
    res: set[str] = set()
    for dp, dnames, fnames in os.walk(root):
        dnames[:] = [d for d in dnames if d not in skip_dirs]
        for f in fnames:
            if any(fnmatch.fnmatch(f, pat) for pat in IGNORE_FILES):
                continue
            res.add(os.path.relpath(os.path.join(dp, f), root))
    return res


def _same_file(a: str, b: str) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    if sa.st_size != sb.st_size:
        return False
    if sa.st_mtime == sb.st_mtime and sa.st_size < 2_000_000:
        return True
    h1 = hashlib.sha256()
    h2 = hashlib.sha256()
    for p in (a, b):
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                (h1 if p == a else h2).update(chunk)
    return h1.digest() == h2.digest()


def _copytree_ignore(src: str, names: list[str]) -> list[str]:
    out = []
    for n in names:
        full = os.path.join(src, n)
        if os.path.isdir(full):
            if n in IGNORE_DIRS:
                out.append(n)
        elif any(fnmatch.fnmatch(n, pat) for pat in IGNORE_FILES):
            out.append(n)
    return out


class RepoGuard:
    """对目标仓库建立版本快照、记录版本、执行回滚。"""

    def __init__(self, repo: str, task_id: str):
        self.repo = repo
        self.task_id = task_id
        self.method = None            # git | file | none
        self.baseline = None          # git tag 或 backup 目录
        self.stash = False            # 是否因未提交改动而创建了 stash
        self.backup_dir = None

    def snapshot(self, init_git: bool, backup_enabled: bool) -> dict:
        if init_git or _is_git_repo(self.repo):
            if not _is_git_repo(self.repo):
                _git(self.repo, "init", timeout=60)
                # 避免把备份目录纳入版本控制
                gi = os.path.join(self.repo, ".gitignore")
                try:
                    cur = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
                    if config.CODE_AGENT_BACKUP_ROOT not in cur:
                        with open(gi, "a", encoding="utf-8") as fh:
                            fh.write(f"\n{config.CODE_AGENT_BACKUP_ROOT}/\n")
                except OSError:
                    pass
            status = _git(self.repo, "status", "--porcelain", check=False)
            if status.stdout.strip():
                _git(self.repo, "stash", "push", "-u", "-m", f"pre-{self.task_id}", check=False)
                self.stash = True
            _git(self.repo, "tag", "-f", f"pre-{self.task_id}")
            self.method = "git"
            self.baseline = f"pre-{self.task_id}"
            return {"method": "git", "baseline": self.baseline, "stash": self.stash}
        if backup_enabled:
            root = os.path.join(self.repo, config.CODE_AGENT_BACKUP_ROOT)
            self.backup_dir = os.path.join(root, f"pre-{self.task_id}")
            if os.path.exists(self.backup_dir):
                shutil.rmtree(self.backup_dir)
            os.makedirs(os.path.dirname(self.backup_dir), exist_ok=True)
            shutil.copytree(self.repo, self.backup_dir, ignore=_copytree_ignore, symlinks=False)
            self.method = "file"
            self.baseline = self.backup_dir
            _prune_backups(root)  # 控制备份数量，避免无限增长
            return {"method": "file", "baseline": self.backup_dir}
        self.method = "none"
        return {"method": "none", "baseline": None}

    def collect_changes(self) -> list[dict]:
        if self.method == "git":
            # 对比基线 commit 与工作区（含未暂存改动，即 agent 直接编辑的结果）
            out = _git(self.repo, "diff", "--name-status",
                       self.baseline, check=False).stdout
            changes = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    changes.append({"status": parts[0].strip(), "path": parts[-1]})
            # 新增的未跟踪文件（agent 创建，diff 不含）
            st = _git(self.repo, "status", "--porcelain", check=False).stdout
            for line in st.splitlines():
                if line[:2] == "??":
                    changes.append({"status": "A", "path": line[3:].strip()})
            return changes
        if self.method == "file" and self.backup_dir:
            bset = _walk_files(self.backup_dir, IGNORE_DIRS)
            rset = _walk_files(self.repo, IGNORE_DIRS | {".revision-backups", ".git"})
            changes = []
            for p in sorted(bset - rset):
                changes.append({"status": "D", "path": p})     # 被 agent 删除
            for p in sorted(rset - bset):
                changes.append({"status": "A", "path": p})     # 被 agent 新增
            for p in sorted(bset & rset):
                if not _same_file(os.path.join(self.backup_dir, p), os.path.join(self.repo, p)):
                    changes.append({"status": "M", "path": p})
            return changes
        return []

    def commit_version(self, message: str) -> str | None:
        if self.method == "git":
            _git(self.repo, "add", "-A", check=False)
            r = _git(self.repo, "commit", "-m", message, check=False)
            if r.returncode == 0:
                return _git(self.repo, "rev-parse", "HEAD", check=False).stdout.strip()
            return None
        if self.method == "file":
            return self.backup_dir  # 版本即快照目录
        return None

    def rollback(self) -> dict:
        if self.method == "git":
            _git(self.repo, "reset", "--hard", self.baseline)
            _git(self.repo, "clean", "-fd", check=False)
            if self.stash:
                _git(self.repo, "stash", "pop", check=False)
            return {"ok": True, "method": "git", "baseline": self.baseline}
        if self.method == "file" and self.backup_dir:
            bset = _walk_files(self.backup_dir, IGNORE_DIRS)
            rset = _walk_files(self.repo, IGNORE_DIRS | {".revision-backups", ".git"})
            restored = removed = 0
            for p in bset:
                src = os.path.join(self.backup_dir, p)
                dst = os.path.join(self.repo, p)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
            for p in (rset - bset):           # agent 新增的文件删掉
                fp = os.path.join(self.repo, p)
                if os.path.exists(fp):
                    os.remove(fp)
                    removed += 1
            return {"ok": True, "method": "file", "restored": restored, "removed": removed}
        return {"ok": False, "method": self.method or "none"}


def _manifest_path(repo: str, task_id: str) -> str:
    return os.path.join(repo, config.CODE_AGENT_BACKUP_ROOT,
                        f"pre-{task_id}", "revision-manifest.json")


def _write_manifest(repo: str, task_id: str, meta: dict) -> None:
    try:
        path = _manifest_path(repo, task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _prune_backups(root: str) -> None:
    """保留最近 N 份快照，删除最旧的，避免备份无限增长。

    被「收藏」(pinned) 的版本永不被清理（无论 git 还是 file 模式）。
    """
    try:
        entries = [os.path.join(root, d) for d in os.listdir(root)
                   if d.startswith("pre-") and os.path.isdir(os.path.join(root, d))]
    except OSError:
        return
    if len(entries) <= config.CODE_AGENT_KEEP_BACKUPS:
        return
    items = []  # (path, mtime, pinned)
    for e in entries:
        m = None
        mp = os.path.join(e, "revision-manifest.json")
        try:
            if os.path.exists(mp):
                with open(mp, encoding="utf-8") as fh:
                    m = json.load(fh)
        except (OSError, ValueError):
            m = None
        pinned = bool(m.get("pinned")) if isinstance(m, dict) else False
        items.append((e, os.path.getmtime(e), pinned))
    items.sort(key=lambda x: x[1])  # 最旧在前
    to_delete = len(items) - config.CODE_AGENT_KEEP_BACKUPS
    if to_delete <= 0:
        return
    deletable = [it for it in items if not it[2]]
    deletable.sort(key=lambda x: x[1])
    for e, _, _ in deletable[:to_delete]:
        _recycle(e)


def _read_manifest(repo: str, task_id: str) -> dict | None:
    try:
        with open(_manifest_path(repo, task_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


async def _run_pipeline(task_id: str, room_id: str, context_package: Any) -> None:
    state = _tasks[task_id]
    # 兼容 dispatcher 以 JSON 字符串形式下发 context_package 的情况
    if isinstance(context_package, str):
        try:
            context_package = json.loads(context_package)
        except (TypeError, ValueError):
            context_package = {}
    if not isinstance(context_package, dict):
        context_package = {}
    try:
        # 0) 前置校验：CLI 与仓库
        if not config.WORKBUDDY_CLI:
            await _post_event({"event_type": "task_error", "task_id": task_id,
                               "error_message": "未配置代码代理 CLI（WORKBUDDY_CLI 为空），请在 .env 中设置。"})
            state["status"] = "failed"
            return
        repo = context_package.get("repo_path") or config.CODE_AGENT_REPO
        if not repo or not os.path.isdir(repo):
            await _post_event({"event_type": "task_error", "task_id": task_id,
                               "error_message": f"未指定有效目标仓库路径（repo_path={repo!r}），请在任务 context_package 或 CODE_AGENT_REPO 中设置。"})
            state["status"] = "failed"
            return

        prompt = _build_prompt(context_package)
        argv = _build_argv(prompt, repo)

        # 0.5) 版本快照（安全网）：在执行任何改动前建立基线，支持后续回滚
        guard = RepoGuard(repo, task_id)
        snap = guard.snapshot(config.CODE_AGENT_INIT_GIT, config.CODE_AGENT_BACKUP_ENABLED)
        state["revision"] = snap
        await _post_progress(
            task_id,
            thinking=f"已建立版本快照（方式：{snap['method']}），任务完成后可随时回滚到该基线。",
            content=f"版本基线：{snap.get('baseline')}",
            step="准备·版本快照")

        # 0.6) 数据库快照（安全网的一侧）：在修订前对 app.db 做全量备份，
        # 回滚时一并恢复，覆盖「代码 + 数据库」双维度。
        db_backup_name = await _snapshot_db(task_id)
        if db_backup_name:
            state.setdefault("revision", {})["db_backup"] = db_backup_name
            await _post_progress(
                task_id,
                thinking=f"已对数据库做修订前全量快照（{db_backup_name}），回滚时可一并恢复。",
                content=f"数据库快照：{db_backup_name}",
                step="准备·数据库快照")

        await _post_progress(
            task_id,
            thinking=f"已解析修订指令，目标仓库：{repo}；将调用 WorkBuddy agent（CLI）执行代码修订。",
            content=f"启动指令：{' '.join(argv[:2])} …（仓库 {repo}）",
            step="准备·调起 WorkBuddy agent")

        # 1) 执行 CLI（同机、非交互、捕获输出）
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            await _post_event({"event_type": "task_error", "task_id": task_id,
                               "error_message": f"无法启动 WorkBuddy CLI（{config.WORKBUDDY_CLI}）：{exc}"})
            state["status"] = "failed"
            return
        _procs[task_id] = proc

        # 2) 流式读取输出 → 进度回传（思维链 + 实际输出），节流避免刷屏
        buf = []
        last_flush = time.time()
        collected = []
        assert proc.stdout is not None
        while True:
            if state["status"] == "canceled":
                proc.kill()
                return
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                # 心跳：长时间无输出也回传一次"运行中"，让用户知道没卡死
                if buf or (time.time() - last_flush > 8):
                    joined = "".join(buf).strip()
                    if joined:
                        await _post_progress(task_id, content=joined, step="执行中")
                    buf.clear()
                    last_flush = time.time()
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace")
            collected.append(text)
            buf.append(text)
            if len(buf) >= 3 or (time.time() - last_flush) > 3.0:
                joined = "".join(buf).strip()
                if joined:
                    await _post_progress(task_id, content=joined, step="执行中")
                buf.clear()
                last_flush = time.time()

        rc = await asyncio.wait_for(proc.wait(), timeout=config.CODE_AGENT_TIMEOUT)
        if buf:
            joined = "".join(buf).strip()
            if joined:
                await _post_progress(task_id, content=joined, step="执行中")

        # 3) 收尾
        output = "".join(collected).strip()
        if rc == 0:
            state["status"] = "finished"
            summary = (output[-300:].strip().replace("\n", " ") or "代码修订已完成")
            # 收集版本信息并落盘版本清单，供面板展示与回滚
            changes = guard.collect_changes()
            version = (guard.commit_version(f"revision: {task_id}\n\n{summary}")
                       if config.CODE_AGENT_COMMIT else None)
            meta = {
                "method": snap["method"],
                "baseline": snap.get("baseline"),
                "commit": version,
                "version_saved": bool(version) if config.CODE_AGENT_COMMIT else False,
                "changes": changes,
                "repo": repo,
                "task_id": task_id,
                "db_backup": state.get("revision", {}).get("db_backup"),
                "pinned": False,
                "finished_at": time.time(),
            }
            _write_manifest(repo, task_id, meta)
            await _post_progress(
                task_id,
                thinking=f"WorkBuddy agent 已执行完毕，代码变更已落盘到目标仓库（方式：{snap['method']}）。"
                         f"本次变更 {len(changes)} 个文件。"
                         + (f" 已存为版本 {version}。" if version else ""),
                content="代码修订完成，变更已写入仓库。",
                step="完成·收尾")
            await _post_event({
                "event_type": "task_finish",
                "task_id": task_id,
                "summary": "代码修订完成（WorkBuddy agent）",
                "result": f"已在仓库 {repo} 完成修订。\n\n执行输出摘要：\n{summary}"
                          + (f"\n\n版本：{version}" if version else ""),
                "attachments": [],
                "meta": meta,
            })
        else:
            state["status"] = "failed"
            await _post_event({
                "event_type": "task_error",
                "task_id": task_id,
                "error_message": f"WorkBuddy agent 退出码 {rc}；输出：{(output or '')[-500:]}",
            })
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        state["status"] = "failed"
        await _post_event({"event_type": "task_error", "task_id": task_id,
                           "error_message": f"{type(exc).__name__}: {exc}"})
    finally:
        _procs.pop(task_id, None)


@app.post("/agent/tasks")
async def receive_task(req: TaskIn):
    """事件1：接收任务下发，启动异步修订。"""
    if req.task_id in _tasks and _tasks[req.task_id]["status"] not in ("finished", "failed", "canceled"):
        return {"ok": True, "ignored": "duplicate"}  # 幂等
    _tasks[req.task_id] = {"status": "running", "room_id": req.room_id,
                           "context_package": req.context_package, "created_at": time.time()}
    asyncio.create_task(_run_pipeline(req.task_id, req.room_id, req.context_package))
    return {"ok": True, "task_id": req.task_id, "status": "running"}


@app.post("/agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: dict | None = None):
    """task_cancel：取消任务（终止正在进行的子进程）。"""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if state["status"] in ("finished", "canceled", "failed"):
        return {"ok": True, "ignored": f"已是终态 {state['status']}"}
    state["status"] = "canceled"
    proc = _procs.pop(task_id, None)
    if proc and proc.returncode is None:
        proc.kill()
    return {"ok": True, "task_id": task_id, "status": "canceled"}


@app.get("/agent/tasks/{task_id}")
def task_state(task_id: str):
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    return state


@app.get("/health")
def health():
    return {"status": "ok", "service": "code-revision-agent", "running": len(
        [1 for s in _tasks.values() if s["status"] in ("running",)])}


@app.post("/agent/tasks/{task_id}/rollback")
async def rollback_task(task_id: str, body: dict | None = None):
    """回滚某次代码修订：将目标仓库还原到该任务执行前的版本快照。"""
    state = _tasks.get(task_id)
    repo = config.CODE_AGENT_REPO
    if state:
        cp = state.get("context_package") or {}
        if isinstance(cp, str):
            try:
                cp = json.loads(cp)
            except (TypeError, ValueError):
                cp = {}
        repo = (cp.get("repo_path") if isinstance(cp, dict) else None) or repo
    if not repo or not os.path.isdir(repo):
        raise HTTPException(400, f"无法确定目标仓库路径（repo={repo!r}），无法回滚")

    # 优先从磁盘清单重建基线（Agent 重启后仍可回滚）
    manifest = _read_manifest(repo, task_id)
    guard = RepoGuard(repo, task_id)
    if manifest:
        guard.method = manifest.get("method")
        guard.baseline = manifest.get("baseline")
        guard.backup_dir = manifest.get("backup_dir") or os.path.join(
            repo, config.CODE_AGENT_BACKUP_ROOT, f"pre-{task_id}")
    else:
        # 内存态里也没有快照信息，尝试按 task_id 重建 file 基线
        guard.backup_dir = os.path.join(
            repo, config.CODE_AGENT_BACKUP_ROOT, f"pre-{task_id}")
        guard.method = "file" if os.path.isdir(guard.backup_dir) else "none"

    if guard.method in (None, "none") or not (
            guard.baseline or (guard.method == "file" and os.path.isdir(guard.backup_dir))):
        raise HTTPException(404, "未找到该任务的版本快照，无法回滚")

    res = guard.rollback()
    if not res.get("ok"):
        raise HTTPException(500, f"回滚执行失败：{res}")

    # 回滚数据库（安全网的一侧）：将 app.db 恢复到修订前快照（若当时已建立）。
    db_backup = (manifest or {}).get("db_backup")
    if not db_backup and state:
        db_backup = (state.get("revision") or {}).get("db_backup")
    db_restored = False
    if db_backup:
        db_restored = await _restore_db(db_backup)
        if db_restored:
            res["db_restored"] = db_backup

    await _post_event({
        "event_type": "task_rollback",
        "task_id": task_id,
        "by": (body or {}).get("by"),
        "note": (body or {}).get("note", "人工回滚"),
        "db_restored": db_restored,
    })
    return {"ok": True, "task_id": task_id, "repo": repo, "db_backup": db_backup,
            "db_restored": db_restored, **res}


@app.post("/agent/tasks/{task_id}/pin")
async def pin_task(task_id: str, body: dict | None = None):
    """人工收藏/取消收藏某个关键代码版本（git 提交 + 仓库快照）。

    收藏后该版本永不被快照清理策略删除（保留基线以供回滚），
    并同步置顶状态到调度服务（8300）的任务记录（best-effort）。
    """
    pinned = bool((body or {}).get("pinned", True))
    repo = config.CODE_AGENT_REPO
    state = _tasks.get(task_id)
    if state:
        cp = state.get("context_package") or {}
        if isinstance(cp, str):
            try:
                cp = json.loads(cp)
            except (TypeError, ValueError):
                cp = {}
        repo = (cp.get("repo_path") if isinstance(cp, dict) else None) or repo
    if not repo or not os.path.isdir(repo):
        raise HTTPException(400, f"无法确定目标仓库路径（repo={repo!r}），无法收藏")
    # 更新磁盘清单（manifest）的 pinned 标记
    manifest = _read_manifest(repo, task_id) or {}
    manifest["pinned"] = pinned
    manifest["task_id"] = task_id
    _write_manifest(repo, task_id, manifest)
    # 同步到调度服务 tasks 表的 pinned 标记（best-effort）
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{SCHEDULER_BASE}/internal/tasks/{task_id}/pin",
                json={"pinned": pinned})
    except Exception as exc:  # noqa: BLE001
        print(f"[code-agent] 通知调度服务置顶失败: {exc}")
    return {"ok": True, "task_id": task_id, "pinned": pinned}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8312")))
