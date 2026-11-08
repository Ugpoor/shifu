import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/models.dart';

/// Tab1 协同聊天面板：SSE 流式逐字输出（打字机效果）、静默模式提示。
class ChatPanel extends StatefulWidget {
  final ApiClient api;
  final String roomId;
  final String memberId;
  final String memberName;
  final bool isHost;
  final ValueChanged<bool>? onSilentChanged;

  const ChatPanel({
    super.key,
    required this.api,
    required this.roomId,
    required this.memberId,
    required this.memberName,
    required this.isHost,
    this.onSilentChanged,
  });

  @override
  State<ChatPanel> createState() => ChatPanelState();
}

class ChatPanelState extends State<ChatPanel> {
  final _listCtrl = ScrollController();
  final _inputCtrl = TextEditingController();
  final List<MessageModel> _messages = [];
  String _streaming = '';
  bool _sending = false;

  @override
  void initState() {
    super.initState();
    reloadHistory();
  }

  Future<void> reloadHistory() async {
    final msgs = await widget.api.messages(widget.roomId);
    if (mounted) {
      setState(() => _messages
        ..clear()
        ..addAll(msgs));
      _scrollBottom();
    }
  }

  void _scrollBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_listCtrl.hasClients) {
        _listCtrl.animateTo(_listCtrl.position.maxScrollExtent + 80,
            duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
      }
    });
  }

  Future<void> _send() async {
    final content = _inputCtrl.text.trim();
    if (content.isEmpty || _sending) return;
    _inputCtrl.clear();
    setState(() {
      _sending = true;
      _streaming = '';
    });
    try {
      await widget.api.chat(widget.roomId, content, (ev) {
        final type = ev['type'];
        if (type == 'user_message') {
          setState(() =>
              _messages.add(MessageModel.fromJson(ev['message'])));
        } else if (type == 'token') {
          setState(() => _streaming += (ev['content'] ?? '') as String);
          _scrollBottom();
        } else if (type == 'silent_skipped') {
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text(ev['message'] ?? 'Agent静默模式')));
          }
        } else if (type == 'agent_message') {
          setState(() {
            _messages.add(MessageModel.fromJson(ev['message']));
            _streaming = '';
          });
        } else if (type == 'error') {
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text('错误：${ev['message']}')));
          }
        }
        _scrollBottom();
      });
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('发送失败：$e')));
      }
    } finally {
      setState(() {
        _sending = false;
        _streaming = '';
      });
      await reloadHistory();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      Expanded(
        child: ListView.builder(
          controller: _listCtrl,
          padding: const EdgeInsets.all(12),
          itemCount: _messages.length + (_streaming.isEmpty ? 0 : 1),
          itemBuilder: (context, i) {
            if (i == _messages.length) {
              return _bubble(const MessageModel(
                msgId: 'streaming',
                roomId: '',
                memberId: 'surface-agent',
                role: 'agent',
                content: _streamingPlaceholder,
                createdAt: 0,
              ));
            }
            return _bubble(_messages[i]);
          },
        ),
      ),
      SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Row(children: [
            Expanded(
              child: TextField(
                controller: _inputCtrl,
                minLines: 1,
                maxLines: 4,
                decoration: const InputDecoration(
                  hintText: '输入消息…（断点出现时可直接说：选保守情景，备注优先控制下行风险）',
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                onSubmitted: (_) => _send(),
              ),
            ),
            const SizedBox(width: 8),
            IconButton.filled(
              onPressed: _sending ? null : _send,
              icon: const Icon(Icons.send),
            ),
          ]),
        ),
      ),
    ]);
  }

  Widget _bubble(MessageModel m) {
    final isMe = m.memberId == widget.memberId;
    final isAgent = m.role == 'agent';
    final content = m.msgId == 'streaming' ? _streaming : m.content;
    return Align(
      alignment: isMe ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        constraints: BoxConstraints(
            maxWidth: MediaQuery.of(context).size.width * 0.78),
        decoration: BoxDecoration(
          color: isMe
              ? const Color(0xFF3370FF)
              : isAgent
                  ? const Color(0xFFEEF4FF)
                  : Colors.white,
          border: isMe ? null : Border.all(color: const Color(0xFFE5E6EB)),
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(10),
            topRight: const Radius.circular(10),
            bottomLeft: Radius.circular(isMe ? 10 : 2),
            bottomRight: Radius.circular(isMe ? 2 : 10),
          ),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
            isMe ? widget.memberName : (isAgent ? '表层Agent' : m.memberId),
            style: TextStyle(
                fontSize: 11,
                color: isMe ? Colors.white70 : Colors.grey.shade500),
          ),
          const SizedBox(height: 2),
          SelectableText(
            content,
            style: TextStyle(
                fontSize: 14, color: isMe ? Colors.white : Colors.black87),
          ),
        ]),
      ),
    );
  }
}

const _streamingPlaceholder = '…';
