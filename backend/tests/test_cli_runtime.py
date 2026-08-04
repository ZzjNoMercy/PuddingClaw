from __future__ import annotations

import subprocess
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
    package_dir = tmp_path / "packages" / "puddingclaw-cli"
    (package_dir / "dist").mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"name":"@pudding/worker-puddingclaw","version":"0.1.0",'
        '"bin":{"puddingclaw":"dist/cli.js"}}',
        encoding="utf-8",
    )
    (package_dir / "dist" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
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
                stdout='{"schema_version":"1","cli_version":"0.1.0"}',
                stderr="",
            )
        raise AssertionError(args)

    status = cli_runtime.ensure_cli_runtime(tmp_path, runner=runner)

    assert status["installed"] is True
    assert status["version"] == "0.1.0"
    assert status["version_mismatch"] is False
