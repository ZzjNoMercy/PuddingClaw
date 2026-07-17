"""Stable Toolset registry for the DeepAgents runtime.

Toolsets are platform capabilities.  Skills request them through their
frontmatter, while the runtime decides which tools are visible for a model
call.  Keep this registry independent from the skill catalogue so new skills
can reuse the same capability boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable

# DeepAgents injects the native tools itself.  They are recorded here for a
# complete, inspectable runtime inventory, but are not created by tools/.
NATIVE_TOOLSETS: dict[str, frozenset[str]] = {
    "core_workspace": frozenset({"ls", "read_file", "glob", "grep", "write_todos"}),
    "workspace_write": frozenset({"write_file", "edit_file"}),
    "local_execution": frozenset({"execute"}),
    "delegation": frozenset({"task"}),
}

# PuddingClaw extensions that are always available but are grouped for runtime
# inventory and Skill documentation. Declaring one of these toolsets in a Skill
# does not gate or expand access.
UNCONDITIONAL_EXTENSION_TOOLSETS: dict[str, frozenset[str]] = {
    "web_research": frozenset({"tavily_search", "fetch_url"}),
    "package_management": frozenset({"install_packages"}),
    "skill_inspection": frozenset({"inspect_skill"}),
}

# PuddingClaw tools are opt-in business capabilities.  A name must occur in
# exactly one toolset; this makes accidental expansion of the model tool
# surface visible in review and tests.
BUSINESS_TOOLSETS: dict[str, frozenset[str]] = {
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
