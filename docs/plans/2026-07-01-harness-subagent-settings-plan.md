# Harness SubAgent Settings Plan

## Goal

Expose a backend Settings page for Harness/SubAgent configuration, starting with a multimodal image analyzer subagent configured as a JSON-backed spec. The default image analyzer uses `qwen:qwen3.7`, and image-oriented needs should be delegated to this subagent from Agent mode.

## Decisions

- Persist the canonical configuration in `backend/config.json`, using `backend/config.py` defaults and `PUT /api/settings` partial updates.
- Keep the UI inside the existing Settings area instead of adding a separate settings system.
- Treat this first pass as a declarative spec editor: enable flags, name, routing hint, model, description, and system prompt.
- Keep `route_trigger` as the persisted compatibility field, but present it as a main-agent delegation hint rather than a deterministic router.
- Store subagents in `config.json` as a plural keyed object (`subagents.image_analyzer`) instead of leaking the frontend list shape (`subagents.items[]`) into user-editable config.
- Follow native DeepAgents behavior for subagent routing guidance: fold the routing hint into the subagent `description`, then let `SubAgentMiddleware` expose it through the `task` tool description and appended system message.
- Do not store secrets in the subagent spec. Provider keys continue to live in the existing AI Gateway / fallback provider settings.
- Uploaded images are explicit user-provided content. The main Agent request stays text-only and must use DeepAgents' native `task` tool to dispatch image work to the configured `image_analyzer` subagent. Local image paths outside the workspace still require the main Agent's external-file permission flow before the analyzer may read them.
- Uploaded/pasted attachments use a single attachment-id pipeline. The frontend sends files to `/api/attachments`, messages carry metadata/ids only, and backend tools/subagents read attachment bytes on demand.
- Configure LangChain's official `ModelCallLimitMiddleware` under `harness.model_call_limit` to prevent runaway model-call loops.

## Tasks

- [x] Inspect existing Settings/API/runtime boundaries.
- [x] Add default `subagents.image_analyzer` schema to backend config and example config.
- [x] Upgrade the Agent settings page into a Harness/SubAgent configuration surface.
- [x] Add/update tests for config display and persistence.
- [x] Refresh the design artifact for the subagent-focused settings page.
- [x] Run targeted validation.
- [x] Add image upload/local image path recognition for Agent mode.
- [x] Expose safe SubAgent optional fields from the official spec: skills and inheritance strategy.
- [x] Split Harness settings into tabs for future configuration groups.
- [x] Add a running pulse indicator on the SubAgent tab when the subagent is enabled.
- [x] Gate workspace-external local image path inlining behind the existing external-file permission boundary.
- [x] Replace the custom subagent system-prompt hint with native DeepAgents `description` / `SubAgentMiddleware` exposure.
- [x] Record native `SubAgentMiddleware` prompt injection in trace as a `wrap_model_call` middleware invocation.
- [x] Canonicalize persisted SubAgent config to keyed objects while keeping legacy `items[]` readable.
- [x] Rename canonical config/API field from legacy `subagent` to plural `subagents`.
- [x] Add Harness model-call limit settings and mount the official `ModelCallLimitMiddleware`.
- [x] Route image attachments/local image inputs through native DeepAgents `task -> image_analyzer` instead of sending `image_url` blocks to the main Agent model.
- [x] Unify upload and paste attachments behind an attachment store so images, PDFs, spreadsheets, Markdown, text, and other files share the same UI and request shape.

## Implementation Notes

- `backend/graph/deepagents_manager.py` builds DeepAgents `SubAgent` entries from normalized `subagents` settings display data.
- The Settings API accepts canonical `subagents` partial updates and still accepts legacy `subagent` as a migration alias.
- Official/local `deepagents.middleware.subagents.SubAgent` fields:
  - required: `name`, `description`, `system_prompt`
  - optional: `tools`, `model`, `middleware`, `interrupt_on`, `skills`, `permissions`
