#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""price_monitor.py — 火山方舟模型价格监控与三档自动更新。

设计要点（对应需求）：
  1. 从「特定网站」（火山方舟模型价格页）抓取最新模型比价数据。
  2. 比价文本交给 LLM 模块做一次【自然语言识别】（非正则死解析），
     精准抽取为结构化价格清单。
  3. 由结构化清单计算三档：
       - cheapest   跨代最低价文本模型（允许跨代，不限制 Seed 系列）
       - moderate   中段价文本模型（Seed 系列）
       - high       最新代 Seed 旗舰（最高代次）
     以及 Seedance 视频创作模型清单。
  4. 写回 server/data/model_tiers.json（保留已有端点/密钥；缺失则标记
     needs_provisioning），并记录 last_updated 与 source_url。
  5. 另存 server/data/model_prices.json（原始抽取结果 + 推荐 + 时间戳）便于审计。
  6. 更新触发：按「月份陈旧度」自动更新——只有 last_updated 与当前不在同一月份
     （即超过约一个月）才真正抓取并更新；其余情况跳过。可在服务启动时由
     app.main 的 lifespan 自动触发，亦可在打开设置页（数据陈旧时）后台触发。
     调试可加 --force 强制刷新；--dry-run 只打印不写文件。

用法：
  python server/utils/price_monitor.py            # 按月份陈旧度决定是否更新
  python server/utils/price_monitor.py --force    # 强制抓取 + LLM抽取 + 更新
  python server/utils/price_monitor.py --dry-run  # 只打印报告，不写文件（隐含 --force）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

# ── 让脚本可直接 import app 包（server/ 为父目录）──────────────────────
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
from app import config, model_tier  # noqa: E402

# 官方价格文档：优先用 Markdown 纯净版（规整表格、token 更少、解析更稳）；
# 若 .md 不可用则回退到 HTML 页面再做表格抽取。两者均非 JSON API（火山暂无公开价格 REST API）。
PRICING_URL_MD = "https://ark.volcengine.com/region:cn-beijing/docs/model-pricing.md"
PRICING_URL_HTML = "https://ark.volcengine.com/region:cn-beijing/docs/model-pricing"

# 视频创作模型（Seedance）初始清单：首跑 LLM 未抽到时作为兜底，
# 之后由抓取结果覆盖。价格为 480p/720p「输入不含视频」在线推理代表价（元/百万token）。
DEFAULT_SEEDANCE = {
    "doubao-seedance-1.0-pro-fast": {"label": "Seedance 1.0 Pro Fast（快速）", "price_in": 4.20, "price_out": 2.10},
    "doubao-seedance-1.0-pro": {"label": "Seedance 1.0 Pro（影视级 1080p）", "price_in": 15.00, "price_out": 7.50},
    "doubao-seedance-2.0-mini": {"label": "Seedance 2.0 Mini（轻量）", "price_in": 23.00, "price_out": 14.00},
    "doubao-seedance-2.0-fast": {"label": "Seedance 2.0 Fast", "price_in": 37.00, "price_out": 22.00},
    "doubao-seedance-2.5": {"label": "Seedance 2.5（30s 超长叙事）", "price_in": 70.00, "price_out": 42.00},
}

TIER_LABELS = {"cheapest": "最便宜", "moderate": "适中", "high": "最新高性能"}


# ── 1. 抓取 + 表格文本化 ────────────────────────────────────────────────
class _TableExtractor(HTMLParser):
    """极简 table→二维数组抽取（仅用于为 LLM 准备上下文，非解析价格）。"""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._cur: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cap = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._cur, self._cap = [], True
        elif tag == "tr" and self._cap:
            self._row = []
        elif tag in ("td", "th") and self._cap:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._cap and self._row is not None:
            self._cur.append(self._row)
            self._row = None
        elif tag == "table" and self._cap:
            self.tables.append(self._cur)
            self._cur, self._cap = None, False


