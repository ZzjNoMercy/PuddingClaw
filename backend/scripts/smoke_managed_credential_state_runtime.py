"""Real-Docker probe for Adapter-owned credential-state round trips.

The probe uses a synthetic Lark App ID/Secret in two disposable containers.
It never reads or writes a real Credential Profile. Container A exercises the
Linux keychain fallback and exports only the Adapter-declared roots. Container
B imports that archive and proves the CLI no longer reports a missing
``client_secret`` before contacting the provider.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from harness.workspace_backends import DEFAULT_SANDBOX_IMAGE
from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.profiles import validate_credential_archive
from runtime_identity.toolchains import ToolchainManager


def _docker_args(toolchain: Path, probe: Path, *, network: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=32m",
        "--tmpfs",
        "/home/puddingclaw:rw,nosuid,nodev,size=128m",
        "--mount",
        f"type=bind,src={toolchain},dst=/opt/puddingclaw/toolchain/node,readonly",
        "--mount",
        f"type=bind,src={probe},dst=/probe",
        "--env",
        "HOME=/home/puddingclaw",
        "--env",
        "LARKSUITE_CLI_DATA_DIR=/home/puddingclaw/.lark-cli/.credential-data",
        "--env",
        "PATH=/opt/puddingclaw/toolchain/node/bin:/usr/local/bin:/usr/bin:/bin",
        DEFAULT_SANDBOX_IMAGE,
        "sh",
        "-c",
    ]


def main() -> int:
    state = ManagedCliRegistry.credential_state_for_provider("lark")
    toolchain = ToolchainManager(
        PuddingClawPaths.from_environment(),
        "python3.12+node22+chromium-v4",
    ).resolve_node().host_path
    with tempfile.TemporaryDirectory(prefix="puddingclaw-credential-state-") as temporary:
        probe = Path(temporary).resolve()
        roots = " ".join(state.paths)
        writer = subprocess.run(
            [
                *_docker_args(toolchain, probe, network="none"),
                (
                    "umask 077; "
                    "mkdir -p /home/puddingclaw/.lark-cli /home/puddingclaw/.local/share/lark-cli; "
                    "lark-cli config init --app-id cli_dummyprobe --app-secret-stdin --brand feishu; "
                    f"tar -czf /probe/state.tar.gz -C /home/puddingclaw -- {roots}"
                ),
            ],
            input="puddingclaw-dummy-secret\n",
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        archive_path = probe / "state.tar.gz"
        if writer.returncode != 0 or not archive_path.exists():
            raise RuntimeError(writer.stderr.strip() or writer.stdout.strip() or "state writer failed")
        archive = validate_credential_archive(
            archive_path.read_bytes(),
            allowed_roots=state.paths,
        )

        import tarfile
        from io import BytesIO

        with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as bundle:
            names = set(bundle.getnames())
        required = {
            ".lark-cli/config.json",
            ".lark-cli/.credential-data/lark-cli/master.key",
            ".lark-cli/.credential-data/lark-cli/appsecret_cli_dummyprobe.enc",
        }
        if not required.issubset(names):
            raise RuntimeError(f"credential archive is incomplete: {sorted(required - names)}")

        reader = subprocess.run(
            [
                *_docker_args(toolchain, probe, network="bridge"),
                (
                    "umask 077; "
                    "tar -xzf /probe/state.tar.gz -C /home/puddingclaw "
                    "--no-same-owner --no-same-permissions; "
                    "lark-cli auth login --recommend --no-wait --json"
                ),
            ],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        combined = f"{reader.stdout}\n{reader.stderr}"
        missing_client_secret = "missing a required parameter: client_secret" in combined
        print(
            json.dumps(
                {
                    "archive_valid": True,
                    "keychain_files_captured": True,
                    "reader_exit_code": reader.returncode,
                    "missing_client_secret": missing_client_secret,
                },
                sort_keys=True,
            )
        )
        return 1 if missing_client_secret else 0


if __name__ == "__main__":
    raise SystemExit(main())
