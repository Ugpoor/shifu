"""全局配置。

设计原则（对应技术文档）：
- 中转调度服务是任务/断点/房间状态的唯一数据源；
- IM（OpenIM）只承载轻量业务通知，不承载任何业务状态；
- 工作AI代理 通过标准化 Webhook 协议对接，可整体替换。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
try:
    DATA_DIR.mkdir(exist_ok=True)
except OSError:  # 目录已存在（某些沙箱/容器环境下 mkdir 会回抛 EEXIST）
    pass


# ── 免依赖 .env 加载 ───────────────────────────────────────────────
# 在引入任何配置项之前调用；仅在「系统环境变量未设置」时才用 .env 填充，
# 因此真实环境变量永远优先于 .env，且无需安装 python-dotenv。
def _load_dotenv(path: Path | None = None) -> None:
    p = path or (BASE_DIR.parent / ".env")  # 项目根目录 .env
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
    print(f"[config] 已从 {p} 加载环境变量")


_load_dotenv()


# SQLite 持久化（对话、任务、断点、记忆资产、事件日志）
DB_PATH = os.environ.get("APP_DB_PATH", str(DATA_DIR / "app.db"))

# ── 数据库备份（结构 + 全量；代码修订回滚安全网的一侧）────────────────
# 备份存放目录：结构(.schema.sql) 与 全量(.db) 成对存放，并附 .json 元信息。
DB_BACKUP_DIR = os.environ.get("DB_BACKUP_DIR", str(DATA_DIR / "db_backups"))
# 保留的备份份数（全量 + 结构合并计算，超出则按时间 FIFO 删除最旧、最旧优先）。
# 默认 20；被「收藏」(pinned) 的备份永不被自动清理。
DB_BACKUP_KEEP = int(os.environ.get("DB_BACKUP_KEEP", "20"))
# 每日自动全量备份（后台任务，随服务启动）。false 关闭。
DB_BACKUP_AUTODAILY = (os.environ.get("DB_BACKUP_AUTODAILY", "true").lower() == "true")

# 服务自身对外地址（用于拼接回调地址等）
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8300")

# ── 工作 AI 代理底座（可插拔：换底座只改这里）──────────────────────────
# 任务下发 Webhook（调度 → 工作AI代理）
HEAVY_AGENT_TASK_ENDPOINT = os.environ.get(
    "HEAVY_AGENT_TASK_ENDPOINT", "http://127.0.0.1:8310/agent/tasks"
)
# Webhook 下发失败重试次数（PRD 非功能需求：失败重试3次，超时标记失败）
WEBHOOK_RETRY_TIMES = int(os.environ.get("WEBHOOK_RETRY_TIMES", "3"))
WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "5.0"))

# ── 代码修订 Agent（按主题路由的可插拔底座）──────────────────────────
# 当 room.theme 命中 CODE_AGENT_THEME 时，任务改发往代码修订 Agent（而非通用底座）。
# 该 Agent 通过同机 CLI 调起服务器端 WorkBuddy agent（Linux），对代码仓库做真实修订。
CODE_AGENT_THEME = os.environ.get("CODE_AGENT_THEME", "师傅来了-核心修订讨论")
CODE_AGENT_TASK_ENDPOINT = os.environ.get(
    "CODE_AGENT_TASK_ENDPOINT", "http://127.0.0.1:8312/agent/tasks"
)
# WorkBuddy agent CLI 调用模板（同机直接调用，subprocess 调起）。
# 支持占位符：{prompt}=任务指令，{repo}=目标仓库绝对路径。
WORKBUDDY_CLI = os.environ.get("WORKBUDDY_CLI", "")
WORKBUDDY_CLI_ARGS = os.environ.get(
    "WORKBUDDY_CLI_ARGS", "run {prompt} --cwd {repo}"
)
# 默认编辑的仓库绝对路径（也可由任务的 context_package.repo_path 覆盖）。
CODE_AGENT_REPO = os.environ.get("CODE_AGENT_REPO", "")
# 代码修订任务单次执行超时（秒），超时判失败。
CODE_AGENT_TIMEOUT = float(os.environ.get("CODE_AGENT_TIMEOUT", "600.0"))
# 代码修订 Agent 回传事件的目标（指向本调度服务的 webhook 网关）。
CODE_AGENT_EVENTS_URL = os.environ.get(
    "CODE_AGENT_EVENTS_URL", "http://127.0.0.1:8300/internal/webhooks/agent-events"
)
CODE_AGENT_BASE_URL = os.environ.get("CODE_AGENT_BASE_URL", "http://127.0.0.1:8312")
# 回滚指令下发地址（调度 -> 代码修订 Agent）。{task_id} 占位。
CODE_AGENT_ROLLBACK_ENDPOINT = os.environ.get(
    "CODE_AGENT_ROLLBACK_ENDPOINT",
    f"{CODE_AGENT_BASE_URL}/agent/tasks/{{task_id}}/rollback")

# ── 代码修订：版本保存与回滚（安全网）───────────────────────────
# 是否自动在目标仓库初始化 git（仅当仓库尚不是 git 仓库时）。
CODE_AGENT_INIT_GIT = (os.environ.get("CODE_AGENT_INIT_GIT", "false").lower() == "true")
# 每次修订成功后是否提交为一个版本（git: 打 commit；file: 写版本清单）。
CODE_AGENT_COMMIT = (os.environ.get("CODE_AGENT_COMMIT", "true").lower() == "true")
# 是否启用文件级快照备份（无 git 时的回滚兜底方案）。
CODE_AGENT_BACKUP_ENABLED = (os.environ.get("CODE_AGENT_BACKUP_ENABLED", "true").lower() == "true")
# 快照备份根目录（相对仓库）。每个任务在 <root>/pre-<task_id>/ 下存一份快照。
CODE_AGENT_BACKUP_ROOT = os.environ.get("CODE_AGENT_BACKUP_ROOT", ".revision-backups")
# 保留的备份份数（超出则清理最旧）。
CODE_AGENT_KEEP_BACKUPS = int(os.environ.get("CODE_AGENT_KEEP_BACKUPS", "20"))

# ── 会议 AI 代理 LLM（可插拔）──────────────────────────────────────────
# 默认使用内置 Mock（零依赖可演示）；配置 LLM_API_KEY 后自动切换为
# OpenAI 兼容接口（chat/completions + tools function call）。
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# SSE 首 token 延迟预算（PRD：2s 内），Mock 模式下的打字间隔
SSE_TOKEN_INTERVAL = float(os.environ.get("SSE_TOKEN_INTERVAL", "0.02"))

# ── 微信社会化登录（OAuth2）──────────────────────────────────────────
# 网站应用（snsapi_login，授权后直出 nickname/headimgurl）或
# 公众号网页授权（snsapi_userinfo，需再用 access_token 拉用户信息）二选一。
# 未配置 WECHAT_APPID 时自动进入「演示模式」：授权直接回跳到 callback?code=mock，
# 无需真实公众号即可联调登录态、头像与昵称获取流程。
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_APPSECRET = os.environ.get("WECHAT_APPSECRET", "")
WECHAT_SCOPE = os.environ.get("WECHAT_SCOPE", "snsapi_login")
# 服务端回调地址：微信要求与开放平台登记完全一致（含协议、域名、路径）。
WECHAT_REDIRECT_URI = os.environ.get("WECHAT_REDIRECT_URI",
                                     f"{PUBLIC_BASE_URL}/auth/wechat/callback")
# 无 AppID 即视为演示模式（避免误调真实微信网关）
WECHAT_MOCK = (WECHAT_APPID == "")

# ── 短信验证码（演示用，无真实短信网关）─────────────────────────────
# 开启后「发送验证码」接口会在响应里回显 dev_code，便于本地联调；
# 生产环境设 SMS_DEV_MODE=0 即可隐藏验证码（但仍需走验证流程）。
SMS_DEV_MODE = (os.environ.get("SMS_DEV_MODE", "1") == "1")
