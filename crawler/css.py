# -*- coding: utf-8 -*-
"""CSS 选择器 -> XPath 转换（覆盖常见写法，避免为此引入额外依赖）。

支持：
    div / *                标签
    .class                 class 包含
    #id                    id 等于
    [attr] [attr=v]       属性存在 / 等于
    [attr^=v] [$=v] [*=v] 属性前缀 / 后缀 / 包含
    a b                   后代
    a > b                 子元素
    a, b                  并集（分别转换后用 | 连接）
    :first-child
    :nth-child(n)
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(
    r"""
    (?P<tag>\*|[a-zA-Z][\w-]*)?
    (?P<simple>(?:\#[\w-]+|\.[\w-]+|\[[^\]]*\]|:(?:first-child|last-child|nth-child\(\s*\d+\s*\)))*)
    """,
    re.VERBOSE,
)

_SIMPLE_RE = re.compile(r"(\#[\w-]+|\.[\w-]+|\[[^\]]*\]|:[a-z-]+(?:\([^)]*\))?)")


class CssError(ValueError):
    """无法转换的 CSS 选择器。"""


def _attr_to_xpath(expr: str) -> str:
    inner = expr[1:-1].strip()
    if not inner:
        raise CssError("空的属性选择器 []")
    for op in ("^=", "$=", "*=", "="):
        if op in inner:
            name, value = inner.split(op, 1)
            name = name.strip()
            value = value.strip().strip("'\"")
            if not name:
                raise CssError(f"属性选择器缺少属性名: {expr}")
            if op == "=":
                return f"[@{name}={_quote(value)}]"
            if op == "^=":
                return f"[starts-with(@{name}, {_quote(value)})]"
            if op == "$=":
                return f"[substring(@{name}, string-length(@{name}) - string-length({_quote(value)}) + 1) = {_quote(value)}]"
            return f"[contains(@{name}, {_quote(value)})]"
    return f"[@{inner}]"


def _quote(value: str) -> str:
    if "'" in value:
        return f'"{value}"'
    return f"'{value}'"


def _simple_to_xpath(token: str) -> str:
    if token.startswith("."):
        return f"[contains(concat(' ', normalize-space(@class), ' '), ' {token[1:]} ')]"
    if token.startswith("#"):
        return f"[@id={_quote(token[1:])}]"
    if token.startswith("["):
        return _attr_to_xpath(token)
    if token == ":first-child":
        return "[1]"
    if token == ":last-child":
        return "[last()]"
    match = re.fullmatch(r":nth-child\(\s*(\d+)\s*\)", token)
    if match:
        return f"[{match.group(1)}]"
    raise CssError(f"暂不支持的 CSS 伪类: {token}")


def _convert_single(selector: str) -> str:
    # 先按 > 与空白切分（保留组合符）
    parts = re.split(r"\s*(>)\s*|\s+", selector.strip())
    parts = [part for part in parts if part]
    if not parts:
        raise CssError("空选择器")

    xpath_parts: list[str] = []
    combinator = " "
    for part in parts:
        if part == ">":
            combinator = ">"
            continue
        match = _TOKEN_RE.fullmatch(part)
        if not match:
            raise CssError(f"无法解析的 CSS 片段: {part}")
        tag = match.group("tag") or "*"
        simple = match.group("simple") or ""
        predicates = "".join(_simple_to_xpath(token) for token in _SIMPLE_RE.findall(simple))
        axis = "//" if combinator == " " else "/"
        if not xpath_parts:
            axis = "//"
        xpath_parts.append(f"{axis}{tag}{predicates}")
        combinator = " "

    return "".join(xpath_parts) or "//*"


def _split_groups(selector: str) -> list[str]:
    groups: list[str] = []
    depth = 0
    current: list[str] = []
    for char in selector:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            groups.append("".join(current))
            current = []
            continue
        current.append(char)
    groups.append("".join(current))
    return [group.strip() for group in groups if group.strip()]


def css_to_xpath(selector: str) -> str:
    """把 CSS 选择器转成等价的 XPath（列表页/详情页容器等相对文档的选择器）。"""
    selector = (selector or "").strip()
    if not selector:
        raise CssError("空选择器")
    if selector.startswith(("//", ".//", "/")):
        return selector  # 已经是 XPath
    groups = [_convert_single(group) for group in _split_groups(selector)]
    return " | ".join(groups)


def css_to_relative_xpath(selector: str) -> str:
    """转成相对单个条目的 XPath（以 .// 开头），用于 title/url 等字段。"""
    xpath = css_to_xpath(selector)
    if xpath.startswith("//"):
        return "." + xpath
    return xpath
