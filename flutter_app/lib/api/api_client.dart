import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';

import '../models/models.dart';

/// 调度服务 API 客户端（Dio 统一封装：接口、重试、超时、拦截器）。
class ApiClient {
  final Dio dio;
  String? memberId;

  ApiClient({required String baseUrl})
      : dio = Dio(BaseOptions(
          baseUrl: baseUrl,
          connectTimeout: const Duration(seconds: 10),
          receiveTimeout: const Duration(minutes: 5),
        )) {
    dio.interceptors.add(InterceptorsWrapper(
      onRequest: (options, handler) {
        if (memberId != null) options.headers['X-Member-Id'] = memberId;
        handler.next(options);
      },
    ));
  }

  // ── 房间 ──
  Future<Map<String, dynamic>> createRoom(
      String title, String hostName, String expertName) async {
    final r = await dio.post('/rooms',
        data: {'title': title, 'host_name': hostName, 'expert_name': expertName});
    return r.data;
  }

  Future<Map<String, dynamic>> getRoom(String roomId) async {
    final r = await dio.get('/rooms/$roomId');
    return r.data;
  }

  Future<void> setSilent(String roomId, bool silent) =>
      dio.post('/rooms/$roomId/silent', data: {'silent': silent});

  // ── 消息 ──
  Future<List<MessageModel>> messages(String roomId) async {
    final r = await dio.get('/rooms/$roomId/messages');
    return (r.data as List).map((m) => MessageModel.fromJson(m)).toList();
  }

  /// 发送消息并消费表层Agent SSE 流。
  /// [onEvent] 回调依次收到 user_message / token / tool_call / agent_message /
  /// memory_snapshot / done / silent_skipped / error 事件。
  Future<void> chat(String roomId, String content,
      void Function(Map<String, dynamic>) onEvent) async {
    final resp = await dio.post<ResponseBody>(
      '/rooms/$roomId/chat',
      data: {'member_id': memberId, 'content': content},
      options: Options(responseType: ResponseType.stream),
    );
    final buf = StringBuffer();
    await for (final chunk in resp.data!.stream) {
      buf.write(utf8.decode(chunk));
      final s = buf.toString();
      while (s.contains('\n\n')) {
        final idx = s.indexOf('\n\n');
        final block = s.substring(0, idx);
        buf.clear();
        buf.write(s.substring(idx + 2));
        if (block.startsWith('data: ')) {
          onEvent(jsonDecode(block.substring(6)) as Map<String, dynamic>);
        }
      }
    }
  }

  // ── 看板 ──
  Future<List<TaskModel>> tasks(String roomId) async {
    final r = await dio.get('/rooms/$roomId/tasks');
    return (r.data as List).map((t) => TaskModel.fromJson(t)).toList();
  }

  Future<List<BreakpointModel>> breakpoints(String roomId,
      {String? status}) async {
    final r = await dio.get('/rooms/$roomId/breakpoints',
        queryParameters: status == null ? null : {'status': status});
    return (r.data as List).map((b) => BreakpointModel.fromJson(b)).toList();
  }

  Future<Map<String, dynamic>> taskDetail(String taskId) async {
    final r = await dio.get('/tasks/$taskId');
    return r.data;
  }

  /// 通路A：看板手动填写提交（submit_source=panel_human）。
  Future<void> answerBreakpoint(String breakpointId, Map<String, dynamic> formData) =>
      dio.post('/breakpoints/$breakpointId/answer',
          data: {'form_data': formData, 'submit_source': 'panel_human'});

  Future<void> cancelTask(String taskId) =>
      dio.post('/tasks/$taskId/cancel', data: {});

  // ── 记忆资产 ──
  Future<List<Map<String, dynamic>>> memorySnapshots(String roomId) async {
    final r = await dio.get('/rooms/$roomId/memory/snapshots');
    return (r.data as List).cast<Map<String, dynamic>>();
  }

  Future<Map<String, dynamic>> extractMemory(String roomId) async {
    final r = await dio.post('/rooms/$roomId/memory/snapshots', data: {});
    return r.data;
  }
}
