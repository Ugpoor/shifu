# 协同推演陪跑系统（MVP）

> 咨询师 + 行业专家双人协同：先对齐共识、再异步下发重型推演任务；重度Agent中途抛出结构化表单断点（HITL），支持「看板手动填写」与「对话AI自动填表」双答复通路；任务状态机 + 结构化记忆资产沉淀。

**架构**：表层轻量交互Agent（核心资产层）+ 重度执行Agent（可替换算力底座）+ 中转调度服务（唯一状态源）+ Webhook通信层。

```
师傅来了/
├── design/              权威文档：PRD / 技术实现文档 / HITL与Webhook协议 / 图标与视觉规范
├── server/               中转调度服务 + 表层轻量Agent服务（FastAPI, 端口8300）
│   ├── app/
│   │   ├── config.py             全局配置（LLM/重度Agent端点可插拔）
│   │   ├── db.py                 SQLite持久化（对话/任务/断点/记忆/日志，房间隔离）
│   │   ├── models.py             事件协议（与PRD模块3报文一字不差对齐）
│   │   ├── events.py             轻量业务通知广播（与OpenIM自定义消息同构载荷）
│   │   ├── dispatcher.py         Webhook下发（失败重试3次）
│   │   ├── functions.py          ★ submit_heavy_task / fill_breakpoint_form
│   │   ├── memory_extractor.py   结构化记忆抽取（约束/假设/共识/验收/风险，版本快照）
│   │   ├── surface_agent.py      ★ SSE流式对话编排（Mock引擎/OpenAI兼容LLM可切换）
│   │   └── routers/              rooms / chat / tasks / webhooks / memory
│   └── static/index.html         浏览器演示客户端（与Flutter端同构）
├── heavy_agent/run.py    重度Agent模拟器（端口8310，标准Webhook协议，可整体替换）
├── flutter_app/          Flutter客户端工程（ImBridge抽象层/OpenIM SDK实现/动态表单渲染器）
└── tests/e2e_test.py     端到端验收（覆盖PRD七条上线验收标准，21项断言）
```

## 快速启动

```bash
# 1. 安装依赖（仅首次）
pip install -r server/requirements.txt

# 2. 启动重度Agent模拟器（8310）
cd heavy_agent && python3 run.py

# 3. 启动中转调度服务（8300，含浏览器演示客户端）
cd server && python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8300

# 4. 打开浏览器演示客户端
open http://127.0.0.1:8300
# 「创建新房间」→ 复制专家ID → 换浏览器身份用「协作专家」进入同一房间

# 5. 跑端到端验收
python3 tests/e2e_test.py        # HITL双通路 21 项断言
python3 tests/test_meeting_modes.py  # 会议发言规则 27 项断言
```

## 会议流程 · Agent 发言规则（设置Tab）

设置页为「会议流程设置」，按话题独立配置（仅主持人可改）：

| 发言规则 | 行为 |
|---|---|
| 🔁 轮询所有人 | 任何人发言 Agent 都按序回应（默认） |
| 🎤 只回应主持人 | 嘉宾发言只留痕不打断 |
| 🧑‍💼 只回应嘉宾 | 主持人发言作为旁听记录 |
| 📋 罗伯特议事法则 | 一轮内每人只发言一次；全员说完后 Agent 归集各方立场并**统一回应一次**，随后自动进入下一轮 |

罗伯特议事参数：**发言撤回窗口**（n 分钟内可撤回重说，前一条自动标记「已撤回」；0=不可撤回）、**主持人动议直通**（下发任务/断点填表等程序性动议不受轮次限制）、「结束本轮」手动收口。

相关接口：`GET/POST /rooms/{id}/flow-settings`、`GET /rooms/{id}/meeting-state`、`POST /rooms/{id}/meeting-state/reset`。

## 端到端演示脚本（双通路HITL）

1. 主持人聊天：`我们这次要测算项目的IRR，约束是不能用高杠杆情景`
2. 主持人：`共识已对齐，提交任务，开始推演` → 任务下发重度Agent
3. 约2秒后重度Agent抛出断点 → 看板Tab红点 + 橙色「现金流测算情景选择」表单
4. **通路A**：去任务看板手动选「下行保守情景」提交 → 任务续跑至完成
5. 再次提交任务，断点出现后**不去看板**，直接在聊天框说：`我们选保守情景，备注优先控制下行风险`
   → 表层Agent自动填表提交（留痕 submit_source=chat_agent）→ 任务续跑至完成
6. 试试模糊表述：`我觉得都行，你们看着办` → 自动降级提示「请前往任务看板手动填写」
7. 主持人可随时「取消任务」；完成任务的结果/附件/执行日志在看板回看

## 接真实 LLM

默认使用内置 Mock 引擎（零依赖可演示）。配置环境变量后自动切换 OpenAI 兼容接口（含 function call）：

```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1   # 或任意兼容网关
export LLM_MODEL=gpt-4o-mini
```

## 替换重度Agent底座

只需实现双向 Webhook 协议（见 `design/` 目录下的《双Agent分层协同推演系统_-_技术实现文档（OpenIM开源SDK版）》）：

- 被调用：`POST /agent/tasks`（任务下发，含完整 context_package）
- 回调调度：`human_breakpoint`（携带form_schema+resume/cancel端点）/ `task_finish` / `task_error`
- 断点答复转发：调度 → `continue_endpoint`；取消指令 → `cancel_endpoint`

然后修改环境变量 `HEAVY_AGENT_TASK_ENDPOINT` 指向新底座。表层、看板、记忆资产层完全不动。

## OpenIM 接入（生产IM底座）

MVP 阶段人类消息/轻量通知走 HTTP+SSE（与 OpenIM 自定义消息同构的载荷）。
私有化部署 OpenIM Server 后，Flutter 端把 `ImBridge` 实现从
`LocalBridgeImpl` 切换为 `OpenIMBridgeImpl` 即可，业务代码零改动
（见 `flutter_app/README.md`）。

## 状态机与幂等

`queued → running → waiting_human → finished / canceled / failed`
- 唯一数据源：中转调度服务；客户端与IM仅做展示；
- 同一 breakpoint_id 重复回调幂等忽略；断点仅 pending 可答复（重复提交 409）；
- Webhook 下发失败重试 3 次，超时标记任务失败。
