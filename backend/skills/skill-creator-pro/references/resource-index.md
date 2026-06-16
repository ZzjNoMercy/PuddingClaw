# Resource Index

This index helps you navigate the skill-creator-pro reference library efficiently. Resources are organized by complexity and use case to support progressive learning.

## Directory Structure

```
references/
├── core/              # Fundamental concepts for most users
├── advanced/          # Complex scenarios and detailed guidance
├── templates/         # Request handling templates
└── validation/        # Quality check resources
```

## Quick Navigation by Need

### "I'm creating my first skill"
Start here:
1. `core/quick-start.md` - 5-minute beginner path
2. `core/skill-paradigms.md` - Choose your skill structure
3. `core/design-playbook.md` - Define boundary and scope
4. `validation/checklists.md` - Validate before declaring ready

### "I need to review an existing skill"
Start here:
1. Run `scripts/review_skill.py <path>` first
2. `validation/skill-review-scorecard.md` - Scoring framework
3. `core/not-to-do-red-lines.md` - Check for anti-patterns
4. `advanced/content-review.md` - Evaluate content quality
5. `advanced/remediation-playbook.md` - Map findings to fixes

### "I'm upgrading a skill"
Start here:
1. Review first (see above)
2. `core/skill-paradigms.md` - Confirm paradigm choice
3. `advanced/to-do-constitution.md` - Apply execution rules
4. `advanced/evaluation-handoff.md` - Validate with skill-benchmark

### "I need to handle incomplete requests"
Start here:
1. `templates/request-templates.md` - Collect missing information

### "I'm building a complex multi-mode skill"
Start here:
1. `core/quick-start.md` - Path D: Complex Multi-Mode
2. `core/skill-paradigms.md` - Multi-mode pattern
3. `advanced/module-building-blocks.md` - Whitepaper building blocks
4. `core/examples.md` - Study skill-creator-pro itself

## Resource Loading Strategy

### Core Resources (Load First)
These cover 80% of common skill creation needs:

| File | When to Read | Key Topics |
|------|-------------|------------|
| `quick-start.md` | Always start here | 3-step creation, use case paths, common mistakes |
| `skill-paradigms.md` | Before structuring | Workflow, knowledge-base, automation, multi-mode patterns |
| `design-playbook.md` | Before writing | Boundary definition, scope control, build order |
| `examples.md` | When comparing patterns | Strong vs weak skill examples |
| `not-to-do-red-lines.md` | Before declaring ready | Anti-patterns, failure classes |

### Advanced Resources (Load on Demand)
Read these when you need deeper guidance:

| File | When to Read | Key Topics |
|------|-------------|------------|
| `module-building-blocks.md` | When choosing control-layer modules | Whitepaper building blocks (Identity, Interaction, Decision, Doctrine) |
| `to-do-constitution.md` | During create/upgrade work | Whitepaper execution rules |
| `content-review.md` | When judging quality | Content quality, routing strength evaluation |
| `remediation-playbook.md` | After review findings | Mapping findings to concrete fixes |
| `evaluation-handoff.md` | After create/upgrade | Handoff to skill-benchmark for effectiveness validation |
| `skill-constitution.md` | When designing architecture | Constitutional rules for skill design |

### Template Resources (Load When Needed)
| File | When to Read | Key Topics |
|------|-------------|------------|
| `request-templates.md` | When request is incomplete | Templates for collecting trigger, boundary, output details |

### Validation Resources (Load Before Completion)
| File | When to Read | Key Topics |
|------|-------------|------------|
| `checklists.md` | Before declaring ready | Quality checklist for validation |
| `skill-review-scorecard.md` | During review | Scoring framework (structure, trigger, content, execution) |

## Progressive Learning Paths

### Path 1: Simple Workflow Skill (Fastest)
1. `core/quick-start.md` → Path A
2. `core/skill-paradigms.md` → Workflow Pattern
3. `validation/checklists.md`

### Path 2: Knowledge-Heavy Skill
1. `core/quick-start.md` → Path B
2. `core/skill-paradigms.md` → Knowledge-Base Pattern
3. `core/design-playbook.md` → Progressive Disclosure
4. `validation/checklists.md`

### Path 3: Automation Skill
1. `core/quick-start.md` → Path C
2. `core/skill-paradigms.md` → Automation Pattern
3. `advanced/module-building-blocks.md` → Script Integration
4. `validation/checklists.md`

### Path 4: Complex Multi-Mode Skill (Most Comprehensive)
1. `core/quick-start.md` → Path D
2. `core/skill-paradigms.md` → Multi-Mode Pattern
3. `advanced/module-building-blocks.md`
4. `advanced/to-do-constitution.md`
5. `core/examples.md` → Study skill-creator-pro
6. `validation/skill-review-scorecard.md`
7. `advanced/evaluation-handoff.md`

## Topic Quick Reference

| Topic | File | Category |
|-------|------|----------|
| Getting started | `quick-start.md` | core |
| Skill structure patterns | `skill-paradigms.md` | core |
| Boundary definition | `design-playbook.md` | core |
| Anti-patterns | `not-to-do-red-lines.md` | core |
| Strong vs weak examples | `examples.md` | core |
| Whitepaper building blocks | `module-building-blocks.md` | advanced |
| Execution rules | `to-do-constitution.md` | advanced |
| Content quality | `content-review.md` | advanced |
| Fixing issues | `remediation-playbook.md` | advanced |
| Benchmark integration | `evaluation-handoff.md` | advanced |
| Constitutional rules | `skill-constitution.md` | advanced |
| Request templates | `request-templates.md` | templates |
| Quality checklist | `checklists.md` | validation |
| Review scoring | `skill-review-scorecard.md` | validation |

## Context Optimization Tips

- **First-time users**: Only load `core/quick-start.md` initially
- **Experienced users**: Jump directly to relevant advanced resources
- **Review tasks**: Load validation resources first, then advanced as needed
- **Multi-mode skills**: Expect to load 4-6 resources total
- **Simple skills**: May only need 2-3 core resources

## When to Load Multiple Resources

Load resources in sequence, not all at once:

1. **Planning phase**: Load core resources (quick-start, paradigms, design-playbook)
2. **Implementation phase**: Load advanced resources as specific needs arise
3. **Validation phase**: Load validation resources (checklists, scorecard)
4. **Handoff phase**: Load evaluation-handoff if using skill-benchmark

This modular approach keeps context lean while maintaining access to detailed guidance when needed.
