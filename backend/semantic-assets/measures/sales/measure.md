---
formatter: semantic-asset
name: 销量
type: measure
description: 通过读取本地数据库的Excel/CSV，利用Pandas聚合统计汽车销量
aliases:
- 上险量
- 汽车销量
tags: ['销量']
version: 0.1.0
created: '2026-07-09 15:51:13'
updated_at: '2026-07-09 15:51:13'
---

# 销量

## 类型

度量值

## 业务口径

通过读取本地数据库的Excel/CSV，利用Pandas聚合统计汽车销量

## References

如某些业务对象存在专用识别规则，请放在本度量值目录的 `references/` 下。

分析过程中，命中本度量值后必须继续检索 `references/`，并优先遵守匹配 reference。

## 查询规则

- 明确需要使用的字段或 type_name 口径。
- 明确禁止从名称猜测字段含义。
- 如需分组、筛选或去重，在这里写清楚。

## SQL Hint

```sql
-- 可选：写入 SQL 片段或字段映射提示。
```
