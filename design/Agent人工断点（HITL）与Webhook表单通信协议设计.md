# Agent人工断点（HITL）与Webhook表单通信协议设计

**文档定位：**定义双Agent架构下，重度执行Agent运行过程中触发人工决策断点的完整技术机制，包括表单渲染原理、双向Webhook报文格式、双通路交互流程，是任务看板与AI自动填表功能的核心对接规范。

# 核心原理：Agent运行中弹出表单的底层实现

Agent执行过程中弹出选项、表单、输入框，本质是**大模型Function Calling（工具调用）\+ 应用层渲染**的组合机制，不是LLM原生生成UI。

## 两类实现路径

|实现方式|触发逻辑|适用场景|
|---|---|---|
|**LLM驱动HITL（本产品采用）**|LLM推理到无法自主决策的节点，输出结构化工具调用请求[[1]](https://platform.openai.com/docs/guides/function-calling)，调度层识别为人工输入请求后渲染表单|重度Agent自主推演过程中动态发现分支需要人工选择|
|**流程硬编码断点**|编排流程中提前埋入人工审批节点，走到节点即暂停等待人工输入|固定流水线，不适合开放式咨询推演场景|

## 标准交互流程

1. 重度Agent推理到决策节点，输出结构化的人工输入请求（而非自然语言提问）；

2. 调度层暂停当前Agent执行链路，保存执行上下文；

3. 将表单Schema下发至前端，动态渲染原生表单组件；

4. 人工填写并提交后，将答复数据注入Agent上下文；

5. Agent恢复执行，继续后续推演流程。

# Webhook双向通信报文规范

Webhook仅作为HTTP传输通道，不限制报文格式；表单描述完全由双方约定的JSON Schema定义，通过POST请求在系统间传递。

## 方向1：重度Agent → 调度服务（人工断点事件）

当重度Agent到达决策节点，通过Webhook POST推送断点报文到调度服务的回调地址，载荷格式如下：

```json
{
  "event_type": "human_breakpoint",
  "task_id": "t-20261101-001",
  "breakpoint_id": "bp-001",
  "reason": "分支假设无法自动判定，需要专家选择",
  "form_schema": {
    "title": "现金流测算情景选择",
    "description": "不同假设下IRR差异较大，请选定基准",
    "fields": [
      {
        "field_key": "scenario",
        "label": "情景选择",
        "type": "radio",
        "required": true,
        "options": [
          {"value":"base","label":"基准情景"},
          {"value":"bear","label":"下行保守情景"},
          {"value":"bull","label":"乐观弹性情景"}
        ]
      },
      {
        "field_key": "note",
        "label": "备注说明",
        "type": "text",
        "required": false,
        "placeholder": "写下选择理由"
      }
    ]
  },
  "meta": {
    "continue_endpoint": "https://agent-internal/resume",
    "cancel_endpoint": "https://agent-internal/cancel"
  }
}
```

### 字段说明

- `form_schema`：表单完整描述元数据，是前端动态渲染表单的唯一数据源；支持控件类型：radio单选、checkbox多选、number数字输入、text长文本、file文件上传、date日期选择；

- `continue_endpoint`：人工提交答复后，调度服务将POST表单数据到该地址，Agent恢复执行；

- `cancel_endpoint`：任务取消接口，用于终止正在运行的推演任务；

- `task_id / breakpoint_id`：全局唯一标识，保证多任务、多表单场景下的幂等匹配。

## 方向2：调度服务 → 重度Agent（人工答复回传）

人工通过任务看板手动提交，或表层AI自动填表后，调度服务POST结构化答复报文到meta\.continue\_endpoint，恢复Agent执行：

```json
{
  "task_id": "t-20261101-001",
  "breakpoint_id": "bp-001",
  "form_data": {
    "scenario": "bear",
    "note": "优先评估下行风险，符合本次尽调要求"
  },
  "submit_source": "panel_human"
}
```

### submit\_source枚举值（资产溯源标记）

|取值|含义|
|---|---|
|`panel_human`|人工在任务看板手动填写提交|
|`chat_agent`|用户在聊天框自然语言表达，表层Agent识别后自动填表提交|

## 配套事件类型

1. `task_finish`：任务完成事件，携带摘要、完整结果、附件URL；摘要推送至聊天流，完整结果在任务看板展示；

2. `task_error`：任务异常事件，携带错误信息、失败原因，自动标记任务状态；

3. `task_cancel`：任务取消指令，由主持人触发，发送至cancel\_endpoint终止执行。

# 双通路填表交互流程

## 通路1：任务看板手动填写（基础通路）

1. 重度Agent触发断点 → Webhook回调调度服务存储表单Schema；

2. 调度服务通过OpenIM推送轻量通知（仅带task\_id/breakpoint\_id）；

3. Flutter客户端收到通知后，任务Tab出现红点提醒；

4. 用户切换至任务看板，前端根据form\_schema动态渲染原生表单；

5. 用户手动填写后提交，调度服务转发form\_data到Agent的continue\_endpoint；

6. Agent恢复执行，完成后续推演。

## 通路2：对话AI自动填表（智能通路）

1. 断点生成、红点提醒与通路1完全一致；

2. 用户无需打开看板，直接在聊天框用自然语言表达决策（例：“我们选保守情景，优先控制下行风险”）；

3. 表层LLM识别当前房间存在待确认断点，触发`fill_breakpoint_form`工具调用；

4. 调度服务读取该断点的form\_schema，通过小模型将自然语言映射为合法的form\_data结构；

5. 校验必填项、枚举值合法性后，自动提交至Agent恢复接口；

6. 聊天流中输出确认消息：“已识别你的选择，自动提交断点【情景选择】：下行保守情景”；

7. 若自然语言模糊无法匹配Schema，自动降级提示用户前往任务看板手动填写。

# 关键约束与技术要求

1. **Agent状态持久化要求：**重度Agent抛出断点后必须挂起执行上下文，不能销毁会话；收到form\_data后必须基于原有上下文恢复执行，不能重新启动新会话，否则断点机制完全失效。

2. **大体积数据隔离：**form\_schema等结构化数据必须存在调度服务，IM通道仅传输轻量通知id，禁止将完整表单JSON塞入IM消息体，避免消息阻塞。

3. **幂等性保证：**所有断点提交必须以task\_id\+breakpoint\_id为幂等键，防止重复提交导致Agent重复执行。

4. **超时机制：**超过设定时间未收到人工答复的断点，自动标记为超时，支持主持人手动恢复或终止。

# 总结

本协议通过标准化的双向Webhook报文，将Agent内部的人工决策断点转化为前端可渲染的结构化表单，同时支持手动看板操作与AI自动填表双通路，既保证了重度Agent复杂推演过程中需要人工决策的灵活性，又通过与聊天流解耦的任务看板设计，保持了前台对话的流畅性，完美适配咨询业务“跑一步看一步、分步决策”的工作模式。

> （注：部分内容由豆包工作 AI 生成）
