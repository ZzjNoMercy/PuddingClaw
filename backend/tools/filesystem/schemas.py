"""Function-calling schemas for PuddingClaw filesystem tools."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReplacementHunk(BaseModel):
    old_string: str
    new_string: str
    replace_all: bool = False

    @model_validator(mode="after")
    def strings_differ(self) -> "ReplacementHunk":
        if self.old_string == self.new_string:
            raise ValueError("old_string and new_string must differ")
        return self


class InspectFileVersionInput(BaseModel):
    file_path: str
    include_content: bool = Field(
        default=True,
        description=(
            "Set false when the file content is already known and only the latest "
            "sha256 is needed; set true when content must be read to plan the patch."
        ),
    )


class PatchFileInput(BaseModel):
    file_path: str
    expected_sha256: str | None = Field(
        default=None,
        description=(
            "Optional sha256:<hex> precondition. Omit it for a unique-anchor patch "
            "against the current content; the observed base hash is still recorded."
        ),
    )
    replacements: list[ReplacementHunk] = Field(min_length=1, max_length=100)


class FilePatchSpec(BaseModel):
    file_path: str
    expected_sha256: str
    replacements: list[ReplacementHunk] = Field(min_length=1, max_length=100)


class PatchFilesInput(BaseModel):
    files: list[FilePatchSpec] = Field(min_length=2, max_length=50)


class ReplaceFileInput(BaseModel):
    file_path: str
    content: str
    expected_sha256: str = Field(
        description="sha256:<hex> for the exact file version being replaced"
    )


class CopyFileInput(BaseModel):
    source_path: str = Field(description="Exact authorized source file")
    target_path: str = Field(
        description="Exact new target file; existing targets are never overwritten"
    )
    expected_source_sha256: str | None = Field(
        default=None,
        description=(
            "Optional sha256:<hex> source precondition. The actual copied hash is "
            "always recorded and returned."
        ),
    )


class MaterializeDestination(BaseModel):
    kind: Literal["file", "slot"]
    target_path: str | None = None
    mode: Literal["create", "replace"] = "create"
    expected_sha256: str | None = None
    template_path: str | None = None
    template_sha256: str | None = None
    slot_id: str | None = None
    output_path: str | None = None
    output_mode: Literal["create", "replace"] = "replace"
    expected_output_sha256: str | None = None

    @model_validator(mode="after")
    def validate_destination(self) -> "MaterializeDestination":
        if self.kind == "file":
            if not self.target_path:
                raise ValueError("file destination requires target_path")
            if self.mode == "replace" and not self.expected_sha256:
                raise ValueError("file replace requires expected_sha256")
        else:
            if not all(
                (
                    self.template_path,
                    self.template_sha256,
                    self.slot_id,
                    self.output_path,
                )
            ):
                raise ValueError(
                    "slot destination requires template_path, template_sha256, "
                    "slot_id and output_path"
                )
            if self.output_mode == "replace" and not self.expected_output_sha256:
                raise ValueError("slot output replace requires expected_output_sha256")
        return self


class MaterializeSourceRefInput(BaseModel):
    source_ref: str
    destination: MaterializeDestination
    renderer: Literal["identity", "json", "csv", "js_array", "text"]
    projection: list[str] = Field(default_factory=list)
    expected_schema_ref: str | None = None
    expected_item_count: int | None = Field(default=None, ge=0)


class StageExternalArtifactInput(BaseModel):
    file_path: str = Field(description="Approved absolute external source path")


class CommitExternalArtifactInput(BaseModel):
    lease_id: str
    file_path: str = Field(description="Exact external target bound to the lease")
    expected_source_sha256: str | None = Field(
        default=None,
        description=(
            "Optional lease source hash. Omit it to use the immutable source hash recorded by the lease; "
            "this is not the edited staged-file hash."
        ),
    )
    expected_draft_sha256: str | None = Field(
        default=None,
        description=(
            "Edited staged-file hash. Required for code-like artifacts and must match the exact "
            "bytes covered by validation_receipt_ids."
        ),
    )
    validation_receipt_ids: list[str] = Field(
        default_factory=list,
        description="Server-persisted ValidationReceipt ids authorizing this exact target/draft hash.",
    )


class UpsertScratchFileInput(BaseModel):
    file_path: str = Field(description="Exact /scratch path to create or atomically replace")
    content: str
    expected_sha256: str | None = Field(
        default=None,
        description="Required when replacing an existing scratch file; omit only when creating it",
    )


class ValidateArtifactContractInput(BaseModel):
    contract_id: str = Field(description="Registered deterministic artifact contract id")
    html_file_path: str
    javascript_file_path: str


class ValidateHtmlReportInput(BaseModel):
    html_file_path: str = Field(
        description=(
            "Absolute HTML report path. Ordinary validation reads the report and "
            "its local resources directly; contract-required browser E2E mounts "
            "the exact parent directory read-only in an offline container."
        )
    )
    browser_e2e: bool | None = Field(
        default=None,
        description=(
            "Normally omit this server-owned parameter. Harness resolves it "
            "from the frozen verification contract. An explicit value must "
            "match that contract."
        ),
    )
    timeout: int = Field(default=120, ge=1, le=600)


class RewindExternalFileChangesInput(BaseModel):
    """The active Run scope is supplied by the Backend, not the model."""


class ExecuteExternalDirectoryInput(BaseModel):
    directory_path: str = Field(
        description="Exact authorized external directory bound to this command"
    )
    command: str = Field(
        min_length=1,
        description="Exact shell command; receives a separate command-level approval",
    )
    timeout: int = Field(default=120, ge=1, le=600)
    mode: Literal["read_only", "writable_draft"] = "read_only"
    lease_id: str | None = Field(
        default=None,
        description=(
            "Required for writable_draft. The command writes only to this "
            "server-owned external-directory snapshot, never directly to the host."
        ),
    )
