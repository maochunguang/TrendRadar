# coding=utf-8
"""
frequency_words.txt 结构化编辑支持

在结构化模型与原文本之间双向转换，供 Web 设置中心的词组编辑器使用。

结构化模型：
{
    "header": "原文件头部注释（原样保留）",
    "global_filters": [词条],          # [GLOBAL_FILTER] 区
    "filters": [词条],                 # 组内 ! 过滤词（主程序实现为全局生效）
    "groups": [
        {
            "alias": "组别名（可选，空串表示无）",
            "required": [词条],        # + 必须词
            "normal": [词条],          # 普通词
            "max_count": 0,            # @N 上限（0=不限制）
        }
    ],
}

词条格式（kind: word/regex）：
    {"kind": "word", "text": "华为", "display": "华为科技"}
    {"kind": "regex", "text": r"\\bAI\\b", "flags": "i", "display": "AI"}

序列化布局：
    [header]
    [GLOBAL_FILTER]
    ...全局过滤词

    [WORD_GROUPS]
    !过滤词池（单独一组，仅 ! 行，不产生显示组）

    [组别名]        （可选）
    +必须词
    普通词
    @N
"""

import re
from typing import Dict, List

# 与主程序 _parse_word 相同的正则语法：/pattern/flags => display
_ENTRY_RE = re.compile(r"^/(.+)/([a-z]*)\s*(?:=>\s*(.+))?$")
_WORD_RE = re.compile(r"^(.+?)\s*=>\s*(.+)$")


def _parse_entry(raw: str) -> Dict:
    """解析单行词条为结构化表示"""
    text = raw.strip()
    m = _ENTRY_RE.match(text)
    if m:
        return {
            "kind": "regex",
            "text": m.group(1),
            "flags": m.group(2) or "",
            "display": (m.group(3) or "").strip(),
        }
    m = _WORD_RE.match(text)
    if m:
        return {"kind": "word", "text": m.group(1).strip(), "display": m.group(2).strip()}
    return {"kind": "word", "text": text, "display": ""}


def _serialize_entry(entry: Dict) -> str:
    """词条 → 单行文本"""
    if entry.get("kind") == "regex":
        text = f"/{entry.get('text', '')}/{entry.get('flags', '')}"
    else:
        text = entry.get("text", "")
    display = entry.get("display", "")
    return f"{text} => {display}" if display else text


