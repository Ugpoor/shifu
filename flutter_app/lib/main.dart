import 'package:flutter/material.dart';

import 'api/api_client.dart';
import 'pages/login_page.dart';

/// 协同推演陪跑系统 · Flutter客户端入口。
///
/// 生产环境切换 IM 底座：私有化部署 OpenIM Server 后，
/// 将 ImBridge 实现从 LocalBridgeImpl 切到 OpenIMBridgeImpl
/// （在 HomePage 构造时传入 bridge 参数即可，业务代码零改动）。
const String kSchedulerBaseUrl = String.fromEnvironment(
  'SCHEDULER_URL',
  defaultValue: 'http://127.0.0.1:8300',
);

void main() {
  runApp(const HitlCoachApp());
}

class HitlCoachApp extends StatelessWidget {
  const HitlCoachApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '协同推演陪跑系统',
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF3370FF),
        useMaterial3: true,
      ),
      home: LoginPage(baseUrl: kSchedulerBaseUrl),
    );
  }
}

// ApiClient 单例可按需持有；LoginPage 每次进入房间会构建独立实例。
ApiClient buildApi(String baseUrl) => ApiClient(baseUrl: baseUrl);
