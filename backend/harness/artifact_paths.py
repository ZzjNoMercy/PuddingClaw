"""Parse user-declared local artifacts without treating paths as shell tokens."""

from __future__ import annotations

import re
from pathlib import Path

LOCAL_RESOURCE_SUFFIXES = tuple(
    sorted(
        {
            ".bmp",
            ".csv",
            ".doc",
            ".docx",
            ".gif",
            ".html",
            ".ipynb",
            ".jpeg",
            ".jpg",
            ".js",
            ".json",
            ".markdown",
            ".md",
            ".pdf",
            ".png",
            ".py",
            ".ppt",
            ".pptx",
            ".tif",
            ".tiff",
            ".tsv",
            ".txt",
            ".webp",
            ".sql",
            ".svg",
            ".tar",
            ".ts",
            ".tsx",
            ".xls",
            ".xlsx",
            ".xml",
            ".yaml",
            ".yml",
            ".zip",
        },
        key=len,
        reverse=True,
    )
)

_DIRECT_ACTIONS = (
    "修改",
    "编辑",
    "更新",
    "刷新",
    "重写",
    "替换",
    "覆盖",
    "同步",
    "保存到",
    "保存",
    "写入",
    "输出到",
    "输出",
    "生成到",
)
_REFERENCE_PREFIXES = ("这个", "该", "上述", "前面的")
_FOLLOWING_TARGET_ACTIONS = ("保存到", "写入", "输出到", "生成到")
_NEGATED_ACTION_RE = re.compile(
    r"(?:不要|请勿|切勿|不可|不再|不必|无需|不需要|禁止|仅查看|只读)\s*"
    r"(?:修改|编辑|更新|刷新|重写|替换|覆盖|同步|保存|写入|输出|生成)\s*$"
)
_POSIX_ROOT_PREFIXES = (
    "/Users/",
    "/home/",
    "/workspace/",
    "/Volumes/",
    "/private/",
    "/tmp/",
    "/var/",
    "/mnt/",
    "/opt/",
)
_VIRTUAL_RESOURCE_PREFIXES = ("/workspace", "/scratch", "/skills", "/memories")
_SCRIPT_SRC_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"'](?P<src>[^\"']+\.js(?:\?[^\"']*)?)[\"']",
    re.IGNORECASE,
)


def extract_local_resource_paths(message: str) -> list[str]:
    """Extract pasted local filenames, preserving spaces up to a known suffix."""

    return [item[0] for item in _path_spans(message)]


def extract_local_directory_paths(message: str) -> list[str]:
    """Extract existing absolute directories mentioned in free-form text.

    Directory names have no reliable suffix, so candidates are validated
    against the host filesystem and the longest matching prefix wins.
    """

    found: list[str] = []
    occupied: list[tuple[int, int]] = []
    file_spans = _path_spans(message)
    for start in _path_starts(message):
        if message.startswith(_VIRTUAL_RESOURCE_PREFIXES, start):
            continue
        # A precise file path also has existing directory prefixes. Do not
        # reinterpret its parent as an explicitly supplied directory.
        if any(file_start <= start < file_end for _path, file_start, file_end in file_spans):
            continue
        if any(existing_start <= start < existing_end for existing_start, existing_end in occupied):
            continue
        for end in range(len(message), start, -1):
            raw = message[start:end].replace("\\ ", " ").strip().strip("`'\"()（）[]【】{},，。；;:：")
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            try:
                if not candidate.is_absolute() or not candidate.is_dir():
                    continue
                resolved = str(candidate.resolve())
            except OSError:
                continue
            if resolved not in found:
                found.append(resolved)
            occupied.append((start, start + len(raw)))
            break
    return found


