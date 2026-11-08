import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';

import '../models/models.dart';

/// 本地降级 ImBridge 实现（MVP 联调通道，无 OpenIM Server 依赖）。
///
/// 与 OpenIMBridgeImpl 实现同一抽象接口：业务通知走房间 SSE 事件流
/// （载荷与 OpenIM 自定义消息同构），消息收发走调度服务 HTTP 接口。
/// 生产环境切换为 OpenIMBridgeImpl 即可，业务代码零改动。
class LocalBridgeImpl implements ImBridge {
  final Dio dio;
  final String baseUrl;
  late StreamController<BusinessNotifyModel> _notifyCtrl;
  StreamSubscription<BusinessNotifyModel>? _esSub;

  LocalBridgeImpl({required this.baseUrl})
      : dio = Dio(BaseOptions(
          baseUrl: baseUrl,
          connectTimeout: const Duration(seconds: 10),
          receiveTimeout: const Duration(minutes: 5),
        )) {
    _notifyCtrl = StreamController<BusinessNotifyModel>.broadcast();
  }

  /// 订阅房间业务事件流（SSE）。MVP 用 dio ResponseType.stream 解析。
  void subscribeRoomEvents(String roomId) {
    _esSub?.cancel();
    dio
        .get<ResponseBody>('/rooms/$roomId/events',
            options: Options(responseType: ResponseType.stream))
        .then((resp) {
      final lines = Stream<List<int>>.fromIterable([]);
      // dart:io 不引入时用简单逐行解析：ResponseBody 为字节流
      resp.data!.stream
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen((line) {
        if (line.startsWith('data: ')) {
          try {
            final json = jsonDecode(line.substring(6)) as Map<String, dynamic>;
            if (json['eventType'] != 'connected') {
              _notifyCtrl.add(BusinessNotifyModel.fromJson(json));
            }
          } catch (_) {}
        }
      });
      // ignore: unused_local_variable
    });
    // ignore: dead_code
    lines;
  }

  @override
  Future<void> init() async {}

  @override
  Future<void> login(String userId, String token) async {}

  @override
  Future<void> logout() async {}

  @override
  Future<void> sendRoomText(String roomId, String content) async {
    // MVP：人类文本消息经聊天SSE接口发送（与表层Agent响应同流），
    // 历史落地后由 listenNewMessage 的上层实现统一刷新。
  }

  @override
  Future<void> sendRoomImage(String roomId, dynamic imageFile) async {
    throw UnimplementedError('MVP 图片走 OpenIMBridgeImpl');
  }

  @override
  Future<void> sendRoomFile(String roomId, dynamic file) async {
    throw UnimplementedError('MVP 文件走 OpenIMBridgeImpl');
  }

  @override
  Future<void> sendBusinessNotify(
      String roomId, Map<String, dynamic> event) async {
    await dio.post('/debug/heartbeat', data: event);
  }

  @override
  Stream<BusinessNotifyModel> listenBusinessNotify() => _notifyCtrl.stream;

  @override
  Stream<MessageModel> listenNewMessage() => const Stream.empty();

  void dispose() {
    _esSub?.cancel();
    _notifyCtrl.close();
  }
}
