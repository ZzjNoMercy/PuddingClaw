# Legacy Chat runtime

The original Chat runtime (`POST /api/chat`, backed by
`backend/graph/agent.py`) is retired and no longer maintained.

- The frontend no longer exposes an Agent/Chat selector.
- New conversations default to and always run through the Agent runtime.
- Maintained conversation traffic uses `POST /api/agent` and DeepAgents.
- The old endpoint and proxy remain only for temporary compatibility with
  callers that have not migrated. They must not receive new features or fixes.
- Known compatibility callers are the skill review hook and the deep-research
  helper's model bootstrap. They should migrate before those workflows are
  extended.
- Existing Chat session data is not deleted. Search may still identify those
  records as `Legacy Chat（已停用）`.

Any new integration must use the Agent runtime. A future cleanup may remove
the legacy endpoint after all remaining callers have migrated.
