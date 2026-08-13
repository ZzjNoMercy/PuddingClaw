# Grain Markdown authoring guide

Use this checklist while talking with the user. Grain rules remain in the authoritative body; do not invent a YAML identity DSL.

## Resolve before drafting

- the business object being counted or grouped;
- its stable unique key or confirmed composite key;
- duplicate arrival, correction, snapshot, and late-update behavior;
- allowed parent rollups and forbidden lower-level substitutions;
- Measures that require or cannot use this Grain;
- normal, duplicate, and ambiguous examples.

Inspect profiles or sample data when available, but describe uniqueness as observed evidence until the user confirms the business identity. A distinct-count result is not proof of a permanent business key.

All examples in this guide are fictional shape examples, never defaults. Do not copy their identity, deduplication, aliases, tags, or rollup rules without evidence or user confirmation.

## Minimum frontmatter

```yaml
formatter: semantic-asset
name: 订单
type: grain
description: 每个已确认订单对应一个业务对象
aliases: [销售订单]
tags: [销售]
```

The Backend may fill only `formatter` and `type` from the target path. The body must explain business object, identity, deduplication, rollup, and acceptance examples.