def _render_table(t: list[list[str]], max_cell: int = 240) -> str:
    rows = [[c[:max_cell] for c in r] for r in t if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = [" | ".join(rows[0])]
    out.append(" | ".join("---" for _ in range(width)))
    for r in rows[1:]:
        out.append(" | ".join(r))
    return "\n".join(out)


def _extract_tables_from_html(html: str) -> str:
    """HTML 兜底：抽取含 doubao 模型且有价格信息的表格，转为 Markdown。"""
    p = _TableExtractor()
    p.feed(html)
    kept = []
    for t in p.tables:
        flat = " ".join(" ".join(r) for r in t)
        has_model = bool(re.search(r"doubao[\w.\-]+", flat))
        has_price = ("元/百万" in flat) or ("/百万" in flat) or ("价格" in flat)
        if has_model and has_price and len(t) > 1:
            md = _render_table(t)
            if md:
                kept.append(md)
    text = "\n\n".join(kept)
    if not text.strip():
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
    return text


def fetch_pricing_text(url: str | None = None, timeout: int = 60) -> str:
    """抓取价格文本。优先 Markdown 纯净版；失败时回退 HTML 抽取。"""
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    urls = [url] if url else [PRICING_URL_MD, PRICING_URL_HTML]
    last_err = None
    for u in urls:
        try:
            r = httpx.get(u, timeout=timeout, follow_redirects=True, headers=headers)
            r.raise_for_status()
            text = r.text or ""
            # Markdown 版（或已含价格表的文本）直接作为上下文
            if u.endswith(".md") or ("元/百万" in text and "doubao" in text):
                return text
            # 否则按 HTML 抽取表格
            return _extract_tables_from_html(text)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"价格页抓取失败：{last_err}")


# ── 2. LLM 自然语言抽取价格 ────────────────────────────────────────────
_SYSTEM = (
    "你是火山方舟模型价格的抽取助手。用户会给你一段从官网价格页提取的 Markdown 表格文本，"
    "请从中识别所有【有明确 token 单价】的模型，并输出结构化 JSON 数组。"
    "不要输出任何解释或 markdown 代码块，只输出纯 JSON 数组。"
)

_USER_TMPL = """请从以下价格表中抽取所有模型，输出 JSON 数组，每个元素格式：
{{"model":"doubao-seed-2.1-lite","family":"doubao-seed","generation":"2.1","modality":"text","price_in":0.40,"price_out":1.35,"unit":"元/百万token","note":"在线推理标准价(输入长度≤1024)"}}

规则：
- model：模型 id，如 doubao-seed-2.1-lite、doubao-seedance-1.0-pro-fast、doubao-seedream-5-0-lite。
- family：doubao-seed（豆包 Seed 系列） / doubao-seedance（视频生成） / doubao-seedream（图像生成） / doubao（其它豆包系列）。
- generation：从 model 取主版本号，如 2.1、2.0、1.0、4.0。
- modality：text（语言/对话） / video（视频生成，Seedance） / image（图像，Seedream） / audio。
- price_in / price_out：在线推理标准价，单位元/百万token；若模型无独立输入价（如视频按输出计费），两值取相同代表价；确实无法取得数值则填 null。
- 文本模型只抽取【在线推理(常规)】表格（标准后付费口径，单位 元/百万token，输入/输出分开），取该模型在常规表里的代表档（一般为输入长度≤1024 或 [0,32] 档），note 注明所取档位；忽略 低延迟/低优/批量推理/精调 等其它表格，避免口径不一致与重复。
- 视频模型只抽取【视频生成模型】表格中的 Seedance 系列；分辨率区分定价时，取「输出 480p/720p、输入不含视频」的在线推理价作为 price_out 代表价，price_in 取相同值，note 注明分辨率；若表格对视频模型明确给出独立输入价（如 doubao-seedance-1.0-pro 给出 15.00 / 7.50），则分别填入对应 price_in / price_out。
- 所有价格单位统一为 元/百万token；遇到「元/千 token」等其它单位请换算成 元/百万token（×1000）后再填写，并在 note 注明。
- 只抽取含明确单价的模型，去除示例、计算器、免费额度、缓存存储等非价格行。
- 仅输出 JSON 数组，不要任何前后说明文字。

价格表如下：
{text}"""


