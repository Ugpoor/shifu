# 协同推演陪跑系统 · Flutter 客户端

双人协同聊天面板 + 任务看板 + HITL 人工断点双通路（看板手动填表 / 对话AI自动填表）。

## 目录结构

```
lib/
├── main.dart                      入口（SCHEDULER_URL 可用 --dart-define 覆盖）
├── api/api_client.dart            Dio 统一封装：接口 / SSE流式 / 重试 / 拦截器
├── bridge/
│   ├── im_bridge.dart             ★ 统一抽象层（业务层永不直接调 OpenIM API）
│   ├── openim_bridge_impl.dart    OpenIM 开源SDK实现（生产底座）
│   └── local_bridge_impl.dart     本地降级实现（MVP联调，SSE事件流同构载荷）
├── models/models.dart             数据模型（与调度服务接口协议对齐）
├── widgets/dynamic_form.dart      ★ 通用 Schema 表单渲染器（一套代码适配全部断点表单）
└── pages/
    ├── login_page.dart            创建房间 / 凭ID进入（主持人 / 专家）
    ├── home_page.dart             Tab框架 + 红点通知 + 静默开关
    ├── chat_panel.dart            Tab1 协同聊天（SSE逐字打字机）
    └── board_panel.dart           Tab2 任务看板（状态标签/断点表单/结果/取消）
```

## 运行（MVP联调，配合本地调度服务 + 重度Agent模拟器）

```bash
# 1. 启动服务端（仓库根目录）
cd server && python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8300 &
cd heavy_agent && python3 run.py &

# 2. 启动 Flutter 客户端
cd flutter_app && flutter pub get
flutter run --dart-define=SCHEDULER_URL=http://127.0.0.1:8300
```

iOS 模拟器里 `127.0.0.1` 即本机；真机需改为电脑局域网 IP。

## 接入 OpenIM 私有化部署

1. 私有化部署 OpenIM Server（docker-compose 一键起）；
2. 在 `main.dart` 完成 `OpenIM.iMManager.initSDK(...)` 初始化；
3. `HomePage(bridge: OpenIMBridgeImpl(), ...)` 传入实现，其余业务代码零改动；
4. IM 通道仅承载：文本/图片/文件消息 + 轻量业务通知
   `{eventType, taskId, breakpointId, roomId}` —— 表单 Schema、任务详情等
   大体积业务数据一律由客户端主动 HTTP 拉取。

## 核心交互说明（对应 PRD）

- **通路A（看板手动填写）**：看板出现「待人工确认」橙色卡片 → 按 Schema 动态渲染表单 → 填写提交（submit_source=panel_human）；
- **通路B（对话AI自动填表）**：断点出现后直接在聊天框说「选保守情景，备注优先控制下行风险」→ 表层Agent 调用 fill_breakpoint_form 自动提交（submit_source=chat_agent）；表述模糊时自动降级提示前往看板手动填写；
- **红点**：Tab2 标签红点=待确认断点数量；聊天流不自动插入表单，保持对话干净；
- **权限**：仅主持人可开静默 / 提交任务 / 取消任务；专家可参与对话与填表。
