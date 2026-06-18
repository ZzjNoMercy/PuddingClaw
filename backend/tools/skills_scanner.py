"""Skills Scanner — Scan skills/ directory and generate SKILLS_SNAPSHOT.md."""

from pathlib import Path

import yaml


def scan_skills(base_dir: Path) -> str:
    """Scan all SKILL.md files and generate SKILLS_SNAPSHOT.md."""
    skills_dir = base_dir / "skills"
    snapshot_path = base_dir / "workspace" / "SKILLS_SNAPSHOT.md"

    if not skills_dir.exists():
        skills_dir.mkdir(parents=True)

    skills = []
    seen_names: set[str] = set()
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        try:
            content = skill_md.read_text(encoding="utf-8")
            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1])
                    if meta:
                        name = meta.get("name", skill_md.parent.name)
                        if name in seen_names:
                            print(f"⚠️  Duplicate skill name '{name}' at {skill_md}, skipping")
                            continue
                        seen_names.add(name)
                        rel_path = str(skill_md.relative_to(base_dir))
                        skills.append({
                            "name": name,
                            "description": meta.get("description", ""),
                            "location": rel_path,
                        })
        except Exception as e:
            print(f"⚠️ Error scanning {skill_md}: {e}")

    # Build XML-style snapshot
    lines = ["<available_skills>"]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{s['name']}</name>")
        lines.append(f"    <description>{s['description']}</description>")
        lines.append(f"    <location>{s['location']}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    snapshot = "\n".join(lines)
    snapshot_path.write_text(snapshot, encoding="utf-8")
    print(f"📋 Skills snapshot: {len(skills)} skills found")
    return snapshot
