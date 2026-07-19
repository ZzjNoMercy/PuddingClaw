"""Parse user-declared local artifacts without treating paths as shell tokens."""

from __future__ import annotations

import re
from pathlib import Path


LOCAL_RESOURCE_SUFFIXES = tuple(
    sorted(
        {
            ".bmp", ".csv", ".doc", ".docx", ".gif", ".html", ".ipynb",
            ".jpeg", ".jpg", ".js", ".json", ".markdown", ".md", ".pdf", ".png", ".py",
            ".ppt", ".pptx", ".tif", ".tiff", ".tsv", ".txt", ".webp",
            ".sql", ".svg", ".tar", ".ts", ".tsx", ".xls", ".xlsx",
            ".xml", ".yaml", ".yml", ".zip",
        },
        key=len,
        reverse=True,
    )
)

_DIRECT_ACTIONS = (
    "修改", "编辑", "更新", "刷新", "重写", "替换", "覆盖", "同步",
    "保存到", "保存", "写入", "输出到", "输出", "生成到",
)
_REFERENCE_PREFIXES = ("这个", "该", "上述", "前面的")
_NEGATED_ACTION_RE = re.compile(
    r"(?:不要|请勿|切勿|不可|不再|不必|无需|不需要|禁止|仅查看|只读)\s*"
    r"(?:修改|编辑|更新|刷新|重写|替换|覆盖|同步|保存|写入|输出|生成)\s*$"
)
_POSIX_ROOT_PREFIXES = (
    "/Users/", "/home/", "/workspace/", "/Volumes/", "/private/",
    "/tmp/", "/var/", "/mnt/", "/opt/",
)


def extract_local_resource_paths(message: str) -> list[str]:
    """Extract pasted local filenames, preserving spaces up to a known suffix."""

    return [item[0] for item in _path_spans(message)]


def extract_declared_artifact_targets(message: str) -> list[str]:
    """Return explicit write/update targets, excluding ordinary input paths."""

    targets: list[str] = []
    for path, start, end in _path_spans(message):
        before = message[max(0, start - 20) : start].strip(" `\t\r\n，,：:")
        after = message[end : min(len(message), end + 30)].strip(" `\t\r\n，,：:")
        direct_before = any(action in before for action in _DIRECT_ACTIONS)
        if _NEGATED_ACTION_RE.search(before):
            direct_before = False
        direct_after = any(after.startswith(action) for action in _DIRECT_ACTIONS)
        referential_after = any(
            after.startswith(f"{action}{reference}")
            for action in _DIRECT_ACTIONS
            for reference in _REFERENCE_PREFIXES
        )
        if direct_before or direct_after or referential_after:
            targets.append(path)
    return targets


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
    starts: list[int] = []
    for index, char in enumerate(message):
        if char == "/" or char == "~":
            suffix = message[index:]
            is_known_root = any(suffix.startswith(prefix) for prefix in _POSIX_ROOT_PREFIXES)
            if char == "/" and (
                (index > 0 and message[index - 1] == ":" and message[index : index + 2] == "//")
                or (index >= 2 and message[index - 2:index + 1] == "://")
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
        ):
            starts.append(index)

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


__all__ = [
    "artifact_path_matches",
    "extract_declared_artifact_targets",
    "extract_local_resource_paths",
]
