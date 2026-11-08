"""FastAPI 应用入口（中转调度服务 + 会议AI代理服务，单体分层）。

状态唯一数据源；IM/客户端仅做展示；工作AI代理 通过 Webhook 协议可插拔对接。
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg
from . import db, events, memory_extractor, model_tier
from .routers import (auth, chat, contacts, db_backup, memory, rooms, tasks,
                      webhooks, settings, categories)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    events.bind_loop(asyncio.get_running_loop())
    # 价格三档自动更新：若超过一个月未更新（price_monitor 按月份陈旧度判定），
    # 则在后台静默刷新（抓取火山官方价格页 + LLM 解析，必要时结构化兜底）。
    # 非手动触发，亦非外部定时任务；仅在服务启动时自动检查一次。
    asyncio.create_task(_price_auto_refresh())
    # 数据库每日自动全量备份（版本/回滚安全网的兜底；保留份数见 DB_BACKUP_KEEP）。
    asyncio.create_task(_db_auto_backup())
    yield


async def _price_auto_refresh():
    import sys
    from pathlib import Path
    server_dir = Path(__file__).resolve().parent.parent   # server/
    utils_dir = server_dir / "utils"
    for d in (str(server_dir), str(utils_dir)):
        if d not in sys.path:
            sys.path.insert(0, d)
    import price_monitor
    try:
        await asyncio.to_thread(price_monitor.maybe_refresh)
    except Exception as e:
        print(f"[startup] 价格自动更新失败：{e}")


async def _db_auto_backup():
    """每日全量备份数据库（后台常驻任务）。
    失败仅记录，不影响主流程；保留份数由 config.DB_BACKUP_KEEP 控制。"""
    if not cfg.DB_BACKUP_AUTODAILY:
        return
    while True:
        try:
            info = await asyncio.to_thread(db.backup_database, "daily", True)
            print(f"[db-backup] 每日自动备份完成：{info['name']}")
        except Exception as e:  # noqa: BLE001
            print(f"[db-backup] 每日自动备份失败：{e}")
        # 约 24 小时执行一次
        await asyncio.sleep(24 * 3600)


app = FastAPI(title="师傅来了· AI系统开发运营陪跑 · 中转调度服务", version="0.1.0-MVP",
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(rooms.router)
app.include_router(chat.router)
app.include_router(tasks.router)
app.include_router(webhooks.router)
app.include_router(memory.router)
app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(contacts.router)
app.include_router(categories.router)
app.include_router(db_backup.router_user)
app.include_router(db_backup.router_internal)


class Heartbeat(BaseModel):
    client: str = ""


@app.get("/health")
def health():
    return {"status": "ok", "service": "scheduler", "version": "0.1.0-MVP"}


@app.get("/system/info")
def system_info():
    """公开能力信息（不含密钥）：用于设置页展示 Webhook 与微信登录能力。"""
    return {
        "base_url": cfg.PUBLIC_BASE_URL,
        "heavy_agent_endpoint": cfg.HEAVY_AGENT_TASK_ENDPOINT,
        "wechat_enabled": bool(cfg.WECHAT_APPID),
        "version": "0.1.0-MVP",
    }


@app.get("/agents")
async def list_agents():
    """机器人清单：会议AI代理 + 工作AI代理（可替换底座）状态汇总。"""
    import httpx

    heavy_base = cfg.HEAVY_AGENT_TASK_ENDPOINT.rsplit("/agent/tasks", 1)[0]
    heavy_status, heavy_detail = "offline", "未连接"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{heavy_base}/health")
            data = r.json()
            heavy_status = "online" if data.get("status") == "ok" else "degraded"
            heavy_detail = f"运行中任务 {data.get('running', 0)}"
    except Exception as exc:  # noqa: BLE001
        heavy_detail = str(exc)[:80]
    return {
        "agents": [
            {
                "id": "meeting-ai",
                "name": "会议AI代理",
                "type": "surface",
                "desc": "对话澄清 · 共识沉淀 · 记忆抽取 · 对话驱动填表",
                "engine": "OpenAI兼容LLM" if cfg.LLM_API_KEY else "内置Mock引擎",
                "tier": model_tier.active_label() if cfg.LLM_API_KEY else "Mock",
                "status": "online",
                "endpoint": "/rooms/{room_id}/chat",
            },
            {
                "id": "work-ai",
                "name": "工作AI代理（可替换底座）",
                "type": "heavy",
                "desc": "复杂多步骤推演 · 断点挂起续跑 · 结构化事件输出",
                "engine": "标准Webhook协议",
                "status": heavy_status,
                "detail": heavy_detail,
                "endpoint": cfg.HEAVY_AGENT_TASK_ENDPOINT,
            },
        ]
    }


@app.post("/debug/heartbeat")
def heartbeat(body: Heartbeat):
    db.log_event(None, "heartbeat", body.model_dump())
    return {"ok": True, "trace_id": uuid.uuid4().hex}


# ── PWA 资源（iOS「添加到主屏幕」图标 / manifest / Service Worker 灰度壳）──
_PWA_FILES = {
    "/manifest.webmanifest": ("static/manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("static/sw.js", "text/javascript"),
    "/icon-192.png": ("static/icon-192.png", "image/png"),
    "/icon-512.png": ("static/icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("static/apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("static/icon-192.png", "image/png"),
}


def _pwa_file(path: str, media: str):
    return FileResponse(path, media_type=media)


for _p, (_f, _m) in _PWA_FILES.items():
    app.get(_p)(lambda f=_f, m=_m: _pwa_file(f, m))


# ── 浏览器演示客户端（MVP 联调用，与 Flutter 端同构）────────────────
app.mount("/demo", StaticFiles(directory="static", html=True), name="demo")


@app.get("/")
def root():
    return FileResponse("static/index.html")
