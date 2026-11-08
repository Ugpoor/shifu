"""Pydantic 请求/响应模型（事件协议与 PRD 模块3 保持一字不差的字段对齐）。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SubmitSource = Literal["panel_human", "chat_agent"]
Role = Literal["host", "expert"]


# ── 房间 ──────────────────────────────────────────────────────────
class CreateRoomReq(BaseModel):
    title: str = Field(..., examples=["某项目投资测算咨询"])
    host_name: str = Field("咨询师", examples=["王咨询师"])
    expert_name: str = Field("行业专家", examples=["李专家"])
    account_id: Optional[str] = None  # 可选：登录用户作为主持人时绑定账号（member_id 复用 account_id）
    theme: Optional[str] = None       # 可选：目录式分组的一级主题（如「投资测算」「青少年编程」）
    tags: list[str] = []              # 可选：主题标签，用于跨主题模糊筛选
    category: str = ""                # 可选：固定类目（''=普通话题 / 'software_revision'=软件修订）
    task_template: Optional[str] = None  # 可选：房间默认任务卡模板（软件修订类目默认 software_revision）


# ── 账号 / 登录态 ─────────────────────────────────────────────────
class LoginReq(BaseModel):
    phone: str
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None


class UpdateProfileReq(BaseModel):
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None
    phone: Optional[str] = None


class SmsSendReq(BaseModel):
    """发送手机验证码（演示模式会在响应回显 dev_code）。"""
    phone: str


class SmsVerifyReq(BaseModel):
    """校验手机验证码。"""
    phone: str
    code: str


class SilentReq(BaseModel):
    silent: bool


class RenameReq(BaseModel):
    title: str = Field(..., min_length=1, examples=["修订后的话题标题"])


class RoomMetaReq(BaseModel):
    """话题的「主题与主题标签」元数据（目录式分组 + 跨主题筛选）。"""
    theme: Optional[str] = None
    tags: list[str] = []


# ── 对话 ──────────────────────────────────────────────────────────
class ChatReq(BaseModel):
    member_id: str = Field(..., description="发言人身份（host/expert 二选一）")
    content: str = Field(..., min_length=1)


# ── 断点事件（工作AI代理 → 调度，PRD 事件2）─────────────────────────
class FormField(BaseModel):
    field_key: str
    label: str
    type: Literal["radio", "checkbox", "number", "text", "textarea"]
    required: bool = False
    options: Optional[list[dict[str, str]]] = None
    placeholder: Optional[str] = None


class FormSchema(BaseModel):
    title: str
    description: Optional[str] = None
    fields: list[FormField]


class BreakpointMeta(BaseModel):
    continue_endpoint: str
    cancel_endpoint: str


class HeavyBreakpointEvent(BaseModel):
    event_type: Literal["human_breakpoint"]
    task_id: str
    breakpoint_id: Optional[str] = None      # 由工作AI代理给定；缺省时调度侧生成
    reason: Optional[str] = None
    form_schema: FormSchema
    meta: BreakpointMeta


class HeavyFinishEvent(BaseModel):
    event_type: Literal["task_finish"]
    task_id: str
    summary: str
    result: Optional[str] = None
    attachments: Optional[list[dict[str, str]]] = None
    meta: Optional[dict[str, Any]] = None  # 版本元数据：基线/版本号/变更清单等


class HeavyRollbackEvent(BaseModel):
    """代码修订回滚事件（调度服务 -> 任务看板，标记已回滚）。"""
    event_type: Literal["task_rollback"]
    task_id: str
    by: Optional[str] = None
    note: Optional[str] = None


class HeavyErrorEvent(BaseModel):
    event_type: Literal["task_error"]
    task_id: str
    error_message: str


class HeavyProgressEvent(BaseModel):
    """工作AI 推演过程中的进度回传（事件4，可选多段）。

    - thinking：思维链/推理过程信息（详情区，全部保留并打时间戳）；
    - content：非思维链的实际回复文字（摘要区）；
    - step：可选的阶段标记。
    调度服务按 kind 拆成两条进度记录分别存储，用于面板「详情/摘要」分区展示与未读标记。
    """
    event_type: Literal["task_progress"]
    task_id: str
    thinking: Optional[str] = None
    content: Optional[str] = None
    step: Optional[str] = None


# ── 断点答复（统一报文，PRD 事件3）────────────────────────────────
class BreakpointAnswerReq(BaseModel):
    form_data: dict[str, Any]
    submit_source: SubmitSource = "panel_human"


# ── 会议AI代理 Function 入参（PRD 模块1）───────────────────────────
class SubmitHeavyTaskArgs(BaseModel):
    room_id: str
    summary: str
    constraints: list[str] = []
    discussion_history: str = ""
    deliver_requirement: str = ""


class FillBreakpointFormArgs(BaseModel):
    room_id: str
    breakpoint_id: Optional[str] = None       # 缺省时取房间唯一 pending 断点
    user_statement: str


class FillResult(BaseModel):
    ok: bool
    breakpoint_id: Optional[str] = None
    form_data: Optional[dict[str, Any]] = None
    message: str = ""


# ── 手动创建 / 更新任务卡（软件修订类目用）──────────────────────────
class CreateTaskReq(BaseModel):
    """主持人手动创建任务卡（如软件修订需求卡）：输入需求，按房间模板落卡。"""
    summary: str = Field(..., min_length=1, description="任务卡标题/摘要")
    requirement: Optional[str] = None   # 需求描述（软件修订模板的核心输入）
    template: Optional[str] = None      # 任务卡模板（缺省继承房间默认模板）
    stage: Optional[str] = None         # 初始进程状态（缺省 需求整理）
    agent_kind: Optional[str] = None    # 底座类型（缺省由房间类目决定：软件修订→code）


class UpdateTaskReq(BaseModel):
    """更新任务卡（仅主持人）：推进进程状态、修订需求/摘要。"""
    stage: Optional[str] = None
    requirement: Optional[str] = None
    summary: Optional[str] = None