- The runtime keeps configured agents declarative. `image_analyzer` is a normal `SubAgent` spec; its image bridge is mounted as subagent-local middleware and only materializes images after that subagent has called `read_resource`.
- `skills` is safe to expose as a simple inherit/custom path strategy. `middleware`, `interrupt_on`, and `permissions` should stay advanced/JSON-only until there is typed UI validation.
- Harness settings now use tabs: `SubAgent`, `输入识别`, `Tools / Skills`, and `高级策略`, so later config groups can be added without extending one long page.
- The SubAgent tab shows a `status-pulse-ring` live indicator when any configured subagent is enabled.
- Local image path recognition passes session/workspace context into multimodal message construction. If a path is outside the workspace and no permission grant exists, the backend leaves it as text with a main-Agent authorization note instead of reading the file bytes.
- Main-agent message construction defaults to text-only to avoid OpenAI-compatible providers that reject multimodal content parts with errors such as `unknown variant image_url, expected text`.
- The configured `image_analyzer` is mounted as a declarative subagent behind DeepAgents' native `task` tool. The main Agent sees only resource refs and `harness_attachment_session_id`; the task-launched subagent must call `read_resource` before image bytes are materialized for its own multimodal model call. Runtime parsing keeps backward compatibility with the earlier `harness_image_session_id` label.
- `/api/attachments` stores all uploaded/pasted files under `backend/data/attachments/<session>/<attachment_id>/` and returns public metadata. Image analysis keeps attachment messages as ids/metadata; `read_resource` returns a compact image resource marker, and the subagent-local bridge converts only that already-read resource to model-readable `image_url`.
- Runtime inventory now shows the DeepAgents default `general-purpose` subagent as `source=deepagents.default`; configured subagents are shown separately as `source=config`.
- Trace now records SubAgent exposure when DeepAgents injects `task` / `Available subagent types` into the final system prompt. `SubAgentMiddleware` is modeled as `wrap_model_call`, matching the installed DeepAgents source.
- Frontend still receives display-friendly `subagents.items[]`, but saves `config.json` as:
  `{"subagents":{"image_analyzer":{"enabled":true,"model":"qwen3.7-plus",...}}}`.
- `harness.model_call_limit` defaults to `enabled=true`, `run_limit=50`, `thread_limit=null`, `exit_behavior=end`. This maps directly to official `ModelCallLimitMiddleware(run_limit, thread_limit, exit_behavior)`.

## Validation

- `python -m py_compile backend/config.py backend/api/config_api.py backend/graph/deepagents_manager.py backend/graph/trace_collector.py`
- `PYTHONPATH=. pytest tests/test_gateway_settings.py tests/test_trace_collector.py tests/test_deepagents_manager.py -q` from `backend/` -> 48 passed, 1 warning.
- `python -m py_compile backend/graph/deepagents_manager.py backend/tools/read_resource_tool.py` -> passed.
- `PYTHONPATH=. pytest tests/test_gateway_settings.py tests/test_external_file_permission.py tests/test_deepagents_manager.py -q` from `backend/` -> 40 passed, 1 warning.
- `npm run build` from `frontend/` -> compiled successfully.
- Previewed `designs/harness-settings/SubAgent Settings Panel.html` at `http://localhost:4387/harness-settings/SubAgent%20Settings%20Panel.html`; title, qwen model, spec preview, route nodes present; browser console had no errors.
- Started frontend dev server at `http://localhost:3002` and smoke-tested `/settings`; left nav shows `Harness 配置`, Harness page shows `SubAgent Spec`, `Tools`, `Skills`, and `qwen:qwen3.7`; browser console had no errors.
- Smoke-tested Harness tab switching at `http://localhost:3002/settings`; all four tabs render, `输入识别` shows upload/path support copy, and enabling both SubAgent switches shows one live pulse indicator on the SubAgent tab; browser console had no errors.
- Re-validated external local image paths: workspace-external image paths without grants are not inlined as `image_url`; the constructed user message asks the main Agent to go through external-file permission first.
- Re-validated image routing after provider 400: main Agent `_build_messages` no longer emits `image_url` blocks by default; explicit multimodal construction is reserved for the native task-launched `image_analyzer` subagent.
