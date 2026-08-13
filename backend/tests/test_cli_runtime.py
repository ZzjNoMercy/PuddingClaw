from __future__ import annotations

import subprocess
import ast
import tomllib
from pathlib import Path

import cli_runtime


def test_cli_runtime_never_policy_is_read_only(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PUDDINGCLAW_CLI_INSTALL_POLICY", "never")
    monkeypatch.setattr(cli_runtime.shutil, "which", lambda _name: None)
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    status = cli_runtime.ensure_cli_runtime(tmp_path, runner=runner)

    assert status["installed"] is False
    assert status["install_policy"] == "never"
    assert status["install_attempted"] is False
    assert calls == []


def test_production_defaults_to_prompt(monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_CLI_INSTALL_POLICY", raising=False)
    monkeypatch.setenv("PUDDINGCLAW_ENV", "production")

    assert cli_runtime._requested_policy() == ("prompt", False)


def test_cli_runtime_auto_installs_local_package(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PUDDINGCLAW_CLI_INSTALL_POLICY", "auto")
    package_dir = tmp_path / "packages" / "puddingclaw-deploy-cli"
    (package_dir / "src").mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"name":"@puddingai/puddingclaw","version":"0.1.2",'
        '"bin":{"puddingclaw":"src/cli.js"}}',
        encoding="utf-8",
    )
    (package_dir / "src" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setenv("PUDDINGCLAW_CLI_PACKAGE_DIR", str(package_dir))
    paths = {"node": "/fake/node", "npm": "/fake/npm", "puddingclaw": None}
    monkeypatch.setattr(cli_runtime.shutil, "which", paths.get)
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        if args[0] == "/fake/node":
            return subprocess.CompletedProcess(args, 0, stdout="v22.1.0", stderr="")
        if args[0] == "/fake/npm" and len(args) == 2:
            return subprocess.CompletedProcess(args, 0, stdout="10.5.0", stderr="")
        if args[0] == "/fake/npm" and args[1] == "install":
            return subprocess.CompletedProcess(args, 0, stdout="installed", stderr="")
        raise AssertionError(args)

    status = cli_runtime.ensure_cli_runtime(tmp_path, runner=runner)

    assert status["install_attempted"] is True
    assert status["install_succeeded"] is True
    assert any(args[1:3] == ["install", "--global"] for args in calls)


def test_cli_version_json_is_reported_without_treating_schema_version_as_cli_version(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PUDDINGCLAW_CLI_INSTALL_POLICY", "never")
    paths = {"node": "/fake/node", "npm": "/fake/npm", "puddingclaw": "/fake/puddingclaw"}
    monkeypatch.setattr(cli_runtime.shutil, "which", paths.get)

    def runner(args, **kwargs):
        if args[0] == "/fake/node":
            return subprocess.CompletedProcess(args, 0, stdout="v22.1.0", stderr="")
        if args[0] == "/fake/npm":
            return subprocess.CompletedProcess(args, 0, stdout="10.5.0", stderr="")
        if args[0] == "/fake/puddingclaw":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"schema_version":"1","cli_version":"0.1.2"}',
                stderr="",
            )
        raise AssertionError(args)

    status = cli_runtime.ensure_cli_runtime(tmp_path, runner=runner)

    assert status["installed"] is True
    assert status["version"] == "0.1.2"
    assert status["version_mismatch"] is False


def test_wheel_manifest_covers_backend_local_import_closure():
    backend = Path(__file__).resolve().parent.parent
    document = tomllib.loads((backend / "pyproject.toml").read_text(encoding="utf-8"))
    included = {
        Path(item).stem if str(item).endswith(".py") else str(item).split("/", 1)[0]
        for item in document["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    }
    local = {path.stem for path in backend.glob("*.py")}
    local.update(path.name for path in backend.iterdir() if path.is_dir() and (path / "__init__.py").is_file())
    imported = {"app"}
    for source in backend.rglob("*.py"):
        if any(part in {".venv", "tests", "__pycache__"} for part in source.parts):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
    missing = (imported & local) - included
    assert missing == set(), f"Backend wheel only-include is missing local imports: {sorted(missing)}"
