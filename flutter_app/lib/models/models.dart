/// 数据模型：与中转调度服务的接口协议一一对应。
library;

/// 断点表单 Schema（服务端下发，动态渲染器据此渲染UI）
class FormFieldModel {
  final String fieldKey;
  final String label;
  final String type; // radio | checkbox | number | text | textarea
  final bool required;
  final List<Option> options;
  final String? placeholder;

  FormFieldModel({
    required this.fieldKey,
    required this.label,
    required this.type,
    this.required = false,
    this.options = const [],
    this.placeholder,
  });

  factory FormFieldModel.fromJson(Map<String, dynamic> j) => FormFieldModel(
        fieldKey: j['field_key'],
        label: j['label'],
        type: j['type'],
        required: j['required'] ?? false,
        options: ((j['options'] ?? []) as List)
            .map((o) => Option.fromJson(o))
            .toList(),
        placeholder: j['placeholder'],
      );
}

class Option {
  final String value;
  final String label;
  Option({required this.value, required this.label});

  factory Option.fromJson(Map<String, dynamic> j) =>
      Option(value: j['value'], label: j['label']);
}

class FormSchemaModel {
  final String title;
  final String? description;
  final List<FormFieldModel> fields;

  FormSchemaModel({required this.title, this.description, required this.fields});

  factory FormSchemaModel.fromJson(Map<String, dynamic> j) => FormSchemaModel(
        title: j['title'] ?? '',
        description: j['description'],
        fields: ((j['fields'] ?? []) as List)
            .map((f) => FormFieldModel.fromJson(f))
            .toList(),
      );
}

/// 任务（状态唯一数据源：中转调度服务）
class TaskModel {
  final String taskId;
  final String roomId;
  final String summary;
  final String status; // queued|running|waiting_human|finished|canceled|failed
  final String? resultSummary;
  final String? resultFull;
  final List<Map<String, dynamic>> attachments;
  final String? errorMessage;
  final num createdAt;

  TaskModel({
    required this.taskId,
    required this.roomId,
    required this.summary,
    required this.status,
    this.resultSummary,
    this.resultFull,
    this.attachments = const [],
    this.errorMessage,
    required this.createdAt,
  });

  factory TaskModel.fromJson(Map<String, dynamic> j) => TaskModel(
        taskId: j['task_id'],
        roomId: j['room_id'],
        summary: j['summary'] ?? '',
        status: j['status'],
        resultSummary: j['result_summary'],
        resultFull: j['result_full'],
        attachments: ((j['attachments'] ?? []) as List)
            .whereType<Map<String, dynamic>>()
            .toList(),
        errorMessage: j['error_message'],
        createdAt: j['created_at'] ?? 0,
      );

  static const statusText = {
    'queued': '排队中',
    'running': '运行中',
    'waiting_human': '待人工确认',
    'finished': '已完成',
    'failed': '已失败',
    'canceled': '已取消',
  };
  String get statusLabel => statusText[status] ?? status;
}

/// 断点
class BreakpointModel {
  final String breakpointId;
  final String taskId;
  final String roomId;
  final String reason;
  final FormSchemaModel formSchema;
  final String status; // pending | answered | canceled
  final Map<String, dynamic>? answerFormData;
  final String? answerSubmitSource; // panel_human | chat_agent

  /// 原始 Schema JSON（供动态表单渲染器使用）
  Map<String, dynamic> get formSchemaJson => _formSchemaJson;
  Map<String, dynamic> _formSchemaJson;

  BreakpointModel({
    required this.breakpointId,
    required this.taskId,
    required this.roomId,
    required this.reason,
    required this.formSchema,
    required this.status,
    this.answerFormData,
    this.answerSubmitSource,
    Map<String, dynamic>? rawSchema,
  }) : _formSchemaJson = rawSchema ?? {};

  factory BreakpointModel.fromJson(Map<String, dynamic> j) => BreakpointModel(
        breakpointId: j['breakpoint_id'],
        taskId: j['task_id'],
        roomId: j['room_id'],
        reason: j['reason'] ?? '',
        formSchema: FormSchemaModel.fromJson(j['form_schema'] ?? {}),
        status: j['status'],
        answerFormData: j['answer_form_data'],
        answerSubmitSource: j['answer_submit_source'],
        rawSchema: (j['form_schema'] ?? {}) as Map<String, dynamic>,
      );
}

/// 聊天消息
class MessageModel {
  final String msgId;
  final String roomId;
  final String memberId;
  final String role; // host | expert | agent
  final String content;
  final String msgType;
  final num createdAt;

  MessageModel({
    required this.msgId,
    required this.roomId,
    required this.memberId,
    required this.role,
    required this.content,
    this.msgType = 'text',
    required this.createdAt,
  });

  factory MessageModel.fromJson(Map<String, dynamic> j) => MessageModel(
        msgId: j['msg_id'],
        roomId: j['room_id'],
        memberId: j['member_id'],
        role: j['role'],
        content: j['content'],
        msgType: j['msg_type'] ?? 'text',
        createdAt: j['created_at'] ?? 0,
      );
}

/// 成员（角色权限）
class MemberModel {
  final String memberId;
  final String roomId;
  final String name;
  final String role; // host | expert

  MemberModel({
    required this.memberId,
    required this.roomId,
    required this.name,
    required this.role,
  });

  factory MemberModel.fromJson(Map<String, dynamic> j) => MemberModel(
        memberId: j['member_id'],
        roomId: j['room_id'],
        name: j['name'],
        role: j['role'],
      );

  bool get isHost => role == 'host';
}

/// 轻量业务通知（IM 自定义消息同构载荷：只传 id 信号，完整数据主动拉取）
class BusinessNotifyModel {
  final String eventType;
  final String? taskId;
  final String? breakpointId;
  final String roomId;
  final Map<String, dynamic> raw;

  BusinessNotifyModel({
    required this.eventType,
    this.taskId,
    this.breakpointId,
    required this.roomId,
    required this.raw,
  });

  factory BusinessNotifyModel.fromJson(Map<String, dynamic> j) =>
      BusinessNotifyModel(
        eventType: j['eventType'] ?? '',
        taskId: j['taskId'],
        breakpointId: j['breakpointId'],
        roomId: j['roomId'] ?? '',
        raw: j,
      );
}
