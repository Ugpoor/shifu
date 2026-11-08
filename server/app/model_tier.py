"""会议AI代理「模糊等级」模型档位管理。

三档对应火山方舟模型（价格由低到高，档位含义见下）：
  - cheapest 最便宜   : 跨代最低价文本模型（默认 doubao-seed-2.1-lite）
  - moderate 适中     : 中段价文本模型（默认 doubao-seed-2.1-turbo）
  - high     最新高性能: 最新代 Seed 旗舰（默认 doubao-seed-2.1-pro）

另含视频创作模型（Seedance 系列）选择，存放于同文件的 video 节点。

配置落盘于 server/data/model_tiers.json：
  - 每档端点 API Key（endpoint_id + api_key，Key 与端点严格绑定）
  - 由 price_monitor.py 每月抓取火山方舟价格页、经 LLM 抽取后刷新
    model / price_in / price_out / generation / needs_provisioning
  - last_updated 记录上一次价格更新时间，source_url 记录来源页

后端通过 active_llm() 取当前档位的 (model_id, api_key, base_url) 注入每次调用；
active_llm() 在所选档位缺失端点时自动回退到任一已配置端点，保证对话不中断。

注意：本模块每次调用均从磁盘读取（不缓存），以便 price_monitor 的定时更新
无需重启服务即可生效。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import config

TIERS_PATH = config.DATA_DIR / "model_tiers.json"

# 兜底：若配置文件缺失，回退到 .env 中的单模型（高性能档）
_FALLBACK = {
    "selected": "high",
    "last_updated": None,
    "source_url": None,
    "tiers": {
        "high": {
            "label": "最新高性能",
            "model": "doubao-seed-2.1-pro",
            "generation": "2.1",
            "price_in": 3.00,
            "price_out": 15.00,
            "endpoint_id": config.LLM_MODEL,
            "api_key": config.LLM_API_KEY,
            "needs_provisioning": False,
        }
    },
    "video": {"selected": None, "models": {}},
}


def _load() -> dict:
    if TIERS_PATH.exists():
        try:
            return json.loads(TIERS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _FALLBACK


def _save(data: dict) -> None:
    TIERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 原子写入：先写临时文件再 rename，避免监控脚本与服务并发读写导致半截 JSON
    fd, tmp = tempfile.mkstemp(dir=str(TIERS_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TIERS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_tiers() -> dict:
    """返回设置页所需的完整档位清单（不含密钥）。"""
    d = _load()
    tiers = {
        k: {
            "label": v.get("label", k),
            "model": v.get("model", ""),
            "generation": v.get("generation", ""),
            "price_in": v.get("price_in"),
            "price_out": v.get("price_out"),
            "needs_provisioning": bool(v.get("needs_provisioning", False)),
        }
        for k, v in d.get("tiers", {}).items()
    }
    video = d.get("video", {}) or {}
    return {
        "selected": d.get("selected", "high"),
        "last_updated": d.get("last_updated"),
        "source_url": d.get("source_url"),
        "tiers": tiers,
        "video": {
            "selected": video.get("selected"),
            "models": video.get("models", {}),
        },
    }


def set_selected(key: str) -> str:
    d = _load()
    if key not in d.get("tiers", {}):
        raise ValueError(f"未知档位: {key}")
    d["selected"] = key
    _save(d)
    return key


def active_llm() -> tuple[str, str, str]:
    """返回当前档位的 (model_id, api_key, base_url)。

    model_id 使用端点 id（ep-...），因为每档 Key 严格绑定各自端点。
    若所选档位缺端点/密钥，自动回退到任一已配置端点，保证对话不中断；
    若全部缺失则回退到 .env 单模型配置。
    """
    d = _load()
    sel = d.get("selected", "high")
    tiers = d.get("tiers", {})

    def _usable(t: dict) -> bool:
        return bool(t.get("endpoint_id")) and bool(t.get("api_key"))

    tier = tiers.get(sel)
    if tier and _usable(tier):
        return tier["endpoint_id"], tier["api_key"], config.LLM_BASE_URL
    # 回退：所选档位缺端点时，自动选用【仍可用且最便宜】的已配置档位，
    # 以尽量贴合「最便宜」意图，同时保证对话不中断。
    usable = [t for t in tiers.values() if _usable(t)]
    if usable:
        best = min(
            usable,
            key=lambda t: (t.get("price_in") or 0) + (t.get("price_out") or 0),
        )
        return best["endpoint_id"], best["api_key"], config.LLM_BASE_URL
    return config.LLM_MODEL, config.LLM_API_KEY, config.LLM_BASE_URL


def active_label() -> str:
    d = _load()
    sel = d.get("selected", "high")
    return d.get("tiers", {}).get(sel, {}).get("label", sel)


# ── 视频创作模型（Seedance）────────────────────────────────────────────
def get_video_models() -> dict:
    return _load().get("video", {}).get("models", {})


def get_video_model() -> dict | None:
    d = _load()
    v = d.get("video", {}) or {}
    sel = v.get("selected")
    if sel and sel in v.get("models", {}):
        return {"key": sel, **v["models"][sel]}
    return None


def set_video_model(key: str) -> str:
    d = _load()
    v = d.setdefault("video", {})
    models = v.get("models", {})
    if key not in models:
        raise ValueError(f"未知视频模型: {key}")
    v["selected"] = key
    _save(d)
    return key


def save_full(data: dict) -> None:
    """由 price_monitor 写入完整结构（原子写）。"""
    _save(data)
