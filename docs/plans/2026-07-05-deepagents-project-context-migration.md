# DeepAgents Project Context Migration

## Goal

Move the current inline DeepAgents system prompt into durable, editable prompt files without losing the operational lessons accumulated in the existing prompt.

## Phases

### Phase 1 - Prompt Storage

- [x] Add DeepAgents prompt files for base identity, project context template, and tool guides.
- [x] Preserve every current inline prompt rule in one of those files.

### Phase 2 - Project Context Lifecycle

- [x] Copy the project context template into `<project>/.puddingclaw/PROJECT_CONTEXT.md` when a local project is registered.
- [x] Provide backend APIs to read and update the selected project's context file.

### Phase 3 - Runtime Assembly

- [x] Replace the inline DeepAgents prompt with a builder that assembles `BASE.md`, project-level `PROJECT_CONTEXT.md`, and `TOOL_GUIDES.md`.
- [x] Fall back to the template context for unscoped workspaces or missing project context files.

### Phase 4 - Frontend Editing

- [x] Add a Settings surface for editing the current project's `PROJECT_CONTEXT.md`.

### Phase 5 - Verification

- [x] Run backend tests for project context and DeepAgents prompt assembly.
- [x] Run frontend type check.

## Verification Notes

- `PYTHONPATH=backend pytest backend/tests/test_deepagents_project_context.py backend/tests/test_trace_collector.py -q` passed: 27 tests.
- `PYTHONPATH=backend python -m py_compile backend/projects/project_context.py backend/projects/registry.py backend/api/projects.py backend/graph/deepagents_prompt_builder.py backend/graph/deepagents_manager.py` passed.
- `npx tsc --noEmit` passed in `frontend`.
- `PYTHONPATH=backend pytest backend/tests/test_deepagents_project_context.py backend/tests/test_deepagents_manager.py -q` had one existing `/knowledge/` route assertion failure because this local environment resolves knowledge root from current config instead of `tmp_path / "knowledge"`; the new project context tests passed.
