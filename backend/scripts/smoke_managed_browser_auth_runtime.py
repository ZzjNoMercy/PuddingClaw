"""Real-Docker smoke test for the managed blocking BrowserAuth runner.

The test uses the installed shared Toolchain, an empty temporary workspace,
and an isolated synthetic Profile identity. It never reads or writes a real
Credential Profile and always removes the browser container before exiting.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from harness.workspace_backends import ProjectSandboxManager, _lark_config_verification_url
from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.authorization_drivers import LarkAuthorizationDriver
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.toolchains import ToolchainManager


def main() -> int:
    manager = ProjectSandboxManager({})
    available, reason = manager.probe()
    if not available:
        raise RuntimeError(f"Docker is unavailable: {reason}")
    toolchain = ToolchainManager(
        PuddingClawPaths.from_environment(),
        manager.runtime_contract,
    ).resolve_node()
    owner_user_id = f"smoke-{uuid.uuid4().hex[:8]}"
    profile_id = "lark_smoke"
    result = None
    credential_state_spec = ManagedCliRegistry.credential_state_for_provider("lark")
    with tempfile.TemporaryDirectory(prefix="puddingclaw-browser-auth-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        runtime_image_digest = manager.managed_runtime_image_digest(workspace)
        try:
            result = manager.run_managed_browser_auth_cli(
                workspace,
                argv=["lark-cli", "config", "init", "--new"],
                environment={
                    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
                    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                },
                credential_state_spec=credential_state_spec,
                toolchain_path=toolchain.host_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=b"",
                owner_user_id=owner_user_id,
                provider="lark",
                profile_id=profile_id,
                adapter_id="lark-cli",
                authorization_contract_fingerprint=LarkAuthorizationDriver.contract_fingerprint,
                expected_runtime_image_digest=runtime_image_digest,
            )
            print(
                json.dumps(
                    {
                        "browser_status": result.browser_status,
                        "exit_code": result.exit_code,
                        "has_verification_url": _lark_config_verification_url(result.output) is not None,
                    },
                    sort_keys=True,
                )
            )
            return 0 if result.browser_status == "awaiting_user_browser" else 1
        finally:
            if result is not None and result.browser_job_id:
                manager.finalize_managed_browser_auth_cli(
                    owner_user_id=owner_user_id,
                    provider="lark",
                    profile_id=profile_id,
                    browser_job_id=result.browser_job_id,
                )


if __name__ == "__main__":
    raise SystemExit(main())
