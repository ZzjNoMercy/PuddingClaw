---
name: code-review
description: Review Python code for bugs, style, and security risks. Trigger when user pastes code asking for feedback or audit.
allowed-tools: read_file
---

# Code Review Skill

When invoked:

1. Identify language and framework
2. Check for: syntax errors, security risks (eval/exec/SQL injection/secret leakage), style issues (PEP-8), performance pitfalls
3. Return findings as a numbered list with severity (HIGH/MED/LOW)
4. Suggest one concrete fix per finding

Keep tone constructive. Cite line numbers when possible.