def _extract_json_array(content: str) -> list:
    # 去掉可能的 ```json 围栏
    content = re.sub(r"```(?:json)?", "", content).strip()
    s = content.find("[")
    e = content.rfind("]")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("LLM 返回中未找到 JSON 数组")
    return json.loads(content[s : e + 1])


def extract_prices_with_llm(text: str, timeout: int = 60) -> list[dict]:
    import httpx

    model_id, api_key, base_url = model_tier.active_llm()
    if not api_key:
        raise RuntimeError("未配置 LLM_API_KEY，无法用 LLM 抽取价格")
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model_id,
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER_TMPL.format(text=text[:60000])},
            ],
        },
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json_array(msg)
    out = []
    for it in data:
        if not isinstance(it, dict) or not it.get("model"):
            continue
        out.append(it)
    return out


# ── 2b. 结构化兜底解析（不依赖 LLM，解析 .md 价格表，快速且确定）──────────
def _parse_md_section_rows(text: str, section_title: str) -> list[list[str]]:
    """从 .md 文本截取某 '## 标题' 段落，返回其中的表格行（已按 | 切分、去转义）。"""
    m = re.search(r"##\s*" + re.escape(section_title), text)
    if not m:
        return []
    start = m.end()
    end = text.find("\n## ", start)
    seg = text[start: end if end != -1 else len(text)]
    rows = []
    for line in seg.splitlines():
        line = line.strip()
        if line.startswith("|"):
            cells = [c.strip().rstrip("\\").strip() for c in line.strip("|").split("|")]
            rows.append(cells)
    return rows


def extract_prices_structured(text: str) -> list[dict]:
    """结构化兜底：直接解析 .md 的【在线推理(常规)】文本模型价格表。
    不调用 LLM，速度快、确定性强；用于 LLM 抽取失败（如代理超时）时的降级，
    保证三档更新不会因网络抖动而静默失败。视频模型此时沿用上一次结果。"""
    rows = _parse_md_section_rows(text, "在线推理（常规）")
    header = None
    data_start = 0
    for i, r in enumerate(rows):
        if r and "模型名称" in r[0]:
            header = r
            data_start = i + 2  # 跳过表头与分隔行
            break
    if not header:
        return []
    i_in = next((j for j, c in enumerate(header) if "输入(非音频)" in c or c.strip() == "输入"), None)
    i_out = next((j for j, c in enumerate(header) if c.strip().startswith("输出")), None)
    if i_in is None or i_out is None:
        return []
    out, seen = [], {}
    for r in rows[data_start:]:
        if len(r) <= i_out:
            continue
        model = r[0]
        if not model or model == "^^" or "---" in model:
            continue
        if not re.search(r"doubao|glm|kimi|deepseek|qwen|ernie|minimax|abab", model, re.I):
            continue
        pin, pout = _num(r[i_in]), _num(r[i_out])
        if pin is None or pout is None:
            continue
        if model in seen:  # 同一模型取首个（基础档）价位
            continue
        seen[model] = True
        out.append({
            "model": model,
            "family": _family(model),
            "generation": str(_gen(model)),
            "modality": "text",
            "price_in": pin,
            "price_out": pout,
            "unit": "元/百万token",
            "note": "在线推理(常规)基础档·结构化解析兜底",
        })
    return out


