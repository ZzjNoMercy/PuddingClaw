# 维度创建方式实施计划

> 状态：已完成（第一版）  
> 范围：统一维度分类、创建方式、机器可读定义、Registry、前端创建体验和 Agent 注入兼容。

## 目标

维度在用户视角保持一个分类；差异由 `resolution_mode` 表达，而不是把车系、时间、价格等拆成互不相干的产品入口。

`resolution_mode` 描述“值怎样得到”。语义资产目录只保存业务语义与构建完成的内容，例如 `dimension.md`、`bindings/`、`entities.jsonl`、`references/`；多表清洗、增量构建和中间表生成由独立的 `build-semantic-dimension` Skill 负责。车系只是该 Skill 的一个真实示例，不是一种新的维度类别。

第一版支持：

| 创建方式 | `resolution_mode` | 用途 |
| --- | --- | --- |
| 直接字段 | `source_field` | 读取已确认的字段，例如能源类型、车型级别 |
| 推导规则 | `derived` | 从一个或多个字段按规则得到值，例如价格段、上市年份 |
| 实体匹配 | `entity_lookup` | 多来源原始名称映射到规范实体键，例如车系 |
| 日历映射 | `calendar_lookup` | 日期字段映射到自然周、自然月等周期成员 |


## 本轮边界

- [x] 统一维度的文件格式与向后兼容策略。
- [x] 后端创建参数、校验和 Registry 摘要支持创建方式。
- [x] 创建独立的维度构建 Skill，定义多表输入、产物、验证和人工审核边界。
- [x] 前端按创建方式录入结构化定义、在资产卡片中显示，并保留现有 Markdown 编辑入口。
- [x] 维度详情提供“编辑维度设置”；保存时只更新 `resolution_mode` / `resolution`，保留正文、构建 Skill 声明和结果文件。
- [x] 现有维度补齐显式创建方式；补回归测试。
- [x] 验证定义可随 `dimension.md` 被 Registry 和 Agent 读取。

不在本轮实现：实体匹配的批量 AI 生成、映射审核工作台、资产关联 CRUD 和日历成员物化。这些能力将消费本轮已经稳定的 `entity_lookup` / `calendar_lookup` 定义格式，并通过 `build-semantic-dimension` Skill 生成或刷新完成的维度内容。

## 可迁移契约

`dimension.md` 的 frontmatter 以 `resolution_mode` 和 `resolution` 为机器可读事实；正文保留业务解释、SQL Hint 与禁止规则。

```yaml
resolution_mode: source_field
resolution:
  bindings:
    - asset_ref: dbs_xxx.vehicle_params_wide
      display_name: insight_data · vehicle_params_wide
      fields:
        value: energy_type
```

每个模式都有自己的受限字段集合，创建 API 会拒绝缺少必要字段或未知模式的定义。运行时仍把完整 Markdown 注入 Agent，因此既不会丢失自然语言口径，也能给后续执行器一个稳定的结构化入口。

语义资产目录示例：

```text
dimensions/vehicle_series/
  dimension.md        # 语义、输入输出契约、AI 使用边界
  entities.jsonl       # 规范实体
  bindings/
    insurance_sales.jsonl
    vehicle_config.jsonl
  references/
    byd_chery_demo.json
```

构建 Skill 是通用的**维度构建器**：从一个或多个来源读入原始键，按维度自身的语义规则构造规范成员和可复用 lookup/crosswalk。它可以只落 JSONL，也可以在显式配置后产出数据库中间维度表；业务事实表始终保持原样。

```yaml
skill: build-semantic-dimension
inputs: [source_bindings]
outputs:
  - kind: lookup_jsonl
    path: dimensions/<dimension_id>/bindings/<source>.jsonl
  - kind: intermediate_table
    target: analytics_dim_<dimension_id>
refresh: incremental
execution: manual_or_authorized_workflow
```

`vehicle_series` 只是在该模板下将输入键定义为“销量品牌+车系 / 配置品牌+车系”，将输出实体键定义为 `entity_key` 的首个验证实例。任何多表中间维度都复用这一结构，不新增专用资产类型。
