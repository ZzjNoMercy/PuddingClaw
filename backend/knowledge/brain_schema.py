"""LLM Wiki Schema Bundle and official gbrain schema-pack support.

The gbrain custom pack remains an official ``gbrain-schema-pack-v1`` file.
PuddingClaw-specific Wiki/Agent rules live beside it in ``brain.schema.yaml``.
Both files are versioned and hashed as one bundle, but are never conflated.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from gbrain_runtime import gbrain_subprocess_environment, resolve_gbrain_binary
from knowledge.paths import get_knowledge_root

SCHEMA_API_VERSION = "gbrain-schema-pack-v1"
DEFAULT_CUSTOM_PACK = "puddingclaw-wiki"
DEFAULT_PARENT_PACK = "gbrain-base-v2"
PACK_NAME_RE = re.compile(r"^[a-z0-9._-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
GBRAIN_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
PRIMITIVES = ("entity", "media", "temporal", "annotation", "concept")
AGGREGATORS = ("scalar_brier", "weighted_brier", "count_based", "cluster_summary")


class BrainSchemaError(RuntimeError):
    """Raised for catalog, validation, or bundle persistence failures."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractableSpec(StrictModel):
    prompt_template: str | None = None
    fixture_corpus: str | None = None
    eval_dimensions: list[str] = Field(default_factory=list)
    benchmark_min_recall: float | None = Field(default=None, ge=0, le=1)
    verifier_path: str | None = None


class SubtypeWhen(StrictModel):
    path_pattern: str | None = None
    frontmatter_field: str | None = None
    frontmatter_value: str | int | float | bool | None = None


class PageSubtype(StrictModel):
    name: str = Field(min_length=1)
    when: SubtypeWhen


class PageType(StrictModel):
    name: str = Field(min_length=1)
    primitive: Literal["entity", "media", "temporal", "annotation", "concept"]
    path_prefixes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    extractable: bool | ExtractableSpec = False
    expert_routing: bool = False
    subtypes: list[PageSubtype] | None = None


def workspace_page_prefixes(page_type: PageType | dict[str, Any]) -> list[str]:
    """Project gbrain paths into paths relative to PuddingClaw's wiki/ root.

    Some official packs describe paths from a whole-brain import root and
    therefore include ``wiki/``. PuddingClaw imports the contents of its
    ``wiki/`` directory as the gbrain source root, so retaining that segment
    would create ``wiki/wiki/...`` and incorrect page slugs.
    """

    raw_prefixes = page_type.path_prefixes if isinstance(page_type, PageType) else page_type.get("path_prefixes", [])
    normalized: list[str] = []
    for value in raw_prefixes:
        prefix = str(value).strip().lstrip("/")
        if prefix.startswith("wiki/"):
            prefix = prefix.removeprefix("wiki/")
        if prefix and prefix not in normalized:
            normalized.append(prefix)
    return normalized


class LinkInference(StrictModel):
    regex: str | None = None
    page_type: str | None = None
    target_type: str | None = None


class LinkType(StrictModel):
    name: str = Field(min_length=1)
    inverse: str | None = None
    inference: LinkInference | None = None


class FrontmatterLink(StrictModel):
    page_type: str
    fields: list[str] = Field(min_length=1)
    link_type: str


class EnrichableType(StrictModel):
    type: str
    rubric: str | None = None


class FilingRule(StrictModel):
    kind: str
    directory: str
    examples: list[str] = Field(default_factory=list)
    description: str | None = None


class BorrowFrom(StrictModel):
    pack: str
    types: list[str] | None = None
    link_types: list[str] | None = None