def extract_declared_artifact_targets(message: str) -> list[str]:
    """Return explicit write/update targets, excluding ordinary input paths."""

    targets: list[str] = []
    for path, start, end in _path_spans(message):
        before = message[max(0, start - 20) : start].strip(" `\t\r\n，,：:")
        after = message[end : min(len(message), end + 30)].strip(" `\t\r\n，,：:")
        direct_before = any(action in before for action in _DIRECT_ACTIONS)
        if _NEGATED_ACTION_RE.search(before):
            direct_before = False
        direct_after = any(
            after.startswith(action) for action in _DIRECT_ACTIONS
        ) and not any(
            after.startswith(action) for action in _FOLLOWING_TARGET_ACTIONS
        )
        referential_after = any(
            after.startswith(f"{action}{reference}") for action in _DIRECT_ACTIONS for reference in _REFERENCE_PREFIXES
        )
        if direct_before or direct_after or referential_after:
            targets.append(path)
    return targets


def resolve_declared_artifact_targets(message: str) -> list[str]:
    """Resolve explicit targets plus deterministic versioned-copy targets.

    A request such as "参考 /path/report.html，开一个新的 V2 版本（包含
    HTML 和 JS）" names an input rather than spelling out the two output
    filenames. Treating that as an open-ended artifact task weakens delivery
    verification. This resolver derives the sibling V2 HTML and its local
    script companion without asking the model to invent acceptance scope.
    """

    targets = extract_declared_artifact_targets(message)
    version_match = re.search(r"(?i)(v\d+)", message)
    if (
        version_match is None
        or not re.search(r"(?:新|另存|副本|版本)", message, re.IGNORECASE)
    ):
        return targets

    version = version_match.group(1).lower()
    requested_year = _requested_version_year(message, version)
    lowered = message.lower()
    wants_html = "html" in lowered
    wants_js = bool(re.search(r"(?<![a-z])js(?![a-z])", lowered))
    explicit_targets = set(targets)
    for raw_path in extract_local_resource_paths(message):
        source = Path(raw_path).expanduser()
        suffix = source.suffix.lower()
        if (
            suffix in {".html", ".htm"}
            and wants_html
            and explicit_targets
            and raw_path not in explicit_targets
        ):
            # An explicit output path is authoritative. A preceding reference
            # template remains an input and must not be version-expanded into
            # another acceptance target.
            pass
        elif suffix in {".html", ".htm"} and wants_html:
            _append_unique(
                targets,
                str(_versioned_sibling(source, version, requested_year)),
            )
        if suffix in {".js", ".cjs", ".mjs"} and wants_js:
            _append_unique(
                targets,
                str(_versioned_sibling(source, version, requested_year)),
            )
        if suffix not in {".html", ".htm"} or not wants_js:
            continue
        for script in _local_script_sources(source):
            if _versioned_application_script(script):
                _append_unique(
                    targets,
                    str(_versioned_sibling(script, version, requested_year)),
                )
    return targets


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _requested_version_year(message: str, version: str) -> str | None:
    """Return a year explicitly coupled to the requested new version."""

    coupled = re.search(
        rf"(?i)(?:新(?:的)?|另存|创建|生成|开一个).{{0,16}}"
        rf"(?P<year>20\d{{2}})\s*{re.escape(version)}",
        message,
    )
    return coupled.group("year") if coupled else None


def _versioned_sibling(
    path: Path,
    version: str,
    requested_year: str | None = None,
) -> Path:
    normalized = version.lower()
    stem = path.stem
    if requested_year:
        if re.search(r"20\d{2}", stem):
            stem = re.sub(r"20\d{2}", requested_year, stem)
        else:
            stem = re.sub(
                r"(?i)(?:模型模板|报告模板|[-_.\s]*template)$",
                "",
                stem,
            ).rstrip("-_. ")
            separator = "-" if stem.isascii() and " " not in stem else "_"
            stem = f"{stem}{separator}{requested_year}"
    if re.search(rf"(?i)(?:[-_.\s]){re.escape(normalized)}$", stem):
        return path.with_name(f"{stem}{path.suffix}")
    if not stem:
        return path
    separator = "-" if stem.isascii() and " " not in stem else "_"
    return path.with_name(f"{stem}{separator}{normalized}{path.suffix}")


def _versioned_application_script(path: Path) -> bool:
    """Keep report-owned scripts while reusing generated/vendor dependencies."""

    normalized = path.as_posix().lower()
    name = path.name.lower()
    if "/node_modules/" in normalized:
        return False
    if any(marker in name for marker in (".min.js", ".min.cjs", ".min.mjs")):
        return False
    if re.match(r"^(?:vendor|runtime|polyfills?|chunk)(?:[.\-_]|$)", name):
        return False
    return True