# ── 3. 由结构化价格计算三档 + 视频清单 ──────────────────────────────────
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _gen(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", model or "")
    return float(m.group(1)) if m else 0.0


def _family(model: str) -> str:
    m = (model or "").lower()
    if "seedance" in m:
        return "doubao-seedance"
    if "seedream" in m:
        return "doubao-seedream"
    if m.startswith("doubao-seed-"):
        return "doubao-seed"
    return "doubao"


def select_tiers(prices: list[dict]) -> dict:
    # 仅文本/对话类（含 Seed 与其它豆包系列），且输入/输出价均为数值
    text = [
        p
        for p in prices
        if _num(p.get("price_in")) is not None and _num(p.get("price_out")) is not None
        and (p.get("modality") in (None, "text") or _family(p.get("model")) in ("doubao-seed", "doubao"))
    ]

    def combined(p):
        return _num(p["price_in"]) + _num(p["price_out"])

    def info(p):
        return {
            "model": p["model"],
            "family": p.get("family") or _family(p["model"]),
            "generation": p.get("generation") or str(_gen(p["model"])),
            "price_in": _num(p["price_in"]),
            "price_out": _num(p["price_out"]),
        }

    # cheapest：跨代最低价（允许非 Seed）
    cheapest = min(text, key=combined) if text else None

    # moderate / high：限 Seed 系列
    seed = [p for p in text if _family(p["model"]) == "doubao-seed"]
    if not seed:
        seed = text
    combos = sorted(combined(p) for p in seed)
    median = combos[len(combos) // 2] if combos else 0
    moderate = min(seed, key=lambda p: abs(combined(p) - median)) if seed else None
    high = max(seed, key=lambda p: (_gen(p["model"]), combined(p))) if seed else None

    # Seedance 视频创作清单
    seedance = [p for p in prices if "seedance" in (p.get("model") or "").lower()]
    if not seedance:
        seedance = [
            {"model": k, "family": "doubao-seedance", "generation": str(_gen(k)),
             "modality": "video", "price_in": v["price_in"], "price_out": v["price_out"],
             "note": "初始兜底价"}
            for k, v in DEFAULT_SEEDANCE.items()
        ]
    video_models = {}
    for p in seedance:
        k = p["model"]
        video_models[k] = {
            "label": p.get("label") or DEFAULT_SEEDANCE.get(k, {}).get("label") or k,
            "price_in": _num(p.get("price_in")),
            "price_out": _num(p.get("price_out")),
            "needs_provisioning": True,
        }

    return {
        "cheapest": info(cheapest) if cheapest else None,
        "moderate": info(moderate) if moderate else None,
        "high": info(high) if high else None,
        "video_models": video_models,
        "all_text_count": len(text),
    }


# ── 4. 合并写回（保留已有端点/密钥）─────────────────────────────────────
def reconcile_and_write(selection: dict, source_url: str, now: str, dry_run: bool) -> dict:
    current = model_tier._load()
    provisioned = {
        t.get("model"): t
        for t in current.get("tiers", {}).values()
        if t.get("endpoint_id") and t.get("api_key")
    }

    new_tiers = {}
    for slot in ("cheapest", "moderate", "high"):
        rec = selection.get(slot)
        prev = current.get("tiers", {}).get(slot, {})
        if not rec:
            # 没抽到就沿用旧值
            new_tiers[slot] = prev
            continue
        m = rec["model"]
        bound = provisioned.get(m)
        new_tiers[slot] = {
            "label": TIER_LABELS.get(slot, slot),
            "model": m,
            "generation": rec.get("generation") or str(_gen(m)),
            "price_in": rec.get("price_in"),
            "price_out": rec.get("price_out"),
            "endpoint_id": (bound or {}).get("endpoint_id"),
            "api_key": (bound or {}).get("api_key"),
            "needs_provisioning": (bound is None),
        }

    # 视频模型：合并默认与抽取结果，保留 needs_provisioning 标记
    old_video = current.get("video", {}) or {}
    old_models = old_video.get("models", {}) or {}
    merged_video = dict(old_models)
    for k, v in selection.get("video_models", {}).items():
        merged_video[k] = v
    if not merged_video:
        merged_video = {k: {"label": v["label"], "price_in": v["price_in"],
                           "price_out": v["price_out"], "needs_provisioning": True}
                        for k, v in DEFAULT_SEEDANCE.items()}
    # 默认选中：沿用旧的；否则选最便宜的 Seedance
    sel_video = old_video.get("selected")
    if not sel_video or sel_video not in merged_video:
        sel_video = min(
            merged_video,
            key=lambda k: (merged_video[k].get("price_out") or 1e9),
        )

    out = {
        "selected": current.get("selected", "cheapest"),
        "last_updated": now,
        "source_url": source_url,
        "tiers": new_tiers,
        "video": {"selected": sel_video, "models": merged_video},
    }

    if dry_run:
        print("[dry-run] 将写入 model_tiers.json：")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        model_tier.save_full(out)
        # 同时落一份审计文件
        audit = {
            "last_updated": now,
            "source_url": source_url,
            "recommendation": {k: selection.get(k) for k in ("cheapest", "moderate", "high")},
            "video_models": selection.get("video_models"),
        }
        ap = config.DATA_DIR / "model_prices.json"
        ap.parent.mkdir(parents=True, exist_ok=True)
        tmp = ap.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ap)
    return out


# ── 陈旧度判定（按月份，不看具体天数）─────────────────────────────────
_refresh_lock = threading.Lock()


def _parse_month(ts: str | None):
    """从 ISO/YYYY-MM 时间戳取 (year, month)；无法解析返回 None。"""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt.year, dt.month)
    except Exception:
        m = re.search(r"(\d{4})-(\d{2})", ts or "")
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None


