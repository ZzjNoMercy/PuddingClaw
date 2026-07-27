---
name: skill-management
description: Inspect, install, or update locally managed Agent Skills through a staged and approval-gated workflow. Use when the user asks to check a Skill version or integrity, install a Skill from a remote source, or update an installed Skill.
toolsets:
  - skill_management
---

# Skill Management

Use the typed management tools instead of shell commands or direct writes under
`/skills`. Never download and execute a Skill merely to inspect it.

## Inspect

Call `inspect_skill` for the installed manifest, version metadata, file list and
stable hashes. Report missing or invalid manifests without attempting repair.

## Install

1. Call `prepare_skill_install` with the exact HTTPS source and optional ref/subpath.
2. Review the returned source, validation result, file diff and immutable plan digest.
3. When the result contains `ui_commit_supported=true`, stop after a successful
   prepare. The frontend renders the immutable plan as a confirmation card and
   commits it directly after the user clicks **确认并安装**.

`status=prepared` means only that remote files were staged and validated. It
does **not** mean the Skill was installed. Never describe it as installed or
download-complete. When `ui_commit_supported=true`, never ask the user to reply
with “确认” and do not call `install_skill`. If a short handoff is useful, say:
“已暂存并校验，尚未安装；请在计划卡片中确认安装。” Legacy clients without
structured UI support may call `install_skill` only after their explicit
approval-gated continuation.

Do not overwrite an existing Skill through the install path.

## Update

1. Inspect the installed Skill when its current version or integrity matters.
2. Call `prepare_skill_update`; omit the source only when the managed installation
   already records an authoritative source.
3. Review the baseline, diff and rollback information.
4. When `ui_commit_supported=true`, stop after a successful prepare. The frontend
   plan card owns confirmation and commit; do not ask for a chat reply and do not
   call `update_skill`. Legacy clients may use the explicit approval-gated Tool
   continuation.

Preparation may use the network but must not modify `/skills`. Commit operations
must remain atomic and approval-gated. Never replace either phase with `execute`,
`write_file`, or an ad-hoc package installer.

`install_skill` and `update_skill` remain compatibility tools for older clients
that already implement an explicit one-time approval boundary. They are not the
continuation step for the structured frontend plan card.
