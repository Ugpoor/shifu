import 'package:flutter/material.dart';

import '../api/api_client.dart';
import 'home_page.dart';

/// 登录/进入房间页：创建房间 或 凭 room_id + 身份进入。
class LoginPage extends StatefulWidget {
  final String baseUrl;
  const LoginPage({super.key, required this.baseUrl});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _roomCtrl = TextEditingController();
  final _nameCtrl = TextEditingController();
  String _role = 'host';
  String? _createdInfo;
  late final ApiClient api = ApiClient(baseUrl: widget.baseUrl);

  Future<void> _createRoom() async {
    final name = _nameCtrl.text.trim().isEmpty ? '王咨询师' : _nameCtrl.text.trim();
    final d = await api.createRoom('投资测算协同咨询', name, '李专家');
    setState(() {
      _createdInfo =
          '房间 ${d['room_id']}\n主持人ID: ${d['host']['member_id']}\n专家ID: ${d['expert']['member_id']}';
    });
  }

  Future<void> _enter() async {
    final roomId = _roomCtrl.text.trim();
    if (roomId.isEmpty) return;
    final room = await api.getRoom(roomId);
    final members = (room['members'] as List).cast<Map<String, dynamic>>();
    final me = members.firstWhere((m) => m['role'] == _role);
    final myApi = ApiClient(baseUrl: widget.baseUrl);
    myApi.memberId = me['member_id'];
    if (!mounted) return;
    Navigator.pushReplacement(
      context,
      MaterialPageRoute(
        builder: (_) => HomePage(
          api: myApi,
          roomId: roomId,
          memberId: me['member_id'],
          memberName: me['name'],
          role: _role,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('协同推演陪跑系统')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: ListView(children: [
          TextField(
            controller: _roomCtrl,
            decoration: const InputDecoration(
                labelText: '已有房间ID（留空则先创建）', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            value: _role,
            decoration: const InputDecoration(
                labelText: '我的身份', border: OutlineInputBorder()),
            items: const [
              DropdownMenuItem(value: 'host', child: Text('主持人（咨询师）')),
              DropdownMenuItem(value: 'expert', child: Text('协作专家')),
            ],
            onChanged: (v) => setState(() => _role = v ?? 'host'),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _nameCtrl,
            decoration: const InputDecoration(
                labelText: '显示名', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 24),
          Row(children: [
            Expanded(
              child: FilledButton(onPressed: _enter, child: const Text('进入房间')),
            ),
            const SizedBox(width: 12),
            Expanded(child: OutlinedButton(onPressed: _createRoom, child: const Text('创建新房间'))),
          ]),
          if (_createdInfo != null)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: SelectableText(_createdInfo!,
                  style: const TextStyle(fontSize: 13)),
            ),
        ]),
      ),
    );
  }
}
