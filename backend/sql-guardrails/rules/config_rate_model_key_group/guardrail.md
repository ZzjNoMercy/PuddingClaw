---
formatter: sql-guardrail
id: config_rate_model_key_group
name: 配置率款型颗粒度分组
enabled: true
version: 0.1.0
type: require_group_by
scope:
  table_scope:
    mode: any
    values:
    - vehicle_params
  semantic_assets:
  - measure:config_rate
  intent_any:
  - 配置率
  - 搭载率
  - 渗透率
  - 配备率
  - 占比
params:
  forbidden_columns_only:
  - car_name
action:
  type: rewrite
  message: 默认款型颗粒度必须按 brand + serial_name + car_name 分组。
updated_at: '2026-07-09 15:46:40'
---

# 配置率款型颗粒度分组

## 业务约束

默认款型颗粒度必须按 brand + serial_name + car_name 分组。

## 命中范围

- 表范围：any / vehicle_params
- 语义资产：measure:config_rate

## Detector 参数

```yaml
forbidden_columns_only:
- car_name
```

## 推荐处理

默认款型颗粒度必须按 brand + serial_name + car_name 分组。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
