import 'dart:async';

import 'package:flutter_openim_sdk/flutter_openim_sdk.dart';

import '../models/models.dart';
import 'im_bridge.dart';

/// OpenIM 开源SDK实现（生产IM底座）。
///
/// 严格约束（技术文档「OpenIM 自定义消息规范」）：
/// - 表单Schema、任务详情、断点描述绝不放入IM消息（禁止大体积JSON）；
/// - 业务通知固定轻量载荷 {eventType, taskId, breakpointId, roomId}；
/// - 客户端收到通知后主动 HTTP 拉取完整业务数据（由上层完成）。
///
/// 使用前置条件：私有化部署 OpenIM Server，并在 main.dart 中
/// `await OpenIM.iMManager.initSDK(...)` 完成初始化。
class OpenIMBridgeImpl implements ImBridge {
  final String customMessageType = 'business_notify';
  final _notifyCtrl = StreamController<BusinessNotifyModel>.broadcast();
  final _msgCtrl = StreamController<MessageModel>.broadcast();

  @override
  Future<void> init() async {
    OpenIM.iMManager.addListener(_onListener);
  }

  void _onListener(dynamic method, dynamic data) {
    if (method == ListenerType.onRecvNewMessage) {
      final msg = Message.fromJson(data);
      if (msg.customType == customMessageType) {
        // 业务轻量通知
        try {
          final payload =
              Map<String, dynamic>.from(msg.customEx ?? {})..['roomId'] = msg.sendID;
          _notifyCtrl.add(BusinessNotifyModel.fromJson(payload));
        } catch (_) {}
      } else {
        _msgCtrl.add(MessageModel(
          msgId: msg.clientMsgID ?? '',
          roomId: msg.recvID ?? '',
          memberId: msg.sendID ?? '',
          role: 'expert',
          content: msg.textElem?.content ?? '',
          createdAt: msg.sendTime ?? 0,
        ));
      }
    }
  }

  @override
  Future<void> login(String userId, String token) =>
      OpenIM.iMManager.login(userID: userId, token: token);

  @override
  Future<void> logout() => OpenIM.iMManager.logout();

  @override
  Future<void> sendRoomText(String roomId, String content) =>
      OpenIM.iMManager.sendMessage(
        Message.createTextMessage(text: content),
        userID: roomId,
      );

  @override
  Future<void> sendRoomImage(String roomId, dynamic imageFile) =>
      OpenIM.iMManager.sendMessage(
        Message.createImageMessageByImgPath(imagePath: imageFile as String),
        userID: roomId,
      );

  @override
  Future<void> sendRoomFile(String roomId, dynamic file) =>
      OpenIM.iMManager.sendMessage(
        Message.createFileMessage(filePath: file as String),
        userID: roomId,
      );

  @override
  Future<void> sendBusinessNotify(
      String roomId, Map<String, dynamic> event) async {
    // 轻量信号，绝不放大体积业务JSON
    await OpenIM.iMManager.sendMessage(
      Message.createCustomMessage(data: event, extension: '', description: ''),
      userID: roomId,
    );
  }

  @override
  Stream<BusinessNotifyModel> listenBusinessNotify() => _notifyCtrl.stream;

  @override
  Stream<MessageModel> listenNewMessage() => _msgCtrl.stream;
}
