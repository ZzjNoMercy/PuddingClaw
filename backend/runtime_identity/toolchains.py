"""Shared user Toolchain resource management."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from runtime_identity.paths import PuddingClawPaths


@dataclass(frozen=True)
class ToolchainRef:
    ecosystem: str
    runtime_contract: str
    host_path: Path
    root_path: Path
    mount_path: Path
    container_path: str = "/opt/puddingclaw/toolchain/node"


class ToolchainManager:
    """Locate, lock and record a shared Toolchain resource."""

    def __init__(self, paths: PuddingClawPaths, runtime_contract: str) -> None:
        self.paths = paths
        self.runtime_contract = runtime_contract

    def resolve_node(self) -> ToolchainRef:
        self.paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.paths.root, 0o700)
        root = self.paths.node_toolchain(self.runtime_contract)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        releases = root / "releases"
        releases.mkdir(exist_ok=True, mode=0o700)
        current = root / "current"
        if not current.exists():
            empty = releases / "empty"
            (empty / "bin").mkdir(parents=True, exist_ok=True)
            (empty / "lib" / "node_modules").mkdir(parents=True, exist_ok=True)
            temporary = root / f".current-{uuid.uuid4().hex}"
            temporary.symlink_to(Path("releases") / "empty", target_is_directory=True)
            try:
                os.replace(temporary, current)
            except OSError:
                temporary.unlink(missing_ok=True)
                if not current.exists():
                    raise
        return ToolchainRef("node", self.runtime_contract, current.resolve(), root, current)

    def install_lark(self, backend: object, distribution: str):
        ref = self.resolve_node()
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            release_id = f"release-{int(time.time())}-{uuid.uuid4().hex[:12]}"
            release = ref.root_path / "releases" / release_id
            (release / "bin").mkdir(parents=True, exist_ok=False)
            (release / "lib" / "node_modules").mkdir(parents=True, exist_ok=True)
            result = backend.install_managed_node_cli(
                distribution=distribution,
                toolchain_path=release,
                container_path=ref.container_path,
            )
            if result.exit_code == 0:
                manifest = {
                    "version": 1,
                    "ecosystem": "node",
                    "runtime_contract": ref.runtime_contract,
                    "packages": {
                        "@larksuite/cli": {
                            "distribution": distribution,
                            "executable": "lark-cli",
                            "installed_at": time.time(),
                            "version_output": str(result.output).strip()[-1000:],
                        }
                    },
                }
                temporary = release / ".toolchain-manifest.json.tmp"
                temporary.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.chmod(temporary, 0o600)
                os.replace(temporary, release / "toolchain-manifest.json")
                next_current = ref.root_path / f".current-{uuid.uuid4().hex}"
                next_current.symlink_to(Path("releases") / release_id, target_is_directory=True)
                os.replace(next_current, ref.root_path / "current")
            else:
                # Failed releases are intentionally retained for diagnosis;
                # they are never reachable through ``current``.
                failure = release / "INSTALL_FAILED"
                failure.write_text(str(result.output)[-20_000:], encoding="utf-8")
            return result