def is_stale(last_updated: str | None, now: datetime | None = None) -> bool:
    """价格数据是否超过一个月（只看月份是否与当前不一致）。
    last_updated 为空或无法解析 → 视为陈旧（需更新）。"""
    lm = _parse_month(last_updated)
    if lm is None:
        return True
    now = now or datetime.now(timezone(timedelta(hours=8)))
    return lm != (now.year, now.month)


def maybe_refresh(force: bool = False, dry_run: bool = False) -> dict | None:
    """按「月份陈旧度」决定是否更新三档与视频清单（对应需求：超过一个月才更新，非手动触发）。
    - force=True：忽略月份检查，强制执行（调试/CLI 用）。
    - 非 force 且数据仍是本月：跳过，返回 None。
    - 并发保护：已在更新中则跳过本次。"""
    if not force:
        cur = model_tier._load().get("last_updated")
        if not is_stale(cur):
            print(f"[price_monitor] 价格数据为本月已更新（last_updated={cur}），跳过。"
                  f"使用 --force 可强制刷新。")
            return None
    if not _refresh_lock.acquire(blocking=False):
        print("[price_monitor] 已有更新任务在运行，跳过本次。")
        return None
    try:
        return run_once(dry_run=dry_run)
    finally:
        _refresh_lock.release()


# ── 入口 ────────────────────────────────────────────────────────────────
def run_once(dry_run: bool = False) -> dict:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    print(f"[price_monitor] 抓取价格页：{PRICING_URL_MD}")
    text = fetch_pricing_text()
    print(f"[price_monitor] 页面表格文本化完成，长度 {len(text)} 字符")

    print("[price_monitor] 调用 LLM 做自然语言价格抽取 ...")
    try:
        prices = extract_prices_with_llm(text)
    except Exception as e:
        print(f"[price_monitor] LLM 抽取失败（{e}），改用结构化兜底解析 ...")
        prices = []
    if not prices:
        prices = extract_prices_structured(text)
    print(f"[price_monitor] 共抽取到 {len(prices)} 条模型价格")
    if not prices:
        raise RuntimeError("LLM 与结构化兜底均未抽取到价格，终止以免覆盖现有配置")

    selection = select_tiers(prices)
    print("[price_monitor] 三档推荐：")
    for slot in ("cheapest", "moderate", "high"):
        r = selection.get(slot)
        if r:
            print(f"  - {slot:9s} {r['model']:28s} 输入{r['price_in']}/输出{r['price_out']} 元·百万token")
    print(f"  - Seedance 视频模型 {len(selection['video_models'])} 个")

    out = reconcile_and_write(selection, PRICING_URL_MD, now, dry_run)
    print(f"[price_monitor] {'[dry-run] ' if dry_run else ''}完成，更新时间 {now}")
    return out


def main():
    ap = argparse.ArgumentParser(description="火山方舟模型价格监控与三档更新")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件（隐含 --force）")
    ap.add_argument("--force", action="store_true", help="忽略月份检查，强制更新")
    ap.add_argument("--once", action="store_true", help="（已废弃，等同默认：按月份陈旧度决定是否更新）")
    args = ap.parse_args()
    force = args.force or args.dry_run
    try:
        out = maybe_refresh(force=force, dry_run=args.dry_run)
        if out is None and not force:
            print("[price_monitor] 本月已更新，无需操作。")
    except Exception as e:
        print(f"[price_monitor] 失败：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
