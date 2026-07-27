# Project Context

## Workspace And Skill Paths

User files live under `/workspace/`. Always reference them with this prefix, for example `/workspace/dashboard.html` or `/workspace/subdir/file.py`.

Skill files live under `/skills/`, for example `/skills/design-html/SKILL.md`.

The main Agent owns semantic Skill routing. Use the injected Skill catalog to
decide whether an installed Skill directly applies. When it does, read that
Skill's authoritative `/skills/<skill-id>/SKILL.md` before using its business
tools. Reading the file is the typed activation signal; do not merely claim a
Skill was selected.

When the user explicitly requests a Skill that is not installed, treat this as
a recoverable installation flow rather than a task failure. Explain that it is
missing and offer to install it, search for/provide an authoritative HTTPS
source, or continue with the general Agent only if the user chooses that
fallback. If a source is available or the user asks to install, first read
`/skills/skill-management/SKILL.md` and follow its approval-gated workflow.
After a successful installation, read the newly installed SKILL.md and continue
the original task in the same Session.

`/workspace/` is the only model-visible namespace for project files. A bare root
path such as `/report.html` is a host absolute path, not a project alias; use
`/workspace/report.html` consistently. Host absolute paths are external unless
the runtime authority classifier proves that they resolve inside the current
workspace.

## Knowledge Base Paths

The global knowledge base lives at `/knowledge/`. Its physical path is configured by `PUDDINGCLAW_KNOWLEDGE_DIR`, defaulting to `backend/knowledge/`.

Add Markdown documents under `/knowledge/` when a workflow needs to store retrievable knowledge.

User-selected local Markdown files and MinerU-parsed PDFs are imported by the backend into `/knowledge/imported/...`, so prefer those virtual paths when citing or reading imported docs.

Do NOT store knowledge under `/workspace/knowledge/`.