class CalibrationDomain(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    aggregator: Literal["scalar_brier", "weighted_brier", "count_based", "cluster_summary"]
    page_types: list[str] = Field(min_length=1)


class MigrationFrom(StrictModel):
    pack: str = Field(min_length=1)
    version: str = Field(min_length=1)


class FrontmatterFieldResolver(StrictModel):
    frontmatter_field: str = Field(min_length=1)


ResolverSpec = Literal["frontmatter", "body_first_link", "slug", "body_excerpt"] | FrontmatterFieldResolver


class RetypeRule(StrictModel):
    kind: Literal["retype"]
    from_type: str = Field(min_length=1)
    to_type: str = Field(min_length=1)
    subtype: str | None = None
    subtype_field: Literal["subtype", "legacy_type", "origin", "format", "kind", "period", "domain"] = "subtype"
    path_filter: str | None = None


class PageToLinkRule(StrictModel):
    kind: Literal["page_to_link"]
    from_type: str = Field(min_length=1)
    link_type: str = Field(min_length=1)
    source_slug_from: ResolverSpec
    target_slug_from: ResolverSpec
    inverse: str | None = None
    preserve_notes: bool | None = None


class PageToAliasRule(StrictModel):
    kind: Literal["page_to_alias"]
    from_type: str = Field(min_length=1)
    canonical_from: ResolverSpec
    alias_slug_from: ResolverSpec
    notes_from: ResolverSpec | None = None


MappingRule = Annotated[RetypeRule | PageToLinkRule | PageToAliasRule, Field(discriminator="kind")]


class SchemaPackManifest(StrictModel):
    """Python mirror of gbrain's official SchemaPackManifest v1."""

    api_version: Literal["gbrain-schema-pack-v1"] = SCHEMA_API_VERSION
    name: str = Field(min_length=1)
    version: str
    description: str = ""
    author: str | None = None
    license: str | None = None
    homepage: AnyUrl | None = None
    gbrain_min_version: str = "0.38.0"
    extends: str | None = "gbrain-base"
    borrow_from: list[BorrowFrom] = Field(default_factory=list)
    page_types: list[PageType] = Field(default_factory=list)
    link_types: list[LinkType] = Field(default_factory=list)
    frontmatter_links: list[FrontmatterLink] = Field(default_factory=list)
    takes_kinds: list[str] = Field(default_factory=lambda: ["fact", "take", "bet", "hunch"])
    enrichable_types: list[EnrichableType] = Field(default_factory=list)
    filing_rules: list[FilingRule] = Field(default_factory=list)
    phases: list[str] | None = None
    calibration_domains: list[CalibrationDomain] | None = None
    migration_from: MigrationFrom | None = None
    mapping_rules: list[MappingRule] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PACK_NAME_RE.fullmatch(value):
            raise ValueError("must be a lowercase slug using a-z, 0-9, dot, underscore, or hyphen")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not SEMVER_RE.fullmatch(value):
            raise ValueError("must be semver M.m.p")
        return value

    @field_validator("gbrain_min_version")
    @classmethod
    def validate_gbrain_version(cls, value: str) -> str:
        if not GBRAIN_VERSION_RE.fullmatch(value):
            raise ValueError("must be M.m.p or M.m.p.b")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> SchemaPackManifest:
        own_types = {item.name for item in self.page_types}
        if len(own_types) != len(self.page_types):
            raise ValueError("page_types contains duplicate names")
        own_links = {item.name for item in self.link_types}
        if len(own_links) != len(self.link_types):
            raise ValueError("link_types contains duplicate names")
        prefixes: dict[str, str] = {}
        for page_type in self.page_types:
            for prefix in page_type.path_prefixes:
                owner = prefixes.get(prefix)
                if owner and owner != page_type.name:
                    raise ValueError(f"path_prefix {prefix!r} is declared by both {owner!r} and {page_type.name!r}")
                prefixes[prefix] = page_type.name
        return self


class WikiContract(StrictModel):
    # ``flat`` remains readable for already-created Brains. New saves project
    # the Schema Pack's path_prefixes into the typed directory layout.
    layout: Literal["flat", "typed_directories"] = "typed_directories"
    allowed_page_types: list[str] = Field(default_factory=lambda: ["concept", "system", "debate"])
    allowed_link_types: list[str] = Field(default_factory=lambda: ["relates_to", "supports", "challenges"])
    required_frontmatter: list[str] = Field(
        default_factory=lambda: ["title", "type", "sources", "created", "updated", "schema_version"]
    )


class GbrainPackReference(StrictModel):
    path: str
    name: str
    version: str
    manifest_sha256: str


class BrainSchemaDocument(StrictModel):
    schema_id: str = DEFAULT_CUSTOM_PACK
    bundle_version: str = "0.1.0"
    gbrain_pack: GbrainPackReference
    wiki: WikiContract = Field(default_factory=WikiContract)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_yaml_mapping(text: str, *, source: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BrainSchemaError(f"Invalid YAML in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrainSchemaError(f"{source} must contain a YAML object")
    return value


class _IndentedSafeDumper(yaml.SafeDumper):
    """Emit sequence items indented below their mapping key.

    gbrain's YAML loader expects the conventional ``page_types:\n  - name``
    shape; PyYAML's default indentless sequences are valid YAML but are not
    accepted by that loader.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> int:
        return super().increase_indent(flow, False)


def _dump_yaml(value: dict[str, Any]) -> str:
    # Stable bytes are more important than alphabetic key sorting. Pydantic's
    # declaration order mirrors the official manifest and page/link arrays
    # retain user order because gbrain prefix inference is first-match-wins.
    return yaml.dump(
        value,
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
        default_flow_style=False,
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


@contextmanager
def _file_lock(path: Path, *, shared: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _model_dict(model: BaseModel, *, exclude_none: bool = True) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=exclude_none)


class BrainSchemaService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()

    @property
    def brain_root(self) -> Path:
        return get_knowledge_root(self.base_dir) / "llm-wiki"

    @property
    def schema_root(self) -> Path:
        return self.brain_root / "schema"

    @property
    def custom_pack_path(self) -> Path:
        return self.schema_root / "gbrain" / DEFAULT_CUSTOM_PACK / "pack.yaml"

    @property
    def brain_schema_path(self) -> Path:
        return self.schema_root / "brain.schema.yaml"

    @property
    def bundle_lock_path(self) -> Path:
        return self.brain_root / ".puddingclaw" / "locks" / "schema-bundle.lock"

    @property
    def brain_write_lock_path(self) -> Path:
        return self.brain_root / ".puddingclaw" / "locks" / "brain-write.lock"

    def _bundled_dir(self) -> Path:
        override = os.getenv("PUDDINGCLAW_GBRAIN_SCHEMA_DIR", "").strip()
        if override:
            candidate = Path(override).expanduser().resolve()
            if candidate.is_dir():
                return candidate
            raise BrainSchemaError(f"PUDDINGCLAW_GBRAIN_SCHEMA_DIR is not a directory: {candidate}")

        executable = resolve_gbrain_binary()
        candidates: list[Path] = []
        if executable:
            resolved = Path(executable).resolve()
            # Global Bun installs point at <package>/src/cli.ts. Compiled builds
            # may place the binary beside an unpacked schema-pack resource dir.
            candidates.extend(
                [
                    resolved.parent / "core" / "schema-pack" / "base",
                    resolved.parent / "schema-packs" / "base",
                    resolved.parent.parent / "resources" / "gbrain-schema-packs",
                ]
            )
        candidates.extend(
            [
                self.base_dir.parent.parent / "源码合集" / "gbrain" / "src" / "core" / "schema-pack" / "base",
                self.base_dir / "resources" / "gbrain-schema-packs",
            ]
        )
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.glob("gbrain-*.yaml")):
                return candidate.resolve()
        raise BrainSchemaError(
            "Cannot locate gbrain bundled schema packs. Set PUDDINGCLAW_GBRAIN_SCHEMA_DIR or install gbrain."
        )

    def _read_pack(self, path: Path) -> tuple[SchemaPackManifest, str, str]:
        raw = path.read_text(encoding="utf-8")
        manifest = SchemaPackManifest.model_validate(_load_yaml_mapping(raw, source=str(path)))
        return manifest, raw, _sha256_text(raw)

    def catalog(self) -> dict[str, Any]:
        directory = self._bundled_dir()
        packs: list[dict[str, Any]] = []
        for path in sorted(directory.glob("gbrain-*.yaml")):
            manifest, raw, digest = self._read_pack(path)
            packs.append(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "gbrain_min_version": manifest.gbrain_min_version,
                    "extends": manifest.extends,
                    "borrow_from": _model_dict(manifest).get("borrow_from", []),
                    "manifest_sha256": digest,
                    "page_type_count": len(manifest.page_types),
                    "link_type_count": len(manifest.link_types),
                    "manifest": _model_dict(manifest),
                    "raw_yaml": raw,
                    "recommended": manifest.name == DEFAULT_PARENT_PACK,
                    "legacy": manifest.name != DEFAULT_PARENT_PACK and self._root_parent_name(manifest.name, directory) == "gbrain-base",
                }
            )
        if not packs:
            raise BrainSchemaError(f"No bundled gbrain manifests found under {directory}")
        packs.sort(key=lambda item: (not item["recommended"], item["name"]))
        return {"source_dir": str(directory), "packs": packs, "count": len(packs)}

    def _catalog_manifests(self, directory: Path | None = None) -> dict[str, SchemaPackManifest]:
        root = directory or self._bundled_dir()
        result: dict[str, SchemaPackManifest] = {}
        for path in root.glob("gbrain-*.yaml"):
            manifest, _raw, _digest = self._read_pack(path)
            result[manifest.name] = manifest
        return result

    def _root_parent_name(self, name: str, directory: Path) -> str:
        manifests = self._catalog_manifests(directory)
        seen: set[str] = set()
        current = manifests.get(name)
        while current and current.extends:
            if current.name in seen:
                return current.name
            seen.add(current.name)
            current = manifests.get(current.extends)
        return current.name if current else name

    def _default_custom_manifest(self) -> SchemaPackManifest:
        return SchemaPackManifest(
            name=DEFAULT_CUSTOM_PACK,
            version="0.1.0",
            description="PuddingClaw LLM Wiki custom schema pack",
            gbrain_min_version="0.42.0",
            extends=DEFAULT_PARENT_PACK,
            page_types=[
                PageType(
                    name="system",
                    primitive="entity",
                    path_prefixes=["systems/"],
                    aliases=["agent-system", "ai-system", "agent-product"],
                ),
                PageType(
                    name="debate",
                    primitive="concept",
                    path_prefixes=["debates/"],
                    aliases=["comparison", "controversy", "tradeoff"],
                ),
                PageType(
                    name="research_paper",
                    primitive="media",
                    path_prefixes=["papers/"],
                    aliases=["paper", "arxiv-paper", "technical-report"],
                ),
                PageType(
                    name="software_framework",
                    primitive="entity",
                    path_prefixes=["frameworks/"],
                    aliases=["agent-framework", "ai-framework", "sdk"],
                ),
                PageType(
                    name="ai_model",
                    primitive="entity",
                    path_prefixes=["models/"],
                    aliases=["model", "llm", "foundation-model"],
                ),
                PageType(
                    name="programming_language",
                    primitive="entity",
                    path_prefixes=["languages/"],
                    aliases=["language", "programming-language"],
                ),
                PageType(
                    name="engineering_practice",
                    primitive="concept",
                    path_prefixes=["practices/"],
                    aliases=["practice", "engineering-experience", "lesson-learned", "best-practice"],
                ),
            ],
            link_types=[
                LinkType(name="supports", inverse="supported_by"),
                LinkType(name="challenges", inverse="challenged_by"),
                LinkType(name="introduces", inverse="introduced_by"),
                LinkType(name="implements", inverse="implemented_by"),
                LinkType(name="uses", inverse="used_by"),
                LinkType(name="depends_on", inverse="dependency_of"),
                LinkType(name="evaluates", inverse="evaluated_by"),
                LinkType(name="applies_to", inverse="applied_by"),
            ],
        )

    @staticmethod
    def _wiki_contract_for(resolved: SchemaPackManifest, current: WikiContract | None = None) -> WikiContract:
        """Project the resolved official types into the Wiki/Agent contract.

        The official pack remains the type-system source of truth. Wiki rules
        can narrow layout/frontmatter policy, but must not maintain a second
        hand-edited page/link vocabulary that drifts from gbrain.
        """

        required_frontmatter = (
            list(current.required_frontmatter)
            if current
            else ["title", "type", "sources", "created", "updated", "schema_version"]
        )
        if "created" not in required_frontmatter:
            insertion = required_frontmatter.index("updated") if "updated" in required_frontmatter else len(required_frontmatter)
            required_frontmatter.insert(insertion, "created")
        return WikiContract(
            layout="typed_directories",
            allowed_page_types=[item.name for item in resolved.page_types],
            allowed_link_types=[item.name for item in resolved.link_types],
            required_frontmatter=required_frontmatter,
        )

    def initialize(self) -> dict[str, Any]:
        with _file_lock(self.bundle_lock_path):
            return self._initialize_unlocked()

    def rebuild_agents(self) -> dict[str, Any]:
        """Rebuild the generated AGENTS.md projection from the active Brain Schema."""

        with _file_lock(self.bundle_lock_path):
            if not self.brain_schema_path.is_file() or not self.custom_pack_path.is_file():
                raise BrainSchemaError("LLM Wiki Brain has not been initialized")
            brain_schema = BrainSchemaDocument.model_validate(
                _load_yaml_mapping(
                    self.brain_schema_path.read_text(encoding="utf-8"),
                    source=str(self.brain_schema_path),
                )
            )
            agents_path = self.brain_root / "AGENTS.md"
            custom, _raw, _digest = self._read_pack(self.custom_pack_path)
            _atomic_write(agents_path, self._render_agents(brain_schema, self.resolve_manifest(custom)))
            return self._bundle_unlocked()

    def _initialize_unlocked(self) -> dict[str, Any]:
        self.catalog()  # Fail before writing if the pinned runtime is unavailable.
        for directory in (
            self.brain_root / "raw",
            self.brain_root / "wiki",
            self.brain_root / ".puddingclaw" / "staging",
            self.brain_root / ".puddingclaw" / "jobs",
            self.brain_root / ".puddingclaw" / "locks",
            self.schema_root / "versions",
            self.schema_root / "compiled",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        if not self.custom_pack_path.exists():
            manifest = self._default_custom_manifest()
            _atomic_write(self.custom_pack_path, _dump_yaml(_model_dict(manifest)))
        manifest, raw, digest = self._read_pack(self.custom_pack_path)
        resolved = self.resolve_manifest(manifest)
        brain_schema = BrainSchemaDocument(
            gbrain_pack=GbrainPackReference(
                path=str(self.custom_pack_path.relative_to(self.schema_root)),
                name=manifest.name,
                version=manifest.version,
                manifest_sha256=digest,
            ),
            wiki=self._wiki_contract_for(resolved),
        )
        if not self.brain_schema_path.exists():
            _atomic_write(self.brain_schema_path, _dump_yaml(_model_dict(brain_schema)))
        else:
            brain_schema = BrainSchemaDocument.model_validate(
                _load_yaml_mapping(
                    self.brain_schema_path.read_text(encoding="utf-8"),
                    source=str(self.brain_schema_path),
                )
            )
        for path, initial in (
            (self.brain_root / "raw" / "manifest.jsonl", ""),
            (self.brain_root / "wiki" / "index.md", "# Wiki Index\n"),
            (self.brain_root / "wiki" / "log.md", "# Wiki Ingest Log\n"),
        ):
            if not path.exists():
                _atomic_write(path, initial)
        # AGENTS.md is a generated projection, not user-authored Wiki content.
        # Rebuild it idempotently so template upgrades cannot strand old Brains.
        _atomic_write(self.brain_root / "AGENTS.md", self._render_agents(brain_schema, resolved))
        return self._bundle_unlocked()

    def _render_agents(
        self,
        schema: BrainSchemaDocument,
        resolved: SchemaPackManifest | None = None,
    ) -> str:
        if resolved is None:
            custom, _raw, _digest = self._read_pack(self.custom_pack_path)
            resolved = self.resolve_manifest(custom)
        page_types = ", ".join(schema.wiki.allowed_page_types)
        link_types = ", ".join(schema.wiki.allowed_link_types)
        fields = ", ".join(schema.wiki.required_frontmatter)
        path_rules = [
            f"- `{page_type.name}`：{'、'.join(f'`{prefix}<slug>.md`' for prefix in prefixes)}。"
            for page_type in resolved.page_types
            if (prefixes := workspace_page_prefixes(page_type))
        ]
        path_section = "\n".join(path_rules)
        return (
            "# LLM Wiki Agent 操作契约\n\n"
            f"> schema: {schema.schema_id}@{schema.bundle_version}\n\n"
            "## 所有权与操作边界\n\n"
            "- `raw/` 只读，严禁修改、重命名或删除其中的内容。\n"
            "- Ingest 只能在 `.puddingclaw/staging/` 下写入补丁；由发布流程更新 `wiki/`。\n"
            "- Query 和 Lint 均为只读操作。\n"
            "- `wiki/log.md` 只允许追加，禁止改写已有记录。\n\n"
            "## Schema 约束\n\n"
            f"- 允许的 page types：{page_types}。\n"
            f"- 允许的 link types：{link_types}。\n"
            f"- 必填 frontmatter：{fields}。\n"
            "- Wiki 页面按 Page Type 的 `path_prefixes` 分目录存放；文件名 slug 使用小写连字符格式。\n"
            f"{path_section}\n"
            "- 所有 wikilink 必须写出与 gbrain 页面 slug 完全一致的目录前缀："
            "使用 `[[<type-directory>/<slug>]]` 或 `[[<type-directory>/<slug>|显示文本]]`；"
            "禁止裸写 `[[<slug>]]`，否则 gbrain 无法抽取关系边。\n\n"
            "## AI Agent Wiki 分类准则\n\n"
            "- `research_paper`：论文、预印本和技术报告；保留作者、年份、URL/DOI/arXiv 标识及核心结论。\n"
            "- `software_framework`：LangGraph、AutoGen 等具体软件框架；抽象方法论仍使用 `concept`。\n"
            "- `ai_model`：具体模型或模型系列；模型背后的技术方法使用 `concept`。\n"
            "- `programming_language`：Python、TypeScript、Rust 等具体编程语言。\n"
            "- `system`：具体 Agent 系统、产品或可运行实现；项目过程使用 `project`。\n"
            "- `engineering_practice`：从开发记录中提炼的可复用工程经验，必须说明问题、根因、方案、验证、适用范围和失效条件。\n"
            "- 普通文章、博客、视频等来源优先使用内置 `media` 或 `source`，不要重复增加 `article`。\n"
            "- 一份 raw 可以编译出多个 Wiki 页面；一个 Wiki 页面只描述一个长期稳定的主题，不按 raw 文件机械地一对一建页。\n"
            "- 优先使用 `introduces`、`implements`、`uses`、`depends_on`、`evaluates`、`applies_to`、"
            "`supports`、`challenges` 以及内置关系；关系两端只使用相对 `wiki/` 根目录的完整页面 slug，"
            "例如 `[[concepts/compiled-rag]]`，不得再次添加 `wiki/`。\n\n"
            "## Ingest（摄取）\n\n"
            "开始前读取 `AGENTS.md`、当前 Schema Bundle、本次选中的 raw 文件和现有 Wiki。"
            "生成 staging patch、更新索引覆盖情况，并追加一条 Ingest 日志。"
            "每个新增或更新的页面必须引用至少一个本次选中的 raw 文件；"
            "sources 只能填写 `raw/manifest.jsonl` 中不可变的精确 `snapshot_path`；"
            "直接复制 context 返回的路径，例如 `manual-upload-.../document.md`，不要添加 `raw/` 前缀。\n\n"
            "## Query（查询）\n\n"
            "先读取 `wiki/index.md`，再按需读取相关 Wiki 页面；Query 期间不得读取 raw。"
            "回答时引用 Wiki slug 和 sources；若 Wiki 信息不足，明确报告知识缺口。\n\n"
            "## Lint（检查）\n\n"
            "报告无效 frontmatter、未知类型、断链、孤立页面、索引遗漏、日志改写和过期 raw hash。"
            "Lint 不得修改任何文件。\n"
        )

    @staticmethod
    def _merge_by_key(layers: list[list[dict[str, Any]]], key: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for layer in layers:
            for item in layer:
                value = str(item.get(key, ""))
                if value in seen:
                    continue
                seen.add(value)
                output.append(deepcopy(item))
        return output

    def resolve_manifest(self, custom: SchemaPackManifest) -> SchemaPackManifest:
        manifests = self._catalog_manifests()
        manifests[custom.name] = custom
        ancestors: list[SchemaPackManifest] = []
        current_name = custom.extends
        seen = {custom.name}
        while current_name:
            if current_name in seen:
                raise BrainSchemaError(f"Schema extends cycle detected at {current_name}")
            seen.add(current_name)
            parent = manifests.get(current_name)
            if parent is None:
                raise BrainSchemaError(f"Unknown parent schema pack: {current_name}")
            ancestors.append(parent)
            if len(ancestors) > 8:
                raise BrainSchemaError("Schema extends chain exceeds gbrain hard cap of 8")
            current_name = parent.extends
        ancestors.reverse()  # base first

        borrowed_pages: list[dict[str, Any]] = []
        borrowed_links: list[dict[str, Any]] = []
        for spec in custom.borrow_from:
            target = manifests.get(spec.pack)
            if target is None:
                raise BrainSchemaError(f"Unknown borrowed schema pack: {spec.pack}")
            target_dict = _model_dict(target)
            type_filter = set(spec.types or [])
            link_filter = set(spec.link_types or [])
            borrowed_pages.extend(
                item for item in target_dict["page_types"] if type_filter and item["name"] in type_filter
            )
            borrowed_links.extend(
                item for item in target_dict["link_types"] if link_filter and item["name"] in link_filter
            )

        custom_dict = _model_dict(custom)
        ancestor_dicts = [_model_dict(item) for item in ancestors]
        base_pages = ancestor_dicts[0]["page_types"] if ancestor_dicts else []
        base_by_name = {item["name"]: deepcopy(item) for item in base_pages}
        base_order = [item["name"] for item in base_pages]
        middle = ancestor_dicts[1:]
        for layer in [*[item["page_types"] for item in middle], borrowed_pages, custom_dict["page_types"]]:
            for item in layer:
                if item["name"] in base_by_name:
                    base_by_name[item["name"]] = deepcopy(item)
        new_pages: list[dict[str, Any]] = []
        new_seen: set[str] = set()
        for layer in [custom_dict["page_types"], borrowed_pages, *[item["page_types"] for item in reversed(middle)]]:
            for item in layer:
                name = item["name"]
                if name in base_by_name or name in new_seen:
                    continue
                new_seen.add(name)
                new_pages.append(deepcopy(item))
        resolved_pages = new_pages + [base_by_name[name] for name in base_order]

        ancestors_high = list(reversed(ancestor_dicts))
        resolved = deepcopy(custom_dict)
        resolved["page_types"] = resolved_pages
        resolved["link_types"] = self._merge_by_key(
            [custom_dict["link_types"], borrowed_links, *[item["link_types"] for item in ancestors_high]], "name"
        )
        for field, key in (
            ("enrichable_types", "type"),
            ("filing_rules", "kind"),
        ):
            resolved[field] = self._merge_by_key(
                [custom_dict[field], *[item[field] for item in ancestors_high]], key
            )
        front_seen: set[tuple[str, str]] = set()
        resolved_front: list[dict[str, Any]] = []
        for layer in [custom_dict["frontmatter_links"], *[item["frontmatter_links"] for item in ancestors_high]]:
            for item in layer:
                key = (item["page_type"], item["link_type"])
                if key not in front_seen:
                    front_seen.add(key)
                    resolved_front.append(deepcopy(item))
        resolved["frontmatter_links"] = resolved_front
        take_seen: set[str] = set()
        resolved_takes: list[str] = []
        for layer in [custom_dict["takes_kinds"], *[item["takes_kinds"] for item in ancestors_high]]:
            for take in layer:
                if take not in take_seen:
                    take_seen.add(take)
                    resolved_takes.append(take)
        resolved["takes_kinds"] = resolved_takes
        return SchemaPackManifest.model_validate(resolved)

    def validate_with_gbrain(self, manifest: SchemaPackManifest) -> list[dict[str, Any]]:
        """Run gbrain's own shape, semantic lint, and resolver gates."""

        binary = resolve_gbrain_binary()
        if not binary:
            raise BrainSchemaError("gbrain CLI is required to validate an official Schema Pack")
        raw = _dump_yaml(_model_dict(manifest))
        receipts: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="puddingclaw-schema-") as temporary:
            home = Path(temporary)
            pack_dir = home / ".gbrain" / "schema-packs" / manifest.name
            pack_dir.mkdir(parents=True)
            (pack_dir / "pack.yaml").write_text(raw, encoding="utf-8")
            environment = gbrain_subprocess_environment(binary)
            environment["GBRAIN_HOME"] = str(home)
            environment["GBRAIN_SCHEMA_PACK"] = manifest.name
            for command in (
                [binary, "schema", "validate", manifest.name, "--json"],
                [binary, "schema", "lint", manifest.name, "--json"],
                [binary, "schema", "active", "--json"],
            ):
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=20,
                    check=False,
                )
                receipt = {
                    "command": command[1:],
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-8000:],
                    "stderr": result.stderr[-8000:],
                }
                receipts.append(receipt)
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip()
                    raise BrainSchemaError(f"gbrain official Schema validation failed: {detail}")
                if command[2:4] == ["lint", manifest.name]:
                    try:
                        lint_payload = json.loads(result.stdout)
                    except json.JSONDecodeError:
                        lint_payload = None
                    if isinstance(lint_payload, dict) and lint_payload.get("ok") is False:
                        raise BrainSchemaError(
                            f"gbrain official Schema lint failed: {json.dumps(lint_payload, ensure_ascii=False)}"
                        )
        return receipts

    def bundle(self) -> dict[str, Any]:
        with _file_lock(self.bundle_lock_path, shared=True):
            return self._bundle_unlocked()

    def _bundle_unlocked(self) -> dict[str, Any]:
        if not self.custom_pack_path.exists() or not self.brain_schema_path.exists():
            raise BrainSchemaError("LLM Wiki Brain has not been initialized")
        custom, custom_raw, custom_hash = self._read_pack(self.custom_pack_path)
        brain_raw = self.brain_schema_path.read_text(encoding="utf-8")
        brain_schema = BrainSchemaDocument.model_validate(
            _load_yaml_mapping(brain_raw, source=str(self.brain_schema_path))
        )
        agents_path = self.brain_root / "AGENTS.md"
        if not agents_path.is_file():
            raise BrainSchemaError("LLM Wiki AGENTS.md is missing")
        agents_raw = agents_path.read_text(encoding="utf-8")
        expected_agents_raw = self._render_agents(brain_schema, self.resolve_manifest(custom))
        if agents_raw != expected_agents_raw:
            raise BrainSchemaError("LLM Wiki AGENTS.md does not match the active Schema Bundle")
        if brain_schema.gbrain_pack.name != custom.name or brain_schema.gbrain_pack.version != custom.version:
            raise BrainSchemaError("brain.schema.yaml pack identity does not match official custom pack")
        if brain_schema.gbrain_pack.manifest_sha256 != custom_hash:
            raise BrainSchemaError("brain.schema.yaml custom pack hash is stale")
        resolved = self.resolve_manifest(custom)
        resolved_raw = _dump_yaml(_model_dict(resolved))
        parent = next(
            (item for item in self.catalog()["packs"] if item["name"] == custom.extends),
            None,
        )
        parent_hash = str(parent["manifest_sha256"]) if parent else "none"
        bundle_hash = _sha256_text(
            f"{_sha256_text(brain_raw)}:{parent_hash}:{custom_hash}:"
            f"{_sha256_text(resolved_raw)}:{_sha256_text(agents_raw)}"
        )
        return {
            "initialized": True,
            "brain_root": str(self.brain_root),
            "custom": {
                "path": str(self.custom_pack_path),
                "manifest": _model_dict(custom),
                "raw_yaml": custom_raw,
                "manifest_sha256": custom_hash,
            },
            "parent": (
                {
                    "name": parent["name"],
                    "version": parent["version"],
                    "manifest_sha256": parent_hash,
                }
                if parent
                else None
            ),
            "brain_schema": {
                "path": str(self.brain_schema_path),
                "document": _model_dict(brain_schema),
                "raw_yaml": brain_raw,
                "sha256": _sha256_text(brain_raw),
            },
            "agents": {
                "path": str(agents_path),
                "raw_markdown": agents_raw,
                "sha256": _sha256_text(agents_raw),
            },
            "resolved": {
                "manifest": _model_dict(resolved),
                "raw_yaml": resolved_raw,
                "sha256": _sha256_text(resolved_raw),
            },
            "bundle_hash": bundle_hash,
        }

    def save_custom(
        self,
        payload: dict[str, Any],
        *,
        expected_sha256: str,
        expected_bundle_hash: str,
    ) -> dict[str, Any]:
        with _file_lock(self.brain_write_lock_path):
            with _file_lock(self.bundle_lock_path):
                return self._save_custom_unlocked(
                    payload,
                    expected_sha256=expected_sha256,
                    expected_bundle_hash=expected_bundle_hash,
                )

    def _prepare_wiki_schema_migration(
        self,
        *,
        previous_version: str,
        next_version: str,
        resolved: SchemaPackManifest,
        required_frontmatter: list[str],
    ) -> dict[Path, str]:
        """Re-pin a compatible Wiki as one schema-save transaction.

        Only the generated ``schema_version`` line changes. A destructive
        schema edit fails before activation so the current Wiki never becomes
        an unrecoverable mix of old and new contracts.
        """

        wiki_dir = self.brain_root / "wiki"
        allowed_types = {item.name for item in resolved.page_types}
        updates: dict[Path, str] = {}
        for path in sorted(wiki_dir.rglob("*.md")):
            relative = path.relative_to(wiki_dir)
            if relative.as_posix() in {"index.md", "log.md"}:
                continue
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---\n"):
                raise BrainSchemaError(f"Cannot migrate {relative}: missing YAML frontmatter")
            end = content.find("\n---\n", 4)
            if end < 0:
                raise BrainSchemaError(f"Cannot migrate {relative}: unterminated YAML frontmatter")
            metadata = _load_yaml_mapping(content[4:end], source=str(path))
            missing = [field for field in required_frontmatter if metadata.get(field) in (None, "", [])]
            if missing:
                raise BrainSchemaError(f"Cannot migrate {relative}: missing frontmatter {', '.join(missing)}")
            page_type = str(metadata.get("type") or "")
            if page_type not in allowed_types:
                raise BrainSchemaError(
                    f"Cannot activate Schema {next_version}: wiki/{relative} uses removed type {page_type!r}"
                )
            page_version = str(metadata.get("schema_version") or "")
            if page_version not in {previous_version, next_version}:
                raise BrainSchemaError(
                    f"Cannot migrate {relative}: schema_version {page_version!r} is not active version "
                    f"{previous_version!r}"
                )
            migrated, count = re.subn(
                r"(?m)^schema_version\s*:\s*.*$",
                f"schema_version: {next_version}",
                content,
            )
            if count != 1:
                raise BrainSchemaError(f"Cannot migrate {relative}: expected one schema_version field")
            updates[path] = migrated
        if updates:
            log_path = wiki_dir / "log.md"
            prior_log = log_path.read_text(encoding="utf-8")
            separator = "" if prior_log.endswith("\n") else "\n"
            entry = (
                f"## [{datetime.now(UTC).date().isoformat()}] schema-migrate\n\n"
                f"- from: {previous_version}\n"
                f"- to: {next_version}\n"
                f"- pages: {', '.join(path.relative_to(wiki_dir).with_suffix('').as_posix() for path in updates)}\n"
            )
            updates[log_path] = prior_log + separator + entry
        return updates

    def _validate_wiki_migration_with_gbrain(
        self,
        manifest: SchemaPackManifest,
        updates: dict[Path, str],
    ) -> None:
        pages = {path: content for path, content in updates.items() if path.name not in {"index.md", "log.md"}}
        if not pages:
            return
        binary = resolve_gbrain_binary()
        if not binary:
            raise BrainSchemaError("gbrain CLI is required to validate a Wiki schema migration")
        with tempfile.TemporaryDirectory(prefix="puddingclaw-wiki-schema-migrate-") as temporary:
            root = Path(temporary)
            pack_dir = root / ".gbrain" / "schema-packs" / manifest.name
            source_root = root / "source"
            pack_dir.mkdir(parents=True)
            source_root.mkdir(parents=True)
            (pack_dir / "pack.yaml").write_text(_dump_yaml(_model_dict(manifest)), encoding="utf-8")
            for path, content in pages.items():
                relative = path.relative_to(self.brain_root / "wiki")
                destination = source_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            environment = gbrain_subprocess_environment(binary)
            environment["GBRAIN_HOME"] = str(root)
            environment["GBRAIN_SCHEMA_PACK"] = manifest.name
            result = subprocess.run(
                [binary, "lint", str(source_root)],
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
                check=False,
            )
            issue_match = re.search(r"\b(\d+) issue\(s\)", result.stdout)
            if result.returncode != 0 or (issue_match and int(issue_match.group(1)) > 0):
                detail = (result.stderr or result.stdout).strip()
                raise BrainSchemaError(f"Cannot activate Schema: migrated Wiki fails gbrain lint: {detail}")

    def _save_custom_unlocked(
        self,
        payload: dict[str, Any],
        *,
        expected_sha256: str,
        expected_bundle_hash: str,
    ) -> dict[str, Any]:
        if not self.custom_pack_path.exists():
            raise BrainSchemaError("LLM Wiki Brain has not been initialized")
        current_bundle = self._bundle_unlocked()
        if current_bundle["bundle_hash"] != expected_bundle_hash:
            raise BrainSchemaError("Schema Bundle changed since this draft was loaded")
        current_raw = self.custom_pack_path.read_text(encoding="utf-8")
        current_hash = _sha256_text(current_raw)
        if expected_sha256 != current_hash:
            raise BrainSchemaError("Custom pack changed since this draft was loaded")
        manifest = SchemaPackManifest.model_validate(payload)
        if manifest.name != DEFAULT_CUSTOM_PACK:
            raise BrainSchemaError(f"P0 keeps the custom pack name fixed as {DEFAULT_CUSTOM_PACK!r}")
        current_manifest = SchemaPackManifest.model_validate(current_bundle["custom"]["manifest"])
        current_semver = tuple(int(part) for part in current_manifest.version.split("."))
        next_semver = tuple(int(part) for part in manifest.version.split("."))
        next_raw = _dump_yaml(_model_dict(manifest))
        if next_raw != current_raw and next_semver <= current_semver:
            raise BrainSchemaError(
                "Schema content changed; version must be bumped to a SemVer greater than "
                f"{current_manifest.version}"
            )
        resolved = self.resolve_manifest(manifest)  # Parent/borrow/cycle validation before writes.
        self.validate_with_gbrain(manifest)
        raw = next_raw
        digest = _sha256_text(raw)

        brain_raw = self.brain_schema_path.read_text(encoding="utf-8")
        brain = BrainSchemaDocument.model_validate(
            _load_yaml_mapping(brain_raw, source=str(self.brain_schema_path))
        )
        updated = brain.model_copy(
            update={
                "bundle_version": manifest.version,
                "gbrain_pack": GbrainPackReference(
                    path=str(self.custom_pack_path.relative_to(self.schema_root)),
                    name=manifest.name,
                    version=manifest.version,
                    manifest_sha256=digest,
                ),
                "wiki": self._wiki_contract_for(resolved, brain.wiki),
            }
        )
        updated_brain_raw = _dump_yaml(_model_dict(updated))
        agents_path = self.brain_root / "AGENTS.md"
        agents_raw = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        updated_agents_raw = self._render_agents(updated, resolved)
        wiki_updates: dict[Path, str] = {}
        if manifest.version != current_manifest.version:
            wiki_updates = self._prepare_wiki_schema_migration(
                previous_version=current_manifest.version,
                next_version=manifest.version,
                resolved=resolved,
                required_frontmatter=updated.wiki.required_frontmatter,
            )
            self._validate_wiki_migration_with_gbrain(manifest, wiki_updates)
        wiki_originals = {path: path.read_text(encoding="utf-8") for path in wiki_updates}
        try:
            _atomic_write(self.custom_pack_path, raw)
            _atomic_write(self.brain_schema_path, updated_brain_raw)
            _atomic_write(agents_path, updated_agents_raw)
            for path, content in wiki_updates.items():
                _atomic_write(path, content)
        except Exception:
            # Process-level failures are rolled back while the exclusive lock
            # is held. Crash recovery/version pointers remain a later hardening.
            _atomic_write(self.custom_pack_path, current_raw)
            _atomic_write(self.brain_schema_path, brain_raw)
            _atomic_write(agents_path, agents_raw)
            for path, content in wiki_originals.items():
                _atomic_write(path, content)
            raise
        result = self._bundle_unlocked()
        result["schema_migration"] = {
            "from_version": current_manifest.version,
            "to_version": manifest.version,
            "migrated_pages": sorted(
                path.relative_to(self.brain_root / "wiki").with_suffix("").as_posix()
                for path in wiki_updates
                if path.relative_to(self.brain_root / "wiki").as_posix() not in {"index.md", "log.md"}
            ),
        }
        return result

    def preview_custom(
        self,
        payload: dict[str, Any],
        *,
        validate_official: bool = True,
    ) -> dict[str, Any]:
        manifest = SchemaPackManifest.model_validate(payload)
        if manifest.name != DEFAULT_CUSTOM_PACK:
            raise BrainSchemaError(f"P0 keeps the custom pack name fixed as {DEFAULT_CUSTOM_PACK!r}")
        resolved = self.resolve_manifest(manifest)
        # Interactive previews use the exact strict DTO and deterministic
        # resolver without launching three CLI processes on every pause.
        # save_custom remains the authoritative official gbrain gate.
        official_validation = self.validate_with_gbrain(manifest) if validate_official else []
        custom_raw = _dump_yaml(_model_dict(manifest))
        resolved_raw = _dump_yaml(_model_dict(resolved))
        return {
            "valid": True,
            "custom": {
                "manifest": _model_dict(manifest),
                "raw_yaml": custom_raw,
                "manifest_sha256": _sha256_text(custom_raw),
            },
            "resolved": {
                "manifest": _model_dict(resolved),
                "raw_yaml": resolved_raw,
                "sha256": _sha256_text(resolved_raw),
            },
            "gbrain_validation": official_validation,
            "validation_mode": "official" if validate_official else "structural",
        }


def get_brain_schema_service(base_dir: Path) -> BrainSchemaService:
    return BrainSchemaService(base_dir)
