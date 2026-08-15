"""Assemble PuddingClaw filesystem tools for the DeepAgents runtime."""

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from tools.filesystem.copy import build_copy_tools
from tools.filesystem.inspect import build_inspect_tools
from tools.filesystem.leases import build_lease_tools
from tools.filesystem.patch import build_patch_tools
from tools.filesystem.validation import build_validation_tools


class VersionedPatchMiddleware(AgentMiddleware[Any, Any, Any]):
    """Expose PuddingClaw filesystem tools through the DeepAgents middleware slot."""

    def __init__(self, backend: Any, *, compact_model_surface: bool = False) -> None:
        super().__init__()
        groups = (
            build_inspect_tools(backend),
            build_patch_tools(backend),
            build_copy_tools(backend),
            build_lease_tools(backend),
            build_validation_tools(backend),
        )
        by_name = {tool.name: tool for group in groups for tool in group}
        internal_order = (
            "inspect_file_version",
            "patch_file",
            "replace_file",
            "copy_file",
            "materialize_source_ref",
            "patch_files",
            "rewind_external_file_changes",
            "execute_external_directory",
            "validate_html_report",
            "stage_external_artifact",
            "commit_external_artifact",
            "upsert_scratch_file",
            "validate_artifact_contract",
        )
        # Product Agents use native write_file plus standard shell cp/mv/mkdir/rm.
        # Keep the broader HostFileBroker adapters callable internally and in
        # focused tests, but do not make the model choose among overlapping
        # copy/replace/stage/transaction orchestration surfaces.
        model_order = (
            "patch_file",
            "materialize_source_ref",
            "validate_html_report",
            "validate_artifact_contract",
        )
        order = model_order if compact_model_surface else internal_order
        self.tools = [by_name[name] for name in order]
