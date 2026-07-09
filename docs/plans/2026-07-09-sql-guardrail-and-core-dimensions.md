# SQL Guardrail 与核心维度固化

## 背景

`vehicle_params` 是 EAV 风格配置明细表。用户查询“2026 年上市的纯电新车中，空气悬架配备率是多少”时，Vanna 容易生成：

- `EXISTS` / `NOT EXISTS` 多次自关联 `vehicle_params`
- `SELECT DISTINCT car_name`
- `COUNT(DISTINCT ...)`

这类 SQL 即使语义正确，也容易在大表上超时。短期需要 SQL guardrail 止血，长期仍需要事实表 / 物化视图 / 索引优化。

## 目标

- [x] 在 Vanna prompt 中加入 EAV 配置率高性能模板约束。
- [x] 后端检测慢 SQL 反模式，拦截后自动要求 Vanna 重写一次。
- [x] 若重写后仍冲突，阻断执行并返回生成 SQL。
- [x] 固化核心维度：上市时间、能源类型、车型级别、价格段、品牌。
- [x] 增加回归测试，覆盖慢 SQL guardrail。

## 第一版边界

- 不创建事实表、物化视图或索引。
- 不做完整 SQL AST 优化器。
- 不保证所有慢 SQL 自动变快，只针对 `vehicle_params` 配置率高频反模式做硬拦截和一次重写。

## Guardrail 规则

命中 `配置率` 语义资产时，如果 SQL 同时满足：

- 引用 `vehicle_params`
- 包含 `COUNT(DISTINCT ...)`
- 包含多个 `EXISTS` / `NOT EXISTS`
- 以 `DISTINCT car_name` 构造候选集合

则认为命中慢 SQL 反模式。

推荐改写模板：

```sql
WITH car_flags AS (
  SELECT
    car_name,
    BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_2026_launch,
    BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_ev,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
    BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS has_air_suspension
  FROM vehicle_params
  WHERE car_name IS NOT NULL
    AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
  GROUP BY car_name
)
SELECT ...
FROM car_flags;
```

## 验收

- [x] `backend/tests/test_database_semantic_guardrails.py` 通过。
- [x] `backend/tests/test_semantic_assets_registry.py` 通过。
- [x] `backend/analytics/nl2sql/service.py` 可编译。
- [x] 语义资产 registry 能扫描到新增品牌维度。

## 验证记录

- `backend/.venv/bin/pytest backend/tests/test_database_semantic_guardrails.py backend/tests/test_semantic_assets_registry.py -q`
  - 结果：`10 passed`
- `backend/.venv/bin/python -m py_compile backend/analytics/nl2sql/service.py backend/analytics/semantic_assets/registry.py`
  - 结果：通过
- registry 手动检查：
  - `dimension:launch_time`
  - `dimension:energy_type`
  - `dimension:vehicle_level`
  - `dimension:price_band`
  - `dimension:brand`