def _local_script_sources(html_path: Path) -> list[Path]:
    try:
        if not html_path.is_file() or html_path.stat().st_size > 2 * 1024 * 1024:
            return []
        content = html_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    scripts: list[Path] = []
    for match in _SCRIPT_SRC_RE.finditer(content):
        raw_src = match.group("src").split("?", 1)[0].strip()
        if not raw_src or "://" in raw_src or raw_src.startswith(("//", "data:")):
            continue
        candidate = (
            Path(raw_src).expanduser()
            if Path(raw_src).is_absolute()
            else html_path.parent / raw_src
        )
        if candidate not in scripts:
            scripts.append(candidate)
    return scripts


def artifact_path_matches(candidate: str, declared: str) -> bool:
    """Compare literal, expanded host, and normalized virtual paths."""

    if candidate == declared:
        return True
    if candidate.replace("\\", "/") == declared.replace("\\", "/"):
        return True
    try:
        if Path(candidate).expanduser().is_absolute() and Path(declared).expanduser().is_absolute():
            return Path(candidate).expanduser().resolve() == Path(declared).expanduser().resolve()
    except OSError:
        return False
    return False


def _path_spans(message: str) -> list[tuple[str, int, int]]:
    starts = _path_starts(message)

    lowered = message.lower()
    found: list[tuple[str, int, int]] = []
    for start in starts:
        if any(existing_start <= start < existing_end for _, existing_start, existing_end in found):
            continue
        best_end: int | None = None
        for suffix in LOCAL_RESOURCE_SUFFIXES:
            search_from = start
            while True:
                suffix_at = lowered.find(suffix, search_from)
                if suffix_at < 0:
                    break
                end = suffix_at + len(suffix)
                next_char = message[end : end + 1]
                if (
                    not next_char
                    or next_char.isspace()
                    or next_char in "`'\")]）】},，。；;:："
                    or "\u4e00" <= next_char <= "\u9fff"
                ):
                    best_end = end if best_end is None else min(best_end, end)
                    break
                search_from = end
        if best_end is None:
            continue
        path = message[start:best_end].replace("\\ ", " ").strip().strip("`'\"")
        if path and all(existing[0] != path for existing in found):
            found.append((path, start, best_end))
    return found


def _path_starts(message: str) -> list[int]:
    starts: list[int] = []
    for index, char in enumerate(message):
        if char == "/" or char == "~":
            suffix = message[index:]
            is_known_root = any(suffix.startswith(prefix) for prefix in _POSIX_ROOT_PREFIXES)
            if char == "/" and (
                (index > 0 and message[index - 1] == ":" and message[index : index + 2] == "//")
                or (index >= 2 and message[index - 2 : index + 1] == "://")
            ):
                continue
            if (
                index == 0
                or message[index - 1].isspace()
                or message[index - 1] in "`'\"([：:，,"
                or "\u4e00" <= message[index - 1] <= "\u9fff"
                or is_known_root
            ):
                starts.append(index)
        elif (
            char.isalpha()
            and index + 2 < len(message)
            and message[index + 1] == ":"
            and message[index + 2] in "\\/"
            # ``https://.../guide.md`` contains the substring ``s:/``.
            # A Windows drive must start at a token boundary and has exactly
            # one slash after the colon; otherwise a web URL becomes a fake
            # local path such as ``backend/s:/open.feishu.cn/...``.
            and message[index + 2 : index + 4] not in {"//", "\\\\"}
            and (
                index == 0
                or message[index - 1].isspace()
                or message[index - 1] in "`'\"([：:，,"
                or "\u4e00" <= message[index - 1] <= "\u9fff"
            )
        ):
            starts.append(index)

    return starts


__all__ = [
    "artifact_path_matches",
    "extract_declared_artifact_targets",
    "extract_local_directory_paths",
    "extract_local_resource_paths",
    "resolve_declared_artifact_targets",
]
