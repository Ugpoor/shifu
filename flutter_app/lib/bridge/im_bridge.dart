/// ImBridge 统一抽象层（技术文档核心解耦层）。
///
/// 业务层永不直接调用 OpenIM 原生API——所有底层SDK调用只发生在
/// OpenIMBridgeImpl 内部；切换 Matrix 等其他IM底座时仅需新增实现类。
abstract class ImBridge {
  // ── 基础消息能力 ──
  Future<void> sendRoomText(String roomId, String content);
  Future<void> sendRoomImage(String roomId, dynamic imageFile);
  Future<void> sendRoomFile(String roomId, dynamic file);

  // ── 业务事件通知（核心）：只承载轻量 id 信号 ──
  Future<void> sendBusinessNotify(String roomId, Map<String, dynamic> event);

  // ── 事件监听 ──
  Stream<BusinessNotifyModel> listenBusinessNotify();
  Stream<MessageModel> listenNewMessage();

  Future<void> init();
  Future<void> login(String userId, String token);
  Future<void> logout();
}
