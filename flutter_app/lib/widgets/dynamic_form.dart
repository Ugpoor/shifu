import 'package:flutter/material.dart';

/// 通用 Schema 表单渲染器（前端自研，无第三方依赖）。
///
/// 一套代码适配全部断点表单：后端下发 form_schema，自动渲染
/// radio / checkbox / number / text / textarea 控件，
/// 支持字段校验、必填判断、枚举限制、备注扩展。
class DynamicSchemaForm extends StatefulWidget {
  final Map<String, dynamic> schema; // FormSchemaModel 的原始 JSON
  final Future<void> Function(Map<String, dynamic> formData) onSubmit;

  const DynamicSchemaForm({super.key, required this.schema, required this.onSubmit});

  @override
  State<DynamicSchemaForm> createState() => _DynamicSchemaFormState();
}

class _DynamicSchemaFormState extends State<DynamicSchemaForm> {
  final Map<String, dynamic> _data = {};
  String? _error;

  List<Map<String, dynamic>> get _fields =>
      ((widget.schema['fields'] ?? []) as List).cast<Map<String, dynamic>>();

  bool _valid() {
    for (final f in _fields) {
      final v = _data[f['field_key']];
      final required = f['required'] ?? false;
      if (required && (v == null || v == '' || (v is List && v.isEmpty))) {
        setState(() => _error = '缺少必填字段「${f['label']}」');
        return false;
      }
      if (v != null && f['options'] != null) {
        final legal = (f['options'] as List).map((o) => o['value']).toSet();
        final vs = v is List ? v : [v];
        if (!vs.every(legal.contains)) {
          setState(() => _error = '字段「${f['label']}」取值不合法');
          return false;
        }
      }
    }
    return true;
  }

  Future<void> _submit() async {
    if (!_valid()) return;
    setState(() => _error = null);
    try {
      await widget.onSubmit(_data);
    } catch (e) {
      setState(() => _error = '提交失败：$e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      if (widget.schema['title'] != null)
        Text(widget.schema['title'],
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
      if (widget.schema['description'] != null)
        Padding(
          padding: const EdgeInsets.only(top: 4, bottom: 8),
          child: Text(widget.schema['description'],
              style: TextStyle(color: Colors.grey.shade600, fontSize: 13)),
        ),
      ..._fields.map(_renderField),
      if (_error != null)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
        ),
      Row(children: [
        FilledButton(onPressed: _submit, child: const Text('提交答复')),
      ]),
    ]);
  }

  Widget _renderField(Map<String, dynamic> f) {
    final key = f['field_key'];
    final type = f['type'];
    final required = f['required'] ?? false;
    final label = RichText(
      text: TextSpan(
        style: DefaultTextStyle.of(context).style,
        children: [
          TextSpan(text: f['label']),
          if (required) const TextSpan(text: ' *', style: TextStyle(color: Colors.red)),
        ],
      ),
    );

    if (type == 'radio') {
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          label,
          ...(f['options'] as List).map((o) => RadioListTile<String>(
                dense: true,
                title: Text(o['label']),
                value: o['value'],
                groupValue: _data[key],
                onChanged: (v) => setState(() => _data[key] = v),
              )),
        ]),
      );
    }
    if (type == 'checkbox') {
      final selected = (_data[key] as List?) ?? [];
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          label,
          ...(f['options'] as List).map((o) => CheckboxListTile(
                dense: true,
                title: Text(o['label']),
                value: selected.contains(o['value']),
                onChanged: (on) => setState(() {
                  if (on == true) {
                    selected.add(o['value']);
                  } else {
                    selected.remove(o['value']);
                  }
                  _data[key] = selected;
                }),
              )),
        ]),
      );
    }
    if (type == 'number') {
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          label,
          TextField(
            keyboardType: TextInputType.number,
            decoration: InputDecoration(hintText: f['placeholder'], isDense: true),
            onChanged: (v) => _data[key] = num.tryParse(v),
          ),
        ]),
      );
    }
    if (type == 'textarea') {
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          label,
          TextField(
            maxLines: 3,
            decoration: InputDecoration(hintText: f['placeholder'], isDense: true),
            onChanged: (v) => _data[key] = v,
          ),
        ]),
      );
    }
    // text
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        label,
        TextField(
          decoration: InputDecoration(hintText: f['placeholder'], isDense: true),
          onChanged: (v) => _data[key] = v,
        ),
      ]),
    );
  }
}