def parse_frequency(content: str) -> Dict:
    """
    解析 frequency_words 文本为结构化模型

    Raises:
        ValueError: 内容为空
    """
    if not content or not content.strip():
        raise ValueError("文件内容为空")

    lines = content.split("\n")

    # 头部注释 = 第一个区域标记行之前的所有行
    header_end = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header_end = idx
            break
    header = "\n".join(lines[:header_end]).rstrip("\n")

    # 区域切分：按空行分组（与主程序 load_frequency_words 一致）
    groups_text = [g.strip() for g in content.split("\n\n") if g.strip()]
    # 丢弃头部注释组（不含区域标记的注释组会因全是 # 行被主程序忽略，这里同步忽略）
    # 主程序按内容判断，这里按是否含非注释行判断

    global_filters: List[Dict] = []
    filters: List[Dict] = []
    groups: List[Dict] = []
    current_section = "WORD_GROUPS"

    for group_text in groups_text:
        group_lines = [
            line.strip() for line in group_text.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        if not group_lines:
            continue

        # 区域标记
        if group_lines[0].startswith("[") and group_lines[0].endswith("]"):
            section_name = group_lines[0][1:-1].upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                group_lines = group_lines[1:]

        if current_section == "GLOBAL_FILTER":
            for line in group_lines:
                if line.startswith(("!", "+", "@")):
                    continue  # 与主程序一致：全局过滤区不支持特殊语法
                global_filters.append(_parse_entry(line))
            continue

        # 词组区
        group_alias = ""
        if group_lines and group_lines[0].startswith("[") and group_lines[0].endswith("]"):
            potential = group_lines[0][1:-1].strip()
            if potential.upper() not in ("GLOBAL_FILTER", "WORD_GROUPS"):
                group_alias = potential
                group_lines = group_lines[1:]

        group = {
            "alias": group_alias,
            "required": [],
            "normal": [],
            "max_count": 0,
        }
        for line in group_lines:
            if line.startswith("@"):
                try:
                    count = int(line[1:])
                    if count > 0:
                        group["max_count"] = count
                except (ValueError, IndexError):
                    pass
            elif line.startswith("!"):
                filters.append(_parse_entry(line[1:]))
            elif line.startswith("+"):
                group["required"].append(_parse_entry(line[1:]))
            else:
                group["normal"].append(_parse_entry(line))

        if group["required"] or group["normal"]:
            groups.append(group)

    return {
        "header": header,
        "global_filters": global_filters,
        "filters": filters,
        "groups": groups,
    }


def validate_frequency(model: Dict) -> List[str]:
    """校验结构化模型，返回错误列表（正则可编译性等）"""
    errors: List[str] = []

    def check_entries(entries: List[Dict], where: str) -> None:
        for i, entry in enumerate(entries):
            if entry.get("kind") == "regex":
                pattern = entry.get("text", "")
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as e:
                    errors.append(f"{where}[{i + 1}] 正则无效 /{pattern}/: {e}")
            if not str(entry.get("text", "")).strip():
                errors.append(f"{where}[{i + 1}] 内容为空")

    check_entries(model.get("global_filters", []), "全局过滤词")
    check_entries(model.get("filters", []), "过滤词")

    for gi, group in enumerate(model.get("groups", [])):
        where = f"词组 {gi + 1}"
        if not group.get("required") and not group.get("normal"):
            errors.append(f"{where}（{group.get('alias') or '未命名'}）没有任何关键词")
        check_entries(group.get("required", []), f"{where} 必须词")
        check_entries(group.get("normal", []), f"{where} 普通词")
        try:
            max_count = int(group.get("max_count", 0) or 0)
            if max_count < 0:
                errors.append(f"{where} 条数上限不能为负数")
        except (TypeError, ValueError):
            errors.append(f"{where} 条数上限格式错误")

    return errors


def serialize_frequency(model: Dict) -> str:
    """结构化模型 → frequency_words 文本"""
    parts: List[str] = []

    header = (model.get("header") or "").rstrip()
    if header:
        parts.append(header)

    global_filters = [e for e in model.get("global_filters", []) if e.get("text")]
    if global_filters:
        parts.append("[GLOBAL_FILTER]")
        parts.extend(_serialize_entry(e) for e in global_filters)

    filters = [e for e in model.get("filters", []) if e.get("text")]
    groups = [g for g in model.get("groups", []) if g.get("required") or g.get("normal")]

    if filters or groups:
        word_group_parts: List[str] = []
        if filters:
            # 过滤词池：仅 ! 行的独立组（主程序解析时不产生显示组）
            word_group_parts.append("\n".join(f"!{_serialize_entry(e)}" for e in filters))
        for group in groups:
            lines: List[str] = []
            if group.get("alias"):
                lines.append(f"[{group['alias']}]")
            lines.extend(f"+{_serialize_entry(e)}" for e in group.get("required", []))
            lines.extend(_serialize_entry(e) for e in group.get("normal", []))
            if group.get("max_count"):
                lines.append(f"@{int(group['max_count'])}")
            word_group_parts.append("\n".join(lines))

        parts.append("[WORD_GROUPS]")
        parts.append("\n\n".join(word_group_parts))

    # 统一换行与末尾空行（主程序按 \n\n 分组，双换行保证组间分隔）
    text = "\n\n".join(p for p in parts if p)
    return text.rstrip() + "\n"
