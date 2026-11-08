import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../bridge/im_bridge.dart';
import '../bridge/local_bridge_impl.dart';
import '../models/models.dart';
import 'board_panel.dart';
import 'chat_panel.dart';

/// 主框架：Tab1 协同聊天面板 / Tab2 任务看板面板。
/// 红点提示：Tab2 标签出现红点代表有待确认断点；聊天流不自动插入表单。
class HomePage extends StatefulWidget {
  final ApiClient api;
  final String roomId;
  final String memberId;
  final String memberName;
  final String role;
  final ImBridge? bridge;

  const HomePage({
    super.key,
    required this.api,
    required this.roomId,
    required this.memberId,
    required this.memberName,
    required this.role,
    this.bridge,
  });

  @override
  State<HomePage> createState() => HomePageState();
}

class HomePageState extends State<HomePage> {
  int _tab = 0;
  int _pendingCount = 0;
  ImBridge? _bridge;
  final GlobalKey<ChatPanelState> chatKey = GlobalKey<ChatPanelState>();
  final GlobalKey<BoardPanelState> boardKey = GlobalKey<BoardPanelState>();

  ImBridge get bridge => _bridge ??= (widget.bridge ??
      LocalBridgeImpl(baseUrl: widget.api.dio.options.baseUrl));

  bool get isHost => widget.role == 'host';

  @override
  void initState() {
    super.initState();
    _initBridge();
  }

  Future<void> _initBridge() async {
    await bridge.init();
    if (bridge is LocalBridgeImpl) {
      (bridge as LocalBridgeImpl).subscribeRoomEvents(widget.roomId);
    }
    bridge.listenBusinessNotify().listen((n) => _onBusinessNotify(n));
  }

  /// 轻量信号 → 主动拉取完整业务数据（状态源唯一在调度服务）。
  void _onBusinessNotify(BusinessNotifyModel n) {
    if (!mounted) return;
    if (n.eventType == 'human_breakpoint') {
      setState(() => _pendingCount += 1);
      boardKey.currentState?.refresh();
    } else if (n.eventType == 'breakpoint_answered' ||
        n.eventType == 'task_status_changed' ||
        n.eventType == 'task_created') {
      boardKey.currentState?.refresh();
      if (n.eventType == 'breakpoint_answered') refreshPending();
      chatKey.currentState?.reloadHistory();
    } else if (n.eventType == 'message_new') {
      chatKey.currentState?.reloadHistory();
    }
  }

  Future<void> refreshPending() async {
    final bps = await widget.api.breakpoints(widget.roomId, status: 'pending');
    if (mounted) setState(() => _pendingCount = bps.length);
  }

  void switchTab(int i) => setState(() => _tab = i);

  @override
  Widget build(BuildContext context) {
    final chatPanel = ChatPanel(
      key: chatKey,
      api: widget.api,
      roomId: widget.roomId,
      memberId: widget.memberId,
      memberName: widget.memberName,
      isHost: isHost,
      onSilentChanged: (_) {},
    );
    final boardPanel = BoardPanel(
      key: boardKey,
      api: widget.api,
      roomId: widget.roomId,
      isHost: isHost,
    );
    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.memberName}（${isHost ? '主持人' : '专家'}）'),
        actions: [
          if (isHost)
            IconButton(
              tooltip: 'Agent静默开关',
              onPressed: () async {
                final room = await widget.api.getRoom(widget.roomId);
                final cur = room['agent_silent'] == 1;
                await widget.api.setSilent(widget.roomId, !cur);
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                      content: Text(!cur ? '已开启静默：专家发言不触发表层Agent' : '已关闭静默')));
                }
              },
              icon: const Icon(Icons.notifications_off_outlined),
            ),
        ],
      ),
      body: IndexedStack(index: _tab, children: [chatPanel, boardPanel]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) {
          switchTab(i);
          if (i == 1) {
            refreshPending();
            boardKey.currentState?.refresh();
          }
        },
        destinations: [
          const NavigationDestination(
              icon: Icon(Icons.chat_bubble_outline), label: '协同聊天'),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: _pendingCount > 0,
              label: Text('$_pendingCount'),
              child: const Icon(Icons.kanban_outlined),
            ),
            label: '任务看板',
          ),
        ],
      ),
    );
  }
}
