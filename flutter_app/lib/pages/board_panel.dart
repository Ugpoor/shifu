import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../widgets/dynamic_form.dart';

/// Tab2 任务看板：任务列表（状态标签）、待人工确认断点动态表单、
/// 任务详情（结果摘要/完整结果/附件/执行日志）、主持人取消任务。
class BoardPanel extends StatefulWidget {
  final ApiClient api;
  final String roomId;
  final bool isHost;

  const BoardPanel({
    super.key,
    required this.api,
    required this.roomId,
    required this.isHost,
  });

  @override
  State<BoardPanel> createState() => BoardPanelState();
}

class BoardPanelState extends State<BoardPanel> {
  List<TaskModel> _tasks = [];
  List<BreakpointModel> _pendingBps = [];
  List<BreakpointModel> _answeredBps = [];

  @override
  void initState() {
    super.initState();
    refresh();
  }

  Future<void> refresh() async {
    final tasks = await widget.api.tasks(widget.roomId);
    final pending = await widget.api.breakpoints(widget.roomId, status: 'pending');
    final answered = await widget.api.breakpoints(widget.roomId, status: 'answered');
    if (mounted) {
      setState(() {
        _tasks = tasks;
        _pendingBps = pending;
        _answeredBps = answered;
      });
    }
  }

  Color _statusColor(String s) => switch (s) {
        'queued' => Colors.grey,
        'running' => const Color(0xFF3370FF),
        'waiting_human' => const Color(0xFFFF7D00),
        'finished' => const Color(0xFF00B42A),
        'failed' => const Color(0xFFF53F3F),
        _ => Colors.grey,
      };

  Future<void> _cancel(TaskModel t) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('取消任务'),
        content: Text('确认取消任务「${t.summary}」？'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('否')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('确认取消')),
        ],
      ),
    );
    if (ok == true) {
      try {
        await widget.api.cancelTask(t.taskId);
        await refresh();
      } catch (e) {
        if (mounted) {
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text(e.toString())));
        }
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_tasks.isEmpty) {
      return Center(
        child: Text('暂无任务。主持人可在聊天框说「提交任务，开始推演」',
            style: TextStyle(color: Colors.grey.shade500)),
      );
    }
    return RefreshIndicator(
      onRefresh: refresh,
      child: ListView.builder(
        padding: const EdgeInsets.all(12),
        itemCount: _tasks.length,
        itemBuilder: (context, i) {
          final t = _tasks[i];
          final bps = _pendingBps.where((b) => b.taskId == t.taskId).toList();
          final answered =
              _answeredBps.where((b) => b.taskId == t.taskId).toList();
          return Card(
            margin: const EdgeInsets.only(bottom: 12),
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                    decoration: BoxDecoration(
                        color: _statusColor(t.status),
                        borderRadius: BorderRadius.circular(4)),
                    child: Text(t.statusLabel,
                        style: const TextStyle(color: Colors.white, fontSize: 11)),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(t.summary,
                        style: const TextStyle(fontWeight: FontWeight.bold)),
                  ),
                ]),
                const SizedBox(height: 4),
                Text('${t.taskId} · ${DateTime.fromMillisecondsSinceEpoch((t.createdAt * 1000).toInt())}',
                    style: TextStyle(fontSize: 12, color: Colors.grey.shade500)),
                // 待人工确认：动态表单渲染
                ...bps.map((b) => Padding(
                      padding: const EdgeInsets.only(top: 10),
                      child: Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                            color: const Color(0xFFFFF7E6),
                            borderRadius: BorderRadius.circular(8)),
                        child: Column(children: [
                          Align(
                            alignment: Alignment.centerLeft,
                            child: Text('⏸ ${b.reason}',
                                style: const TextStyle(
                                    color: Color(0xFFFF7D00), fontSize: 13)),
                          ),
                          const SizedBox(height: 8),
                          DynamicSchemaForm(
                            schema: b.formSchemaJson,
                            onSubmit: (formData) async {
                              await widget.api
                                  .answerBreakpoint(b.breakpointId, formData);
                              if (mounted) {
                                ScaffoldMessenger.of(context).showSnackBar(
                                    const SnackBar(
                                        content: Text('已提交，重度Agent恢复推演')));
                              }
                              await refresh();
                            },
                          ),
                        ]),
                      ),
                    )),
                // 已答复断点回显（含 submit_source 溯源）
                ...answered.map((b) => Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        '断点已答复（${b.answerSubmitSource == 'chat_agent' ? '对话AI自动填表' : '看板手动填写'}）：${b.answerFormData}',
                        style:
                            TextStyle(fontSize: 12, color: Colors.grey.shade600),
                      ),
                    )),
                if (t.resultSummary != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                          color: const Color(0xFFF7F8FA),
                          borderRadius: BorderRadius.circular(8)),
                      child: Text(
                        '📌 ${t.resultSummary}\n${t.resultFull ?? ''}'
                        '${t.attachments.isNotEmpty ? '\n附件：${t.attachments.map((a) => a['name']).join('、')}' : ''}',
                        style: const TextStyle(fontSize: 13),
                      ),
                    ),
                  ),
                if (t.errorMessage != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text('❌ ${t.errorMessage}',
                        style:
                            const TextStyle(color: Color(0xFFF53F3F), fontSize: 13)),
                  ),
                if (widget.isHost &&
                    t.status != 'finished' &&
                    t.status != 'canceled')
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Align(
                      alignment: Alignment.centerRight,
                      child: OutlinedButton(
                        style: OutlinedButton.styleFrom(
                            foregroundColor: const Color(0xFFF53F3F)),
                        onPressed: () => _cancel(t),
                        child: const Text('取消任务'),
                      ),
                    ),
                  ),
              ]),
            ),
          );
        },
      ),
    );
  }
}
