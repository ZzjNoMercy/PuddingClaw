# Quick Start Guide

Get started with skill-creator-pro in 5 minutes. This guide covers the minimal path to create your first production-grade skill.

## Your First Skill in 3 Steps

### Step 1: Define the Boundary (2 minutes)

Before writing anything, answer these questions:

- **What repeated problem does this skill solve?**
  - Example: "Convert research notes into structured documentation"
- **What user requests should trigger it?**
  - Example: "organize my notes", "structure this research"
- **What nearby requests should NOT trigger it?**
  - Example: "write a blog post", "summarize this article"

If these answers are fuzzy, stop and tighten the scope first.

### Step 2: Scaffold the Structure (1 minute)

Run the initialization script:

```bash
python3 /Users/mac/.claude/skills/skill-creator-pro/scripts/init_skill_pro.py my-skill-name --path ~/.claude/skills --resources references
```

This creates:
- `SKILL.md` with the recommended structure
- `agents/openai.yaml` for routing metadata
- `references/` directory for detailed documentation

### Step 3: Customize the Core (2 minutes)

Edit `SKILL.md` and replace the template sections:

1. **Goal**: One sentence mission statement
2. **Workflow**: 4-6 numbered steps
3. **Decision Tree**: "If X, do Y" routing rules
4. **Constraints**: Non-negotiable rules
5. **Resources**: When to use each script/reference

That's it. You now have a working skill structure.

## What You Just Created

Your skill has three layers:

- **Routing Layer** (frontmatter): Tells the system when to invoke your skill
- **Control Layer** (SKILL.md): Guides execution with workflow and decision logic
- **Execution Support** (scripts/references): Provides detailed knowledge and automation

## Next Steps by Use Case

Choose your path based on what you need:

### Path A: Simple Workflow Skill
Your skill just needs to guide a sequence of steps.

→ Read: `references/core/workflow-patterns.md`
→ Skip: Scripts and complex references

### Path B: Knowledge-Heavy Skill
Your skill needs to reference detailed technical knowledge.

→ Read: `references/core/progressive-disclosure.md`
→ Create: Reference files in `references/`

### Path C: Automation Skill
Your skill needs to run scripts or process files.

→ Read: `references/core/script-integration.md`
→ Create: Python scripts in `scripts/`

### Path D: Complex Multi-Mode Skill
Your skill has multiple operating modes (create/review/upgrade).

→ Read: `references/advanced/multi-mode-design.md`
→ Study: skill-creator-pro itself as a reference implementation

## Common First-Time Mistakes

Avoid these pitfalls:

- **Making it too broad**: Start narrow and strong, not broad and vague
- **Skipping boundary definition**: Fuzzy boundaries lead to trigger confusion
- **Over-engineering**: Don't add features you don't need yet
- **Duplicating content**: Put details in references, not in SKILL.md

## When You're Ready for More

After creating your first skill, explore:

- **Paradigm Selection**: `references/core/skill-paradigms.md` - Choose the right design pattern
- **Trigger Quality**: `references/core/trigger-design.md` - Write better frontmatter
- **Review Process**: `scripts/review_skill.py` - Validate your skill structure
- **Advanced Patterns**: `references/advanced/` - Multi-mode, orchestration, etc.

## Getting Help

If you're stuck:

1. Run the review script: `python3 scripts/review_skill.py path/to/your/skill`
2. Check the examples: `references/core/examples.md`
3. Read the anti-patterns: `references/core/not-to-do-red-lines.md`

The goal is to get you productive quickly, then gradually reveal complexity as you need it.
