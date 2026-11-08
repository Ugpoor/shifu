"""设置接口：会议AI代理「模糊等级」（模型档位）读取与切换，以及视频创作模型选择。

说明：价格三档（最便宜/适中/最新高性能）与 Seedance 视频清单的更新，
由 server/utils/price_monitor.py 按「月份陈旧度」自动判定（见 app/main.py 启动钩子），
不在设置菜单提供手动「更新价格」入口。可经 CLI `python3 server/utils/price_monitor.py --force` 强制刷新。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import model_tier

router = APIRouter(tags=["settings"])


class TierSelectReq(BaseModel):
    tier: str  # cheapest | moderate | high


class VideoModelReq(BaseModel):
    model: str  # Seedance 模型 id


@router.get("/settings/agent")
def get_agent_settings():
    """返回会议AI代理模型档位清单与当前选择（不含密钥）。"""
    return model_tier.get_tiers()


@router.put("/settings/agent")
def set_agent_settings(req: TierSelectReq):
    """切换会议AI代理「模糊等级」档位（最便宜/适中/最新高性能）。"""
    try:
        selected = model_tier.set_selected(req.tier)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "selected": selected, "label": model_tier.active_label()}


@router.get("/settings/video-model")
def get_video_model_settings():
    """返回 Seedance 视频创作模型清单与当前选择（不含密钥）。"""
    d = model_tier.get_tiers()
    return d["video"]


@router.put("/settings/video-model")
def set_video_model_settings(req: VideoModelReq):
    """选择视频创作模型（Seedance）。"""
    try:
        selected = model_tier.set_video_model(req.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "selected": selected}
