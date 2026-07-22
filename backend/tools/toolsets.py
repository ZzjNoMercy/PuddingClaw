"""Stable Toolset registry for the DeepAgents runtime.

Toolsets are platform capabilities.  Skills request them through their
frontmatter, while the runtime decides which tools are visible for a model
call.  Keep this registry independent from the skill catalogue so new skills
can reuse the same capability boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# DeepAgents injects the native tools itself.  They are recorded here for a
# complete, inspectable runtime inventory, but are not created by tools/.
NATIVE_TOOLSETS: dict[str, frozenset[str]] = {
    "core_workspace": frozenset({"ls", "read_file", "glob", "grep", "update_todos"}),
    "workspace_write": frozenset({"write_file", "upsert_scratch_file"}),
    "local_execution": frozenset({"execute"}),
    "delegation": frozenset({"task"}),
}

# PuddingClaw extensions that are always available but are grouped for runtime
# inventory and Skill documentation. Declaring one of these toolsets in a Skill
# does not gate or expand access.
UNCONDITIONAL_EXTENSION_TOOLSETS: dict[str, frozenset[str]] = {
    "harness_files": frozenset({
        "inspect_file_version",
        "patch_file",
        "stage_external_artifact",
        "commit_external_artifact",
        "prepare_attachment_edit",
        "publish_attachment",
        "stage_external_directory",
        "prepare_external_directory_commit",
        "commit_external_directory",
        "validate_artifact_contract",
    }),
    "web_research": frozenset({"tavily_search", "fetch_url"}),
    "package_management": frozenset({"install_packages"}),
}

# PuddingClaw tools are opt-in business capabilities.  A name must occur in
# exactly one toolset; this makes accidental expansion of the model tool
# surface visible in review and tests.
BUSINESS_TOOLSETS: dict[str, frozenset[str]] = {
    "skill_management": frozenset({
        "inspect_skill",
        "prepare_skill_install",
        "install_skill",
        "prepare_skill_update",
        "update_skill",
    }),
    "knowledge_analysis": frozenset({"llamaindex_knowledge_query", "pandas_knowledge_query"}),
    "database_analysis": frozenset({
        "database_schema_inspect",
        "database_sql_generate",
        "database_sql_validate",
        "database_sql_execute",
        "database_query_trace_inspect",
        "database_query_result_page",
    }),
    "semantic_lookup": frozenset({"semantic_entity_lookup"}),
    "semantic_dimension_build": frozenset({
        "inspect_dimension_build_input",
        "request_dimension_build_rule",
        "enqueue_semantic_dimension_build",
        "get_semantic_dimension_build_job",
        "publish_semantic_dimension_build",
    }),
    "logical_dataset": frozenset({
        "ensure_attachment_table_asset",
        "list_logical_dataset_candidates",
        "request_logical_dataset_rule",
        "apply_logical_dataset_rule",
    }),
}

TOOLSETS: dict[str, frozenset[str]] = {
    **NATIVE_TOOLSETS,
    **UNCONDITIONAL_EXTENSION_TOOLSETS,
    **BUSINESS_TOOLSETS,
}
DEFAULT_TOOLSETS = frozenset({*NATIVE_TOOLSETS, *UNCONDITIONAL_EXTENSION_TOOLSETS})
# Explicit PuddingClaw extensions that remain available without loading a
# business Skill. They are not DeepAgents-native and keep their own permission
# policies (for example, read_resource's external-file HITL gate).
DEFAULT_CUSTOM_TOOL_NAMES = frozenset({"read_resource"}).union(
    *UNCONDITIONAL_EXTENSION_TOOLSETS.values()
)
NATIVE_TOOL_NAMES = frozenset().union(*NATIVE_TOOLSETS.values())
UNCONDITIONAL_TOOL_NAMES = NATIVE_TOOL_NAMES | DEFAULT_CUSTOM_TOOL_NAMES


@dataclass(frozen=True)
class ToolControlDescriptor:
    """Harness-relevant effects declared at the Tool registration boundary.

    This is intentionally separate from the per-call shell classifier.  A Tool
    has stable product semantics, while an ``execute`` call acquires its exact
    capabilities from the parsed command.
    """

    side_effect: str
    data_classification: str = "internal"
    network_scope: str = "none"
    idempotency: str = "not_applicable"
    approval_scope: str = "none"
    policy: str = "declared"

    def as_dict(self) -> dict[str, str]:
        return {
            "side_effect": self.side_effect,
            "data_classification": self.data_classification,
            "network_scope": self.network_scope,
            "idempotency": self.idempotency,
            "approval_scope": self.approval_scope,
            "policy": self.policy,
        }


_READ_ONLY = ToolControlDescriptor(side_effect="none")
_WORKSPACE_WRITE = ToolControlDescriptor(side_effect="workspace_write", policy="boundary")
_DYNAMIC_EXECUTION = ToolControlDescriptor(
    side_effect="dynamic",
    network_scope="dynamic",
    approval_scope="dynamic",
    policy="dynamic",
)
_DELEGATION = ToolControlDescriptor(side_effect="delegation", policy="inherit_parent")
_CONTROLLED_NETWORK = ToolControlDescriptor(
    side_effect="none",
    data_classification="public",
    network_scope="validated_public",
    approval_scope="mode",
    policy="dynamic",
)
_PACKAGE_INSTALL = ToolControlDescriptor(
    side_effect="runtime_dependency_write",
    network_scope="package_registry",
    idempotency="best_effort",
    approval_scope="session",
    policy="dynamic",
)
_EXTERNAL_COMMIT = ToolControlDescriptor(
    side_effect="external_mutation",
    idempotency="required",
    approval_scope="call",
    policy="external_lease",
)
_ATTACHMENT_PUBLISH = ToolControlDescriptor(
    side_effect="artifact_publish",
    idempotency="required",
    approval_scope="none",
    policy="attachment_lease",
)
_SKILL_PREPARE = ToolControlDescriptor(
    side_effect="staging_write",
    network_scope="declared_source",
    idempotency="required",
    approval_scope="call",
    policy="dynamic",
)
_SKILL_COMMIT = ToolControlDescriptor(
    side_effect="managed_skill_write",
    idempotency="required",
    approval_scope="call",
    policy="dynamic",
)
_INTERNAL_MUTATION = ToolControlDescriptor(
    side_effect="internal_mutation",
    idempotency="required",
    approval_scope="none",
    policy="tool_contract",
)


TOOL_CONTROL_DESCRIPTORS: dict[str, ToolControlDescriptor] = {
    # Native Harness / DeepAgents capabilities.
    "ls": _READ_ONLY,
    "read_file": _READ_ONLY,
    "glob": _READ_ONLY,
    "grep": _READ_ONLY,
    "update_todos": _INTERNAL_MUTATION,
    "write_file": _WORKSPACE_WRITE,
    "edit_file": _WORKSPACE_WRITE,
    "execute": _DYNAMIC_EXECUTION,
    "task": _DELEGATION,
    # Harness file protocol.
    "inspect_file_version": _READ_ONLY,
    "patch_file": _WORKSPACE_WRITE,
    "upsert_scratch_file": _WORKSPACE_WRITE,
    "stage_external_artifact": _READ_ONLY,
    "commit_external_artifact": _EXTERNAL_COMMIT,
    "prepare_attachment_edit": _WORKSPACE_WRITE,
    "publish_attachment": _ATTACHMENT_PUBLISH,
    "stage_external_directory": _READ_ONLY,
    "prepare_external_directory_commit": _READ_ONLY,
    "commit_external_directory": _EXTERNAL_COMMIT,
    "validate_artifact_contract": _INTERNAL_MUTATION,
    "read_resource": _READ_ONLY,
    # Controlled network and runtime setup.
    "tavily_search": _CONTROLLED_NETWORK,
    "fetch_url": _CONTROLLED_NETWORK,
    "install_packages": _PACKAGE_INSTALL,
    # Skill management.
    "inspect_skill": _READ_ONLY,
    "prepare_skill_install": _SKILL_PREPARE,
    "install_skill": _SKILL_COMMIT,
    "prepare_skill_update": _SKILL_PREPARE,
    "update_skill": _SKILL_COMMIT,
    # Read-only business analysis.
    "llamaindex_knowledge_query": _READ_ONLY,
    "pandas_knowledge_query": _READ_ONLY,
    "database_schema_inspect": _READ_ONLY,
    "database_sql_generate": _READ_ONLY,
    "database_sql_validate": _READ_ONLY,
    "database_sql_execute": _READ_ONLY,
    "database_query_trace_inspect": _READ_ONLY,
    "database_query_result_page": _READ_ONLY,
    "semantic_entity_lookup": _READ_ONLY,
    "inspect_dimension_build_input": _READ_ONLY,
    "get_semantic_dimension_build_job": _READ_ONLY,
    "list_logical_dataset_candidates": _READ_ONLY,
    # Internal, idempotent control-plane mutations.
    "request_dimension_build_rule": _INTERNAL_MUTATION,
    "enqueue_semantic_dimension_build": _INTERNAL_MUTATION,
    "publish_semantic_dimension_build": _INTERNAL_MUTATION,
    "ensure_attachment_table_asset": _INTERNAL_MUTATION,
    "request_logical_dataset_rule": _INTERNAL_MUTATION,
    "apply_logical_dataset_rule": _INTERNAL_MUTATION,
}


def tool_control_descriptor(tool_name: str) -> ToolControlDescriptor | None:
    return TOOL_CONTROL_DESCRIPTORS.get(str(tool_name or ""))


def validate_tool_control_descriptors() -> list[str]:
    """Return registered Tool names that lack a mandatory control contract."""

    registered = set(UNCONDITIONAL_TOOL_NAMES) | set(business_tool_names()) | {"edit_file"}
    return sorted(registered - set(TOOL_CONTROL_DESCRIPTORS))


def tools_for_toolsets(toolsets: Iterable[str]) -> frozenset[str]:
    """Return the tool names declared by the requested known toolsets."""
    return frozenset().union(*(TOOLSETS.get(name, frozenset()) for name in toolsets))


def business_tool_names() -> frozenset[str]:
    return tools_for_toolsets(BUSINESS_TOOLSETS)


def agent_custom_tool_names() -> frozenset[str]:
    """Return the single-source registration set for PuddingClaw Agent tools."""
    return business_tool_names() | DEFAULT_CUSTOM_TOOL_NAMES


def validate_toolset_names(toolsets: Iterable[str]) -> list[str]:
    return sorted({name for name in toolsets if name not in TOOLSETS})
