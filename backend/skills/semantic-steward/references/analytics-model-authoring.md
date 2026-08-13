# Analytics Model Markdown authoring guide

An Analytics Model narrows the analysis surface. It should select existing assets and explain the questions, scope, and outputs a business user expects.

## Resolve before drafting

- model goal, target users, and representative questions;
- selected data assets, Measures, Dimensions, Grains, and Relations;
- default filters, exclusions, time scope, and missing-parameter behavior;
- required Guardrails;
- optional templates and the default template;
- acceptance examples and explicitly unsupported questions.

Read every selected semantic definition first. For more than one data asset, select Relations that form a valid connected graph. This tool publishes only `model.md`; referenced template files must already exist in the model package.

All examples in this guide are fictional shape examples, never defaults. Do not copy their selected assets, filters, Guardrails, templates, aliases, or output policy without evidence or user confirmation.

## Structured projection

```yaml
formatter: analytics-model
id: order-operations
name: 订单经营分析
type: analysis_model
description: 回答订单规模与渠道分布问题
data_assets: {tables: [warehouse.orders], table_aliases: {}}
semantic_assets: {measures: [], dimensions: [], grains: []}
asset_relations: []
guardrails: []
templates: {}
default_template: ''
references: {}
```

The Backend fills `formatter`, `type`, and directory-derived `id`, then validates references and the selected relation graph. It never selects assets, filters, Guardrails, or templates on the user's behalf.
