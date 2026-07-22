---
formatter: sql-guardrail
id: rear_screen_physical_type_name
name: 后排屏使用真实物理字段
enabled: true
version: 0.1.0
type: require_sql_contains
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets:
  - measure:config_rate
  intent_any:
  - 后排多媒体屏
  - 后排屏
params:
  contains: type_name = '后排多媒体屏幕数量'
action:
  type: rewrite
  message: 后排多媒体屏必须使用真实字段 type_name = '后排多媒体屏幕数量'。
updated_at: '2026-07-21 00:00:00'
---

# 后排屏使用真实物理字段

## 业务约束

EAV 表中的真实 `type_name` 为 `后排多媒体屏幕数量`，不得根据自然语言猜成 `后排多媒体屏`。

## 推荐处理

由 Generator 使用 Schema/Profile 证据重新生成 SQL，Agent 不得手改 SQL。
