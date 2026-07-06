# Project Context

## Workspace And Skill Paths

User files live under `/workspace/`. Always reference them with this prefix, for example `/workspace/dashboard.html` or `/workspace/subdir/file.py`.

Skill files live under `/skills/`, for example `/skills/design-html/SKILL.md`.

The bare root `/` is an alias for `/workspace/`, but it MUST NOT be mixed with `/workspace/`. Pick `/workspace/` for user files and `/skills/` for skill files, and stick to those prefixes.

## Knowledge Base Paths

The global knowledge base lives at `/knowledge/`. Its physical path is configured by `PUDDINGCLAW_KNOWLEDGE_DIR`, defaulting to `backend/knowledge/`.

Add Markdown documents under `/knowledge/` when a workflow needs to store retrievable knowledge.

User-selected local Markdown files and MinerU-parsed PDFs are imported by the backend into `/knowledge/imported/...`, so prefer those virtual paths when citing or reading imported docs.

Do NOT store knowledge under `/workspace/knowledge/`.
