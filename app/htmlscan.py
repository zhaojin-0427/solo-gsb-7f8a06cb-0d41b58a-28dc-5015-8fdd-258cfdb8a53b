"""HTML 字节级扫描器。

用一个带位置信息的小型状态机把 HTML 拆成标签与属性，记录每个待处理
引用（a[href]、img[src]、link[rel=stylesheet][href]、base[href]）在【解码后
文本】中的精确区间；同时检出需求中禁止的构造：

* <script>（以及 on* 事件属性）——脚本
* <form>——表单
* style 属性——内联样式
* srcset 属性
* 其他一切承载 URL 的属性（action/poster/iframe src/...）
* <style> 表体交给 cssscan 处理（@import 在那里拦截）

属性区间记录后，lxml 只负责计数交叉校验：若两者看到的相关元素/属性数
不一致，说明该文件无法被唯一定位，按需求禁止导出。
"""
from __future__ import annotations

import html as html_entities
from dataclasses import dataclass, field

from .models import Notice, Problem, ProblemKind, Ref, RefRole

# 空白按 HTML 规范
WS = " \t\n\r\f"

# 原始文本元素（内容不按标记解析）
RAWTEXT_TAGS = {"script", "style", "title", "textarea", "xmp",
                "noembed", "noframes", "plaintext"}

# 除显式放行的 a[href]/img[src]/link[href]/base[href] 外，
# 一切承载 URL 的属性都按“无法处理的引用”拒绝。
URL_ATTRS_BLOCKED = {
    "action", "formaction", "cite", "longdesc", "poster", "data",
    "codebase", "manifest", "archive", "background", "profile",
    "usemap", "icon", "ping", "lowsrc", "dynsrc", "srcdoc",
    "xlink:href", "href", "src",
}


@dataclass
class Attr:
    name: str
    name_start: int
    name_end: int
    value: str | None        # 已做实体解码、去首尾空白；无值属性为 None
    val_start: int | None    # 值（引号内部）在文本中的起点
    val_end: int | None      # 终点（半开）
    quote: str | None
    raw_value: str | None    # 未做实体解码的原文值（去空白）


@dataclass
class Tag:
    name: str
    start: int
    end: int                 # '>' 之后
    attrs: list[Attr]
    closing: bool = False
    style_content: tuple[int, int] | None = None  # style/script 文本体区间


