"""Isolated repository preparation, patch capture, and code verification."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import textwrap
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from runtime_identity.paths import PuddingClawPaths

from .contracts import CodeEvaluationSpec

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_FILES = 250_000
_SWE_CACHE_LOCK = threading.RLock()
_PYTHON_CALLABLE_RUNNER = textwrap.dedent(
    """
    import importlib
    import json
    import sys
    import traceback
    from pathlib import Path

    candidate_root = Path(sys.argv[1]).resolve()
    callable_ref = sys.argv[2]
    call_input = json.loads(sys.argv[3])
    result_path = Path(sys.argv[4]).resolve()
    payload = {"completed": False}
    try:
        sys.path.insert(0, str(candidate_root))
        module_name, separator, attribute = callable_ref.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError("callable must use module:function")
        target = getattr(importlib.import_module(module_name), attribute)
        actual = target(*call_input.get("args", []), **call_input.get("kwargs", {}))
        payload = {"completed": True, "actual": actual}
    except BaseException:
        payload = {**payload, "runner_error": traceback.format_exc(limit=8)}
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    raise SystemExit(0 if payload.get("completed") else 1)
    """
).strip()


def _load_callable_cases(path: Path) -> tuple[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("callable"), str):
        raise ValueError("Hidden callable fixture requires a callable string")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Hidden callable fixture requires at least one case")
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "expected" not in case:
            raise ValueError(f"Hidden callable case {index} requires expected")
        args = case.get("args", [])
        kwargs = case.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError(f"Hidden callable case {index} args/kwargs are invalid")
        normalized.append({"args": args, "kwargs": kwargs, "expected": case["expected"]})
    return payload["callable"], normalized


def _read_receipt(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return {}
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass


def _strict_json_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _strict_json_equal(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _strict_json_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _safe_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or path.parts[0] in {"", ".git"}:
        raise ValueError(f"Code fixture path escapes or mutates repository control data: {raw}")
    return path


def _write_files(root: Path, files: dict[str, str]) -> None:
    canonical_root = root.resolve()
    for raw, content in files.items():
        relative = _safe_relative_path(raw)
        target = root.joinpath(*relative.parts)
        cursor = target
        while cursor != root:
            if cursor.is_symlink():
                raise ValueError(f"Code fixture path crosses a symlink: {raw}")
            cursor = cursor.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = target.resolve()
        if resolved != canonical_root and canonical_root not in resolved.parents:
            raise ValueError(f"Code fixture path escapes workspace: {raw}")
        target.write_text(content, encoding="utf-8")


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "PuddingClaw Evaluation",
        "GIT_AUTHOR_EMAIL": "evaluation@localhost",
        "GIT_COMMITTER_NAME": "PuddingClaw Evaluation",
        "GIT_COMMITTER_EMAIL": "evaluation@localhost",
    }


def _run_git(
    cwd: Path,
    *args: str,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1_000]
        raise RuntimeError(f"git {' '.join(args[:3])} failed ({result.returncode}): {detail}")
    return result


def _git_control_dir(workspace: Path) -> Path:
    return workspace.parent / f".{workspace.name}-evaluation-git"


@contextmanager
def _locked_swe_cache(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _SWE_CACHE_LOCK, lock_path.open("a+b") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_repository_git(
    workspace: Path,
    *args: str,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    control = _git_control_dir(workspace)
    return _run_git(
        workspace,
        "--git-dir",
        str(control),
        "--work-tree",
        str(workspace),
        *args,
        timeout=timeout,
        check=check,
    )


def _initialize_control_repository(workspace: Path) -> Path:
    control = _git_control_dir(workspace)
    if control.exists():
        shutil.rmtree(control)
    control.mkdir(parents=True)
    _run_git(control, "init", "--bare", "--quiet")
    _run_git(control, "config", "core.bare", "false")
    _run_git(control, "config", "core.worktree", str(workspace))
    return control


def _initialize_inline_repository(workspace: Path, files: dict[str, str]) -> None:
    _write_files(workspace, files)
    _initialize_control_repository(workspace)
    _run_repository_git(workspace, "add", "--all")
    _run_repository_git(workspace, "commit", "--quiet", "-m", "evaluation baseline")


def _materialize_swebench_repository(workspace: Path, spec: CodeEvaluationSpec) -> None:
    reference = spec.repository.swebench
    if reference is None:
        raise ValueError("SWE-bench reference is missing")
    cache_root = PuddingClawPaths.from_environment().data() / "evaluation-cache" / "swebench" / "repos"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(reference.repo.encode("utf-8")).hexdigest()[:24]
    bare = cache_root / f"{cache_key}.git"
    github_url = f"https://github.com/{reference.repo}.git"
    with _locked_swe_cache(cache_root / f"{cache_key}.lock"):
        if not bare.exists():
            bare.mkdir(parents=True)
            _run_git(bare, "init", "--bare", "--quiet")
        remotes = _run_git(bare, "remote", check=False).stdout.splitlines()
        if "origin" not in remotes:
            _run_git(bare, "remote", "add", "origin", github_url)
        else:
            _run_git(bare, "remote", "set-url", "origin", github_url)
        _run_git(
            bare,
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=1",
            "origin",
            reference.base_commit,
            timeout=600,
        )
        fetched_commit = _run_git(bare, "rev-parse", "FETCH_HEAD").stdout.strip()
    _initialize_control_repository(workspace)
    _run_repository_git(workspace, "remote", "add", "origin", str(bare))
    _run_repository_git(workspace, "fetch", "--quiet", "--depth=1", "origin", fetched_commit)
    _run_repository_git(workspace, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    _run_repository_git(workspace, "remote", "remove", "origin")
    actual = _run_repository_git(workspace, "rev-parse", "HEAD").stdout.strip().lower()
    if not actual.startswith(reference.base_commit.lower()) and not reference.base_commit.lower().startswith(actual):
        raise RuntimeError("SWE-bench workspace commit does not match the pinned base_commit")


def prepare_code_repository(workspace: Path, spec: CodeEvaluationSpec) -> None:
    if spec.repository.kind == "inline":
        _initialize_inline_repository(workspace, spec.repository.files)
        return
    _materialize_swebench_repository(workspace, spec)


def _validate_workspace_budget(workspace: Path) -> None:
    file_count = 0
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(workspace, followlinks=False):
        directory_names[:] = [name for name in directory_names if not (Path(directory) / name).is_symlink()]
        for name in file_names:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f"Code workspace contains an unsupported file type: {path.name}")
            file_count += 1
            total_bytes += info.st_size
            if file_count > MAX_WORKSPACE_FILES or total_bytes > MAX_WORKSPACE_BYTES:
                raise ValueError("Code workspace exceeds the 250,000 file / 2 GiB verification budget")


def capture_code_patch(workspace: Path) -> tuple[str, list[str]]:
    control = _git_control_dir(workspace)
    if not control.is_dir() or control.is_symlink():
        raise RuntimeError("Platform-owned evaluation Git control directory is missing")
    _validate_workspace_budget(workspace)
    _run_repository_git(workspace, "add", "-N", "--", ".")
    changed_paths = [
        path
        for path in _run_repository_git(workspace, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
        if path
    ]
    patch = _run_repository_git(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "HEAD",
        timeout=120,
    ).stdout
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError(f"Generated code patch exceeds {MAX_PATCH_BYTES} bytes")
    return patch, sorted(set(changed_paths))


def verify_code_case(
    workspace: Path,
    attempt_root: Path,
    spec: CodeEvaluationSpec,
) -> dict[str, Any]:
    patch, changed_paths = capture_code_patch(workspace)
    patch_digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    base: dict[str, Any] = {
        "schema_version": "1",
        "mode": spec.verification.mode,
        "status": "pending",
        "passed": None,
        "patch": patch,
        "patch_sha256": patch_digest,
        "changed_paths": changed_paths,
        "commands": [],
    }
    if spec.verification.require_patch and not patch.strip():
        return {**base, "status": "failed", "passed": False, "reason": "Agent produced no code patch"}
    if spec.verification.mode == "swebench":
        return {
            **base,
            "status": "not_evaluated",
            "reason": "Patch is ready for the official SWE-bench Docker Harness",
        }

    verifier_workspace = attempt_root / "verifier-workspace"
    verifier_control = attempt_root / "verifier-control"
    verifier_scratch = attempt_root / "verifier-scratch"
    if verifier_workspace.exists():
        shutil.rmtree(verifier_workspace)
    if verifier_control.exists():
        shutil.rmtree(verifier_control)
    control = _git_control_dir(workspace)
    patch_file = attempt_root / "candidate.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch, encoding="utf-8")
    # Rebuild the verifier tree from the platform-owned baseline and the
    # captured patch. Never copy the live Agent workspace: a surviving
    # background process could otherwise race the tested snapshot.
    _run_git(
        attempt_root,
        "clone",
        "--quiet",
        "--no-hardlinks",
        str(control),
        str(verifier_workspace),
    )
    if patch.strip():
        _run_git(verifier_workspace, "apply", "--binary", str(patch_file))
    shutil.rmtree(verifier_workspace / ".git")
    verifier_control.mkdir(parents=True)
    verifier_scratch.mkdir(parents=True, exist_ok=True)
    hidden_root = verifier_control / "hidden"
    hidden_root.mkdir()
    _write_files(hidden_root, spec.verification.hidden_files)
    candidate_control = attempt_root / "candidate-control"
    candidate_control.mkdir(parents=True, exist_ok=True)
    runner_path = candidate_control / "python_callable_runner.py"
    runner_path.write_text(_PYTHON_CALLABLE_RUNNER + "\n", encoding="utf-8")
    try:
        from harness.kernel_sandbox import kernel_runner_for_profile
        from harness.sandbox_profiles import SandboxGrantProfile

        max_timeout = max(command.timeout_seconds for command in spec.verification.commands)
        profile = SandboxGrantProfile.build(
            workspace_root=candidate_control,
            scratch_root=verifier_scratch,
            external_read_roots=[verifier_workspace],
            workspace_writable=False,
            network_allowed=False,
            timeout_seconds=max_timeout,
            max_output_bytes=100_000,
            max_processes=128,
        )
        verifier = kernel_runner_for_profile(profile, runtime_root=attempt_root / "kernel-runtime")
        available, reason = type(verifier).probe()
        if not available:
            raise RuntimeError(f"Verifier kernel is unavailable: {reason}")
        command_results: list[dict[str, Any]] = []
        for command in spec.verification.commands:
            if command.runner != "python_callable_json":
                raise ValueError(f"Unsupported verifier runner: {command.runner}")
            test_relative = _safe_relative_path(command.command)
            test_path = hidden_root.joinpath(*test_relative.parts)
            if not test_path.is_file() or test_path.is_symlink():
                raise ValueError(f"Hidden unittest file does not exist: {command.command}")
            callable_ref, hidden_cases = _load_callable_cases(test_path)
            case_receipts: list[dict[str, Any]] = []
            infrastructure_error = False
            for case_index, hidden_case in enumerate(hidden_cases):
                result_path = verifier_scratch / f"{command.command_id}-{case_index}-result.json"
                result_path.unlink(missing_ok=True)
                invocation = shlex.join(
                    [
                        "/usr/bin/python3",
                        "-I",
                        str(runner_path),
                        str(verifier_workspace),
                        callable_ref,
                        json.dumps({"args": hidden_case["args"], "kwargs": hidden_case["kwargs"]}),
                        str(result_path),
                    ]
                )
                result = verifier.execute(invocation, timeout=command.timeout_seconds)
                infrastructure_error = infrastructure_error or result.exit_code in {124, 125, 126, 127} or result.exit_code >= 128
                receipt = _read_receipt(result_path)
                case_receipts.append(
                    {
                        "case_index": case_index,
                        "completed": bool(receipt.get("completed")),
                        "matched": _strict_json_equal(receipt.get("actual"), hidden_case["expected"]),
                        "exit_code": result.exit_code,
                        "runner_error": str(receipt.get("runner_error") or "")[:2_000],
                    }
                )
            receipt_valid = bool(case_receipts) and all(
                item["completed"] and item["matched"] and item["exit_code"] == command.expected_exit_code
                for item in case_receipts
            )
            command_results.append(
                {
                    "command_id": command.command_id,
                    "exit_code": case_receipts[-1]["exit_code"],
                    "expected_exit_code": command.expected_exit_code,
                    "passed": not infrastructure_error
                    and receipt_valid
                    and all(item["exit_code"] == command.expected_exit_code for item in case_receipts),
                    "infrastructure_error": infrastructure_error,
                    "case_receipts": case_receipts,
                }
            )
    except Exception as exc:
        return {
            **base,
            "status": "error",
            "reason": f"Verifier sandbox failed: {type(exc).__name__}: {str(exc)[:500]}",
        }
    if any(item["infrastructure_error"] for item in command_results):
        return {
            **base,
            "status": "error",
            "commands": command_results,
            "reason": "Verifier command timed out or the sandbox runner failed",
        }
    passed = all(item["passed"] for item in command_results)
    return {
        **base,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "commands": command_results,
        "reason": f"Passed {sum(item['passed'] for item in command_results)}/{len(command_results)} verifier commands",
    }
