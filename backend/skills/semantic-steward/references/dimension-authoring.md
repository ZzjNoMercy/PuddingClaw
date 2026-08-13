# Dimension Markdown authoring guide

This generic workflow supports `source_field`, `derived`, and `calendar_lookup`. Route `entity_lookup` to `/skills/build-semantic-dimension/SKILL.md`.

## Resolve before drafting

- what each member means and whether labels are stable;
- source assets and fields, or the confirmed derivation/calendar rule;
- null, unknown, unmatched, deprecated, and newly appearing values;
- time zone and week start for calendar semantics;
- examples that distinguish this Dimension from similar ones.

The body must visibly name the selected resolution mode and explain every business-effective mapping. Do not infer mappings from coincidental column names.

All examples in this guide are fictional shape examples, never defaults. Do not copy their source, field, unknown-value policy, aliases, or tags without evidence or user confirmation.

## Structured projection

```yaml
formatter: semantic-asset
name: 订单渠道
type: dimension
description: 订单成交时采用的渠道分类
resolution_mode: source_field
resolution:
  mode: source_field
  bindings:
    - asset_ref: warehouse.orders
      display_name: 订单事实
      fields:
        value: channel_name
```

The Backend checks this shape but never constructs the mapping from prose. A mode conflict is a hard error.
