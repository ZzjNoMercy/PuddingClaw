# Relation Markdown authoring guide

Relations change join behavior and can silently multiply results. Treat cardinality and keys as business decisions backed by data evidence.

## Resolve before drafting

- both endpoints and their roles;
- key fields and normalization assumptions;
- `dimension_binding` versus `direct_join`;
- cardinality, join type, and Grain on each side;
- duplicate keys, null keys, unmatched rows, coverage, and fan-out risk;
- an example that would reveal duplicate counting.

Use data probes when possible, but report observed cardinality separately from the user-confirmed contract.

All examples in this guide are fictional shape examples, never defaults. Do not copy their endpoints, keys, cardinality, join type, or unmatched-row policy without evidence or user confirmation.

## Structured projection

```yaml
formatter: asset-relation
name: 订单到客户
type: relation
description: 订单事实通过客户标识连接客户主数据
relation_type: direct_join
relation:
  type: direct_join
  left: {ref: warehouse.orders, key_fields: [customer_id]}
  right: {ref: warehouse.customers, key_fields: [customer_id]}
  field_mapping: {left: [customer_id], right: [customer_id]}
  cardinality: many_to_one
  join_type: left
```

The Backend validates shape and known semantic dependencies. It does not guess keys or cardinality.
Every `field_mapping.left/right` field must also be declared in the corresponding endpoint's `key_fields`.
