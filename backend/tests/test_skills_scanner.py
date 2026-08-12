from pathlib import Path


def _skill(root: Path, skill_id: str, content: str) -> Path:
    path = root / skill_id
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: test\n---\n{content}\n",
        encoding="utf-8",
    )
    return path


def test_effective_skill_root_prefers_home_copy(tmp_path):
    from tools.skills_scanner import resolve_effective_skill_root

    package = tmp_path / "backend"
    bundled = _skill(package / "skills", "demo", "bundled")
    home = _skill(tmp_path / "home" / "skills", "demo", "home")

    assert resolve_effective_skill_root(package, home.parent, "demo") == home.resolve()
    assert bundled.resolve() != home.resolve()


def test_effective_skill_root_finds_home_only_skill(tmp_path):
    from tools.skills_scanner import resolve_effective_skill_root

    package = tmp_path / "backend"
    home = _skill(tmp_path / "home" / "skills", "user-only", "home")

    assert resolve_effective_skill_root(package, home.parent, "user-only") == home.resolve()
    assert resolve_effective_skill_root(package, home.parent, "missing") is None
