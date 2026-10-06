"""CSS 扫描器（tinycss2 + 括号区间回扫）。

只处理 url() 引用：
  * url(foo.png)   —— 未加引号的 URLToken
  * url("foo.png") —— 加引号的 FunctionBlock(url) 内含 StringToken
字符串 / 注释中的 "url(...)" 不会被识别（这正是期望行为）。
任何 @import 规则都按需求禁止。

tinycss2 的 source_line/source_column（均 1 基）指向每个词元起点；
但 URLToken.representation 会归一化空白，不能直接用长度切原文，因此
对每个 url 以起点回扫配对括号，并用 token 值交叉验证；无法对齐则报告
LOCATE_AMBIGUOUS，禁止导出。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import tinycss2

from .models import Problem, ProblemKind, Ref, RefRole

_WS = " \t\n\r\f"


@dataclass
class CssScan:
    refs: list[Ref] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def scan_css(css: str, source: str, *, line_offset: int = 0,
             coord_offset: int = 0) -> CssScan:
    """扫描 CSS 文本。

    line_offset / coord_offset：该 CSS 来自 <style> 块时，相对整份
    HTML 文本的行偏移与字符偏移；独立 .css 文件传 0。
    """
    out = CssScan()
    line_starts = [0]
    for i, ch in enumerate(css):
        if ch == "\n":
            line_starts.append(i + 1)

    def to_pos(line: int, column: int) -> int:
        return line_starts[line - 1] + (column - 1)

    def add_problem(kind: ProblemKind, message: str, css_pos: int,
                    url: str | None = None) -> None:
        out.problems.append(Problem(
            kind=kind, message=message, source=source,
            line=css.count("\n", 0, css_pos) + 1 + line_offset, url=url))

    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True,
                                      skip_comments=True)

    def check_imports(nodes) -> None:
        for node in nodes:
            if node.type == "at-rule" and node.lower_at_keyword == "import":
                pos = to_pos(node.source_line, node.source_column)
                add_problem(ProblemKind.FORBIDDEN_CSS_IMPORT,
                            "CSS 包含 @import 规则，按策略禁止封包", pos)

    def walk(nodes) -> None:
        for node in nodes:
            if node.type == "at-rule":
                check_imports([node])
                if node.prelude:
                    walk(node.prelude)
                if node.content:
                    walk(node.content)
            elif node.type == "at-keyword" and \
                    node.value.lower() in ("import", "@import"):
                # 非法位置（如 @media 内）的 @import 被拆成裸 at-keyword
                pos = to_pos(node.source_line, node.source_column)
                add_problem(ProblemKind.FORBIDDEN_CSS_IMPORT,
                            "CSS 包含 @import 规则，按策略禁止封包", pos)
            elif node.type == "qualified-rule":
                if node.prelude:
                    walk(node.prelude)
                if node.content:
                    walk(node.content)
            elif node.type == "function" and node.lower_name == "url":
                _handle_quoted(node)
            elif node.type == "url":
                _handle_unquoted(node)
            # parse_stylesheet 层已展开规则；词元层的括号块
            elif node.type in ("parentheses-block", "curly-brackets-block",
                               "square-brackets-block"):
                if getattr(node, "content", None):
                    walk(node.content)

    def _handle_unquoted(node) -> None:
        start = to_pos(node.source_line, node.source_column)
        open_paren = css.find("(", start)
        if open_paren == -1 or open_paren - start > 3:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "无法在原文字节中唯一定位 url() 词元", start, node.value)
            return
        span = _scan_parens(css, open_paren)
        if span is None:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "无法在原文字节中唯一定位 url() 词元", start, node.value)
            return
        s, e = span
        token_text = css[start:e]
        value = _extract_unquoted(token_text)
        if value is None or value != node.value:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "url() 原文与解析值不一致（含转义等），无法唯一定位",
                        start, node.value)
            return
        out.refs.append(Ref(role=RefRole.CSS_URL, source=source,
                            raw_url=node.value,
                            line=css.count("\n", 0, start) + 1 + line_offset,
                            start=start + coord_offset, end=e + coord_offset,
                            style_offset=coord_offset))

    def _handle_quoted(node) -> None:
        # node 起点是 "url"；内部应恰好一个 StringToken
        strings = [a for a in node.arguments if a.type == "string"]
        if len(strings) != 1 or any(
                a.type not in ("string", "whitespace", "comment",
                               "literal") or
                (a.type == "literal" and a.value != ",")
                for a in node.arguments):
            pos = to_pos(node.source_line, node.source_column)
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "url() 参数结构无法唯一定位",
                        pos, tinycss2.serialize([node]))
            return
        st = strings[0]
        func_start = to_pos(node.source_line, node.source_column)
        span = _scan_parens(css, css.find("(", func_start))
        if span is None:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "无法在原文字节中唯一定位 url() 词元", func_start,
                        st.value)
            return
        s, e = span
        # 字符串词元起点（引号处）
        str_pos = to_pos(st.source_line, st.source_column)
        if str_pos < s or str_pos >= e or css[str_pos] not in "\"'":
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "url() 内字符串无法在原文字节中定位", func_start,
                        st.value)
            return
        q = css[str_pos]
        close = css.find(q, str_pos + 1)
        if close == -1 or close >= e:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "url() 内字符串引号不配对", func_start, st.value)
            return
        inner = css[str_pos + 1:close]
        # tinycss2 字符串转义极少出现于 URL；出现则拒绝以保字节安全
        if "\\" in inner or inner != st.value:
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        "url() 字符串含转义或与解析值不一致，无法唯一定位",
                        func_start, st.value)
            return
        out.refs.append(Ref(role=RefRole.CSS_URL, source=source,
                            raw_url=st.value,
                            line=css.count("\n", 0, s) + 1 + line_offset,
                            start=func_start + coord_offset,
                            end=e + coord_offset,
                            style_offset=coord_offset))

    walk(rules)

    # 解析级错误（如 bad-url）一律按定位歧义拒绝，宁可禁导
    for err in rules:
        if err.type == "error":
            pos = to_pos(err.source_line, err.source_column)
            add_problem(ProblemKind.LOCATE_AMBIGUOUS,
                        f"CSS 解析错误：{err.message}", pos)
    return out


def _scan_parens(css: str, open_paren: int) -> tuple[int, int] | None:
    """从 '(' 起回扫到配对 ')'（字符串与嵌套括号安全），返回 (lparen, rparen+1)。"""
    if open_paren < 0 or open_paren >= len(css) or css[open_paren] != "(":
        return None
    depth = 0
    i = open_paren
    n = len(css)
    quote = None
    while i < n:
        ch = css[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return open_paren, i + 1
        i += 1
    return None


def _extract_unquoted(token_text: str) -> str | None:
    """从 "url( ... )" 文本取出未加引号的值（去首尾空白），
    含引号 / 反斜杠 / 括号 / 换行则拒绝。"""
    assert token_text.endswith(")")
    inner = token_text[4:-1] if token_text[:3].lower() == "url" else token_text
    stripped = inner.strip()
    if not stripped:
        return ""
    if stripped[0] in "\"'" or "\\" in stripped or "\n" in stripped \
            or "(" in stripped or ")" in stripped:
        return None
    return stripped