@dataclass
class HtmlScan:
    refs: list[Ref] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    style_blocks: list[tuple[int, int]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # 与 lxml 交叉校验用的元素/属性计数
    counts: dict[str, int] = field(default_factory=lambda: {
        "script": 0, "form": 0, "a_href": 0, "img_src": 0,
        "link_href": 0, "base_href": 0, "style_attr": 0,
        "srcset_attr": 0, "meta_refresh": 0, "on_attr": 0,
        "other_url_attr": 0})


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_tags(text: str) -> list[Tag]:
    """把 HTML 拆成标签（原始文本元素的内容跳过，注释/声明跳过）。"""
    tags: list[Tag] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch != "<":
            i += 1
            continue
        # 注释
        if text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            i = n if j == -1 else j + 3
            continue
        # CDATA（HTML 中仅外域内容出现，安全起见整体跳过）
        if text.startswith("<![CDATA[", i):
            j = text.find("]]>", i + 9)
            i = n if j == -1 else j + 3
            continue
        # 声明 / 处理指令
        if i + 1 < n and text[i + 1] in "!?":
            j = text.find(">", i + 2)
            i = n if j == -1 else j + 1
            continue
        # 标签名
        j = i + 1
        closing = False
        if j < n and text[j] == "/":
            closing = True
            j += 1
        name_start = j
        while j < n and text[j] not in WS + "/>":
            j += 1
        name = text[name_start:j].lower()
        if not name or not (name[0].isalpha()):
            i += 1
            continue
        attrs, j = _parse_attrs(text, j)
        # 自闭合斜杠
        self_closing = False
        while j < n and text[j] in WS:
            j += 1
        if j < n and text[j] == "/":
            self_closing = True
            j += 1
        if j < n and text[j] == ">":
            j += 1
        else:
            # 畸形：缺少 >，放弃这个标签
            i = max(i + 1, j)
            continue
        tag = Tag(name=name, start=i, end=j, attrs=attrs, closing=closing)
        tags.append(tag)
        i = j
        # 原始文本元素：跳到对应结束标签
        if not closing and not self_closing and name in RAWTEXT_TAGS:
            close = _find_close(text, i, name)
            if name == "style" and i < close:
                tag.style_content = (i, close)  # type: ignore[attr-defined]
            if name == "plaintext":
                break
            i = _skip_end_tag(text, close, name)
    return tags


def _parse_attrs(text: str, i: int) -> tuple[list[Attr], int]:
    n = len(text)
    attrs: list[Attr] = []
    while True:
        while i < n and text[i] in WS:
            i += 1
        if i >= n or text[i] in "/>":
            break
        name_start = i
        while i < n and text[i] not in WS + "/>=":
            i += 1
        name_end = i
        name = text[name_start:name_end].lower()
        while i < n and text[i] in WS:
            i += 1
        attr = Attr(name=name, name_start=name_start, name_end=name_end,
                    value=None, val_start=None, val_end=None,
                    quote=None, raw_value=None)
        if i < n and text[i] == "=":
            i += 1
            while i < n and text[i] in WS:
                i += 1
            quote = None
            if i < n and text[i] in "\"'":
                quote = text[i]
                i += 1
                val_start = i
                while i < n and text[i] != quote:
                    i += 1
                val_end = i  # 引号内部半开区间
                if i < n:
                    i += 1  # 跳过闭合引号
            else:
                val_start = i
                while i < n and text[i] not in WS + ">":
                    i += 1
                val_end = i
            raw = text[val_start:val_end].strip()
            attr.raw_value = raw
            attr.value = html_entities.unescape(raw).strip()
            attr.quote = quote
            attr.val_start = val_start
            attr.val_end = val_end
        attrs.append(attr)
    return attrs, i


def _find_close(text: str, pos: int, name: str) -> int:
    """找原始文本元素的 </name，返回 '<' 位置；找不到返回 len。"""
    needle = "</" + name
    n = len(text)
    while True:
        k = text.lower().find(needle, pos)
        if k == -1:
            return n
        after = k + len(needle)
        if after >= n or text[after] in WS + "/>":
            return k
        pos = after


def _skip_end_tag(text: str, pos: int, name: str) -> int:
    if pos >= len(text):
        return pos
    j = text.find(">", pos)
    return len(text) if j == -1 else j + 1


# ---------------------------------------------------------------------------
# 业务扫描：禁止构造 + 引用采集
# ---------------------------------------------------------------------------
def scan_html(text: str, source: str) -> HtmlScan:
    out = HtmlScan()
    prob = lambda kind, msg, pos, url=None: out.problems.append(
        Problem(kind=kind, message=msg, source=source,
                line=_line_of(text, pos), url=url))

    base_seen = False
    for tag in scan_tags(text):
        if tag.closing:
            continue
        pos = tag.start
        # 原始文本元素：style 体记录给 CSS 扫描；script 直接禁止
        if tag.name == "script":
            out.counts["script"] += 1
            prob(ProblemKind.FORBIDDEN_SCRIPT,
                 "包含 <script> 脚本元素，按策略禁止封包", pos)
            continue
        style_range = getattr(tag, "style_content", None)
        if tag.name == "style":
            type_attr = _attr_by_name(tag.attrs, "type")
            type_val = (type_attr.value or "").lower().strip() \
                if type_attr is not None else ""
            # 浏览器仅把 type 为空或 text/css 的 style 体当样式解析
            if style_range and (not type_val or type_val == "text/css"):
                out.style_blocks.append(style_range)
            continue
        if tag.name in {"title", "textarea", "xmp", "noembed",
                        "noframes", "plaintext"}:
            continue
        if tag.name == "form":
            out.counts["form"] += 1
            prob(ProblemKind.FORBIDDEN_FORM,
                 "包含 <form> 表单元素，按策略禁止封包", pos)

        attr_names = [a.name for a in tag.attrs]
        seen: set[str] = set()
        for a in tag.attrs:
            if a.name in seen and a.name in (
                    "href", "src", "srcset", "style"):
                prob(ProblemKind.LOCATE_AMBIGUOUS,
                     f"属性 {a.name} 在同一标签中重复出现，无法唯一定位",
                     a.name_start)
            seen.add(a.name)

            # on* 事件属性
            if a.name.startswith("on") and len(a.name) > 2 and a.name[2:].isalpha():
                out.counts["on_attr"] += 1
                prob(ProblemKind.FORBIDDEN_SCRIPT,
                     f"包含事件处理属性 {a.name}（内联脚本），按策略禁止封包",
                     a.name_start)
                continue
            if a.name == "style":
                out.counts["style_attr"] += 1
                prob(ProblemKind.FORBIDDEN_INLINE_STYLE,
                     "包含 style 内联样式属性，按策略禁止封包",
                     a.name_start)
                continue
            if a.name == "srcset":
                out.counts["srcset_attr"] += 1
                prob(ProblemKind.FORBIDDEN_SRCSET,
                     "包含 srcset 属性，按策略禁止封包",
                     a.name_start)
                continue
            if a.name == "http-equiv" and tag.name == "meta" and \
                    (a.value or "").lower() == "refresh":
                out.counts["meta_refresh"] += 1
                prob(ProblemKind.FORBIDDEN_URL_ATTR,
                     "包含 meta http-equiv=refresh 跳转，按策略禁止封包",
                     a.name_start)
                continue

        # 锚点收集：任意元素的 id，以及 a[name]
        for a in tag.attrs:
            if a.name == "id" and a.value:
                out.anchors.add(a.value)
            if a.name == "name" and tag.name == "a" and a.value:
                out.anchors.add(a.value)

        href = _attr_by_name(tag.attrs, "href")
        src = _attr_by_name(tag.attrs, "src")

        # base
        if tag.name == "base":
            if href is not None and href.value is not None:
                out.counts["base_href"] += 1
                if base_seen:
                    prob(ProblemKind.LOCATE_AMBIGUOUS,
                         "存在多个 <base href>，基准无法唯一确定",
                         href.name_start)
                base_seen = True
                if href.val_start is not None and href.val_end is not None:
                    out.refs.append(Ref(
                        role=RefRole.BASE, source=source,
                        raw_url=href.value, line=_line_of(text, href.val_start),
                        start=href.val_start, end=href.val_end))
            continue

        if tag.name == "a":
            if href is not None and href.val_start is not None:
                out.counts["a_href"] += 1
                out.refs.append(Ref(
                    role=RefRole.A, source=source, raw_url=href.value or "",
                    line=_line_of(text, href.val_start),
                    start=href.val_start, end=href.val_end))
        elif tag.name == "img":
            if src is not None and src.val_start is not None:
                out.counts["img_src"] += 1
                out.refs.append(Ref(
                    role=RefRole.IMG, source=source, raw_url=src.value or "",
                    line=_line_of(text, src.val_start),
                    start=src.val_start, end=src.val_end))
        elif tag.name == "link":
            rel = _attr_by_name(tag.attrs, "rel")
            rel_tokens = set((rel.value or "").lower().split()) if rel is not None else set()
            if href is not None and href.val_start is not None:
                out.counts["link_href"] += 1
                if "stylesheet" in rel_tokens:
                    out.refs.append(Ref(
                        role=RefRole.STYLESHEET, source=source,
                        raw_url=href.value or "",
                        line=_line_of(text, href.val_start),
                        start=href.val_start, end=href.val_end))
                else:
                    prob(ProblemKind.FORBIDDEN_URL_ATTR,
                         f"非样式表 <link>（rel={(rel.value if rel else None)!r}）"
                         "的 href 不在允许处理范围内",
                         href.val_start, url=href.value)
        else:
            # 其它标签上的 href/src 及一切其余 URL 属性
            for a in tag.attrs:
                if a.name in ("href", "src") or a.name in URL_ATTRS_BLOCKED:
                    out.counts["other_url_attr"] += 1
                    prob(ProblemKind.FORBIDDEN_URL_ATTR,
                         f"<{tag.name}> 的 {a.name} 属性承载 URL，"
                         "不在允许处理的引用范围内",
                         a.name_start, url=a.value)

    return out


def _attr_by_name(attrs: list[Attr], name: str) -> Attr | None:
    for a in attrs:
        if a.name == name:
            return a
    return None
