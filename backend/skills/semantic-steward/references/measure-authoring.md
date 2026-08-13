# Measure Markdown authoring guide

Use this guide as a writing checklist, not as a form presented to the user. Natural headings and prose are allowed.

## Required business content

The body must make these points reviewable:

- what business question the Measure answers and how it differs from similar Measures;
- source values or semantic inputs;
- aggregation and calculation order, especially ratio-of-sums versus average-of-rows;
- default business grain and any forbidden grain;
- time, unit, currency, null, zero denominator, negative value, refund, and duplicate policies that apply;
- examples covering normal behavior, a boundary, and a misleading alternative.

If a rule cannot be determined from data evidence, ask the user. Do not manufacture it.

The examples below are fictional shape examples, never defaults. Do not copy their source grain, currency policy, aliases, tags, refund behavior, or version into a real candidate unless evidence or the user confirmed each one. An explicitly visible “not applicable” is better than an invented rule.

## Minimum frontmatter for the current vertical

```yaml
formatter: semantic-asset
name: 成交均价
type: measure
description: 指定范围内每个售出单位对应的净成交金额
aliases: [平均成交价, ASP]
tags: [销售]
version: 0.1.0
```

The Backend may fill a missing `formatter` or `type` from the explicit Measure target path. It must reject a conflicting value. The Agent owns proposals for `name`, `description`, `aliases`, `tags`, and `version` because they change retrieval, display, or audit behavior.

Do not add `authoring_schema` to published files. The Skill reference itself selects this guide.

## Example body shape

```markdown
# 成交均价

## 业务含义

成交均价表示指定范围内每个售出单位对应的净成交金额。

## 计算口径与颗粒度

先分别汇总成交金额与销量，再计算 `SUM(成交金额) / SUM(销量)`；不得对订单行价格直接取平均。默认按车系展示，但计算分子和分母时保持订单事实粒度。

## 边界规则

- 退货按负数计入净金额和净销量。
- 净销量为 0 时返回空值。
- 一次计算只允许一种币种。

## 验收案例

- 正常：1 台与 2 台订单合并后，用总金额除以 3。
- 边界：退货冲销后净销量为 0，结果为空。
- 反例：不得对两条订单行的单价执行简单平均。
```

## Revision discipline

When editing an existing Measure, preserve unrelated prose and references. If the denominator, aggregation order, default grain, inclusion policy, unit, or time interpretation changes, call it out as a business-breaking change even if the raw Markdown diff is small.
