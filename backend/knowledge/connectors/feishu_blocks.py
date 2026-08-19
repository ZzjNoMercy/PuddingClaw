"""Pure Feishu Docx block-tree to Markdown normalization.

Remote content is data, never instructions. This converter performs no code
execution and emits downloadable image/file descriptors separately so the
sync worker can fetch them through authenticated, size-limited endpoints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote


@dataclass
class FeishuMarkdownResult:
    markdown: str
    assets: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_TEXT_FIELDS = {
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    9: "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
}


def _escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _elements(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []


def _text_elements_to_markdown(elements: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run") if isinstance(element.get("text_run"), dict) else None
        if text_run is not None:
            content = _escape_text(str(text_run.get("content") or ""))
            style = text_run.get("text_element_style") if isinstance(text_run.get("text_element_style"), dict) else {}
            link = style.get("link") if isinstance(style.get("link"), dict) else {}
            if style.get("inline_code"):
                content = f"`{content.replace('`', 'ˋ')}`"
            if style.get("bold"):
                content = f"**{content}**"
            if style.get("italic"):
                content = f"*{content}*"
            if style.get("strikethrough"):
                content = f"~~{content}~~"
            if link.get("url"):
                content = f"[{content}]({str(link['url'])})"
            parts.append(content)
            continue
        mention_doc = element.get("mention_doc") if isinstance(element.get("mention_doc"), dict) else None
        if mention_doc is not None:
            title = str(mention_doc.get("title") or mention_doc.get("obj_type") or "飞书文档")
            url = str(mention_doc.get("url") or "")
            parts.append(f"[{_escape_text(title)}]({url})" if url else _escape_text(title))
            continue
        mention_user = element.get("mention_user") if isinstance(element.get("mention_user"), dict) else None
        if mention_user is not None:
            parts.append(f"@{_escape_text(str(mention_user.get('name') or '用户'))}")
            continue
        equation = element.get("equation") if isinstance(element.get("equation"), dict) else None
        if equation is not None:
            parts.append(f"${str(equation.get('content') or '')}$")
            continue
        reminder = element.get("reminder") if isinstance(element.get("reminder"), dict) else None
        if reminder is not None:
            parts.append(_escape_text(str(reminder.get("text") or "[提醒]")))
    return "".join(parts).strip()


def _block_text(block: dict[str, Any], field_name: str) -> str:
    payload = block.get(field_name) if isinstance(block.get(field_name), dict) else {}
    return _text_elements_to_markdown(_elements(payload.get("elements")))


class FeishuBlockConverter:
    def __init__(self, blocks: list[dict[str, Any]]) -> None:
        self.blocks = [block for block in blocks if isinstance(block, dict) and block.get("block_id")]
        self.by_id = {str(block["block_id"]): block for block in self.blocks}
        self.assets: list[dict[str, str]] = []
        self.warnings: list[str] = []

    def convert(self) -> FeishuMarkdownResult:
        child_ids = {str(child) for block in self.blocks for child in (block.get("children") or [])}
        roots = [block for block in self.blocks if str(block["block_id"]) not in child_ids]
        if not roots and self.blocks:
            roots = [self.blocks[0]]
        rendered: list[str] = []
        visited: set[str] = set()
        for root in roots:
            rendered.extend(self._render(root, depth=0, visited=visited))
        markdown = "\n\n".join(part.strip() for part in rendered if part.strip())
        markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
        return FeishuMarkdownResult(
            markdown=f"{markdown}\n" if markdown else "",
            assets=self.assets,
            warnings=self.warnings,
        )

    def _children(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        return [self.by_id[str(child)] for child in (block.get("children") or []) if str(child) in self.by_id]

    def _render(self, block: dict[str, Any], *, depth: int, visited: set[str]) -> list[str]:
        block_id = str(block.get("block_id") or "")
        if block_id in visited:
            self.warnings.append(f"检测到循环块引用：{block_id}")
            return []
        visited.add(block_id)
        block_type = int(block.get("block_type") or 0)
        children = self._children(block)
        current: list[str] = []
        if block_type == 1:  # page root
            pass
        elif block_type in _TEXT_FIELDS:
            field_name = _TEXT_FIELDS[block_type]
            text = _block_text(block, field_name)
            if block_type in range(3, 12):
                current.append(f"{'#' * min(6, block_type - 2)} {text}")
            elif block_type == 12:
                current.append(f"{'  ' * depth}- {text}")
            elif block_type == 13:
                current.append(f"{'  ' * depth}1. {text}")
            elif block_type == 14:
                language = str((block.get("code") or {}).get("style", {}).get("language") or "")
                current.append(f"```{language}\n{text}\n```")
            elif block_type == 15:
                current.append("\n".join(f"> {line}" for line in (text or " ").splitlines()))
            elif block_type == 17:
                done = bool((block.get("todo") or {}).get("style", {}).get("done"))
                current.append(f"{'  ' * depth}- [{'x' if done else ' '}] {text}")
            else:
                current.append(text)
        elif block_type == 22:
            current.append("---")
        elif block_type == 27:
            image = block.get("image") if isinstance(block.get("image"), dict) else {}
            token = str(image.get("token") or "")
            if token:
                filename = f"feishu-image-{block_id}.bin"
                self.assets.append({"type": "image", "token": token, "block_id": block_id, "filename": filename})
                current.append(f"![飞书图片](./assets/{quote(filename)})")
        elif block_type == 23:
            file_payload = block.get("file") if isinstance(block.get("file"), dict) else {}
            token = str(file_payload.get("token") or "")
            name = str(file_payload.get("name") or f"feishu-file-{block_id}")
            if token:
                self.assets.append({"type": "file", "token": token, "block_id": block_id, "filename": name})
                current.append(f"[{_escape_text(name)}](./assets/{quote(name)})")
        elif block_type == 31:
            current.extend(self._render_table(block, visited=visited))
            children = []  # consumed by table renderer
        elif block_type == 19:
            inner: list[str] = []
            for child in children:
                inner.extend(self._render(child, depth=depth + 1, visited=visited))
            current.append("\n".join(f"> {line}" for line in "\n\n".join(inner).splitlines()))
            children = []
        else:
            self.warnings.append(f"暂未原生支持飞书块类型 {block_type}（{block_id}），已保留其子块。")

        for child in children:
            current.extend(self._render(child, depth=depth + 1, visited=visited))
        return current

    def _render_table(self, block: dict[str, Any], *, visited: set[str]) -> list[str]:
        table = block.get("table") if isinstance(block.get("table"), dict) else {}
        prop = table.get("property") if isinstance(table.get("property"), dict) else {}
        rows = int(prop.get("row_size") or 0)
        columns = int(prop.get("column_size") or 0)
        cells = self._children(block)
        if rows <= 0 or columns <= 0 or len(cells) < columns:
            return []
        values: list[str] = []
        for cell in cells[: rows * columns]:
            rendered: list[str] = []
            for child in self._children(cell):
                rendered.extend(self._render(child, depth=0, visited=visited))
            values.append("<br>".join(part.replace("|", "\\|") for part in rendered if part).strip())
        matrix = [values[index : index + columns] for index in range(0, len(values), columns)]
        if not matrix:
            return []
        header = matrix[0]
        lines = [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * columns)} |"]
        lines.extend(f"| {' | '.join(row + [''] * (columns - len(row)))} |" for row in matrix[1:])
        return ["\n".join(lines)]


def convert_feishu_blocks_to_markdown(blocks: list[dict[str, Any]]) -> FeishuMarkdownResult:
    return FeishuBlockConverter(blocks).convert()
