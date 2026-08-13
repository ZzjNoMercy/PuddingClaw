# Dialogue and publication protocol

## Authoring Brief

Keep a short LLM-readable working note:

```yaml
kind: measure
goal: 定义成交均价
observed:
  - 成交金额和销量来自 monthly_sales
confirmed:
  - 先分别 SUM 再相除
unresolved:
  - 退货是否计入
evidence:
  - monthly_sales Profile
reviewed_topics:
  - business_meaning
  - sources
  - calculation
  - grain
  - rules
  - unit_and_time
  - duplicates
  - examples
body_outline:
  - 业务含义
  - 计算口径与颗粒度
  - 边界规则
  - 验收案例
```

The Brief prevents repeated questions and lost decisions. It is required as a preparation gate but is not a definition. Deleting it must not affect published behavior. `reviewed_topics` means each topic was either confirmed, supported by named evidence, or explicitly marked not applicable in the body; it does not authorize the Agent to invent an answer.

## Discovery boundary

Call `discover_semantic_definitions` before deciding reuse, edit, or create. For an inventory question, an empty query lists the selected kinds with pagination. For authoring, search the user's actual business concept and read every plausible candidate's full Markdown. Explain the comparison to the user.

The targeted call returns a Session-bound receipt. It proves that the relevant Registry snapshot was searched; it does not prove the Agent made the correct business decision. If results are incomplete, narrow the query or request a complete result before drafting. `prepare_semantic_markdown` rejects missing, inventory-only, incomplete, expired, wrong-kind, cross-Session, or stale receipts.

## Prepare boundary

Call `prepare_semantic_markdown` only when unresolved decisions are empty. Pass the targeted discovery receipt and complete Markdown, not fragments. The tool:

- reads the current baseline;
- fills only target-derived technical metadata;
- validates the candidate;
- freezes exact candidate bytes;
- returns the rendered body, machine effects, technical diff, and plan digest;
- does not write the active definition.

Summarize the prepared result and wait. A request such as “create this Measure” authorizes drafting and preparation, but never pre-approves an unseen candidate. Publication also passes through the Harness's one-call permission gate, bound to the exact `plan_id + plan_digest`.

## Publish boundary

Call `publish_semantic_markdown` only with the exact approved plan id and digest. It rejects:

- a modified or expired plan;
- a different Session;
- candidate byte changes;
- published Markdown changes after preparation.

On a stale baseline, do not overwrite. Read the latest Markdown, explain the conflict, and prepare a new candidate.
