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
3. Call `install_skill` with the exact `plan_id` and `plan_sha256` only after approval.

Do not overwrite an existing Skill through the install path.

## Update

1. Inspect the installed Skill when its current version or integrity matters.
2. Call `prepare_skill_update`; omit the source only when the managed installation
   already records an authoritative source.
3. Review the baseline, diff and rollback information.
4. Call `update_skill` with the exact approved plan identifiers.

Preparation may use the network but must not modify `/skills`. Commit operations
must remain atomic and approval-gated. Never replace either phase with `execute`,
`write_file`, or an ad-hoc package installer.
