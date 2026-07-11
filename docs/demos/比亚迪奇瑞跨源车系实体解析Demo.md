# 比亚迪、奇瑞跨源车系实体解析 Demo

> 生成时间：2026-07-10  
> 目标：验证“AI 驱动的实体解析资产”是否可以替代人工维护大量跨源 BI 关系。  
> 范围：真实上险量 Excel 与 PostgreSQL 产品配置库中的比亚迪、奇瑞车系。

## 结论

这不是人工填写品牌-车系维表的 Demo，而是一次可复跑的实体解析过程。每条已解析记录同时保存销量表原始键、配置库原始键和共同的 `entity_key`；未能证明正确的记录保留为候选或未匹配，不进入后续联合统计。

| 指标 | 结果 |
| --- | ---: |
| 销量侧车系数 | 46 |
| 配置库可用车系数 | 100 |
| 自动匹配车系数 | 31 |
| 自动匹配销量覆盖 | 939,767 / 965,100，约 97.37% |
| 待确认候选 | 2 |
| 未匹配 | 13 |

按品牌拆分：比亚迪 20 个自动匹配、7 个未匹配；奇瑞 11 个自动匹配、2 个候选、6 个未匹配。

## 两侧真实数据

| 数据源 | 实体字段 | 示例 |
| --- | --- | --- |
| 2023 年 1-5 月乘用车市场上险量 Excel | `品牌` + `1-子车型` | `比亚迪` + `比亚迪秦PLUS` |
| PostgreSQL `vehicle_params_wide` | `brand` + `serial_name` | `比亚迪` + `秦PLUS` |

`销量` 只用于聚合和计算覆盖率；`1-brandcn车型`只作为原始样例证据，不参与匹配主键。

## 跨源关系长相

真实的已解析关系不是“只存一个规范车系名称”，而是每条记录都同时保留两端的键：

| entity_key | canonical_brand | canonical_series | 销量侧键 | 配置侧键 | 是否可 Join |
| --- | --- | --- | --- | --- | --- |
| `比亚迪::秦plus` | 比亚迪 | 秦PLUS | `品牌=比亚迪` + `1-子车型=比亚迪秦PLUS` | `brand=比亚迪` + `serial_name=秦PLUS` | 是 |
| `奇瑞::qq冰淇淋` | 奇瑞 | QQ冰淇淋 | `品牌=奇瑞` + `1-子车型=奇瑞QQ冰淇淋` | `brand=奇瑞` + `serial_name=QQ冰淇淋` | 是 |
| 无 | 奇瑞 | 艾瑞泽5 | `品牌=奇瑞` + `1-子车型=艾瑞泽5e` | 候选 `brand=奇瑞` + `serial_name=艾瑞泽5` | 否，待审核 |

`canonical_series` 是业务显示名，不能单独作为 Join 条件；跨源联合分析使用 `entity_key`。这个键由 `canonical_brand + canonical_series` 归一化生成，因此不会把不同品牌的同名车系混在一起。

## 已自动处理的真实差异

| 销量品牌 | 销量车系 | 配置品牌 | 配置车系 | 方法 |
| --- | --- | --- | --- | --- |
| 比亚迪 | 比亚迪秦PLUS | 比亚迪 | 秦PLUS | 移除已解析品牌前缀后归一化精确匹配 |
| 比亚迪 | 比亚迪E2 | 比亚迪 | 比亚迪e2 | Unicode / 大小写归一化精确匹配 |
| 奇瑞 | 奇瑞QQ冰淇淋 | 奇瑞 | QQ冰淇淋 | 移除已解析品牌前缀后归一化精确匹配 |
| 奇瑞 | 瑞虎5X | 奇瑞 | 瑞虎5x | Unicode / 大小写归一化精确匹配 |

自动匹配只发生在以下条件同时满足时：

1. `品牌` 已经唯一映射到配置库中的一个 `brand`。
2. 仅在该品牌范围内比较 `1-子车型` 与 `serial_name`。
3. 对名称执行 NFKC、大小写、空格和标点归一化，并允许移除已确认的品牌前缀。
4. 得到唯一的配置车系候选。

## 没有强行匹配的记录

以下是这次数据中最重要的安全边界：相似不等于同一实体。

| 销量品牌 | 销量车系 | 销量 | 状态 | 系统行为 |
| --- | --- | ---: | --- | --- |
| 奇瑞 | 艾瑞泽5e | 223 | 候选 | 仅建议 `艾瑞泽5`，相似度 0.8889，不自动采用 |
| 奇瑞 | 瑞虎3 | 2 | 候选 | 仅建议 `瑞虎3x`，相似度 0.8571，不自动采用 |
| 奇瑞 | 艾瑞泽EX | 12,228 | 未匹配 | 配置库没有可证明的同一车系 |
| 奇瑞 | 奇瑞eQ1 | 11,027 | 未匹配 | 配置库没有可证明的同一车系 |
| 比亚迪 | 比亚迪元 | 108 | 未匹配 | 不能擅自合并到元PLUS |
| 比亚迪 | 比亚迪E1 | 25 | 未匹配 | 配置库中未发现相同规范车系 |

其余未匹配项保存在完整 CSV / JSON 结果中。

## AI 在这里做什么

确定性规则处理了约 97% 的销量覆盖，不应让 AI 对这些记录重复猜测。AI 应该只处理剩下的小集合：

1. 根据双方资料、车型年代和名称语义，为未匹配项给出映射草案。
2. 判断候选是否应成为“同一车系”、父子车系，还是不同已停售车系。
3. 从多条已确认映射中归纳新规则，例如“销量车系带品牌前缀”。
4. 输出带理由、证据和置信度的草案，等待审核后才写入正式资产。

也就是说，AI 建立的是解析资产的增量知识；数据库保存的是已经确认、可复算的结果。

## Demo 产物

- [维度构建 Skill](/Users/pet/Code/AI/Agent/PuddingClaw/backend/skills/build-semantic-dimension/SKILL.md)
- [完整解析结果 JSON](/Users/pet/Code/AI/Agent/PuddingClaw/docs/demos/vehicle-series-resolution/byd-chery-vehicle-series-resolution.json)
- [可筛选审阅 CSV](/Users/pet/Code/AI/Agent/PuddingClaw/docs/demos/vehicle-series-resolution/byd-chery-vehicle-series-resolution.csv)
- [给 AI 读取的可迁移车系维度资产](/Users/pet/Code/AI/Agent/PuddingClaw/backend/semantic-assets/dimensions/vehicle_series/dimension.md)
- [真实跨源 Crosswalk Reference](/Users/pet/Code/AI/Agent/PuddingClaw/backend/semantic-assets/dimensions/vehicle_series/references/byd_chery_demo.json)

运行方式：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw/backend
.venv/bin/python skills/build-semantic-dimension/scripts/vehicle_series_demo.py
```

这个过程只读真实数据源，生成审阅结果与可迁移的 crosswalk reference；不会修改 PostgreSQL 的业务表，也不会生成传统维表。

## 下一步判断标准

如果你认可这个 Demo，下一步不是直接建传统维表，而是建立可迁移的 `entity_lookup` 维度资产，并加一个“AI 生成映射草案 - 人工审核 - 写入已确认解析结果”的闭环。正式联合分析只能使用 `auto_matched` 或 `accepted` 记录，并显式报告未覆盖范围。
