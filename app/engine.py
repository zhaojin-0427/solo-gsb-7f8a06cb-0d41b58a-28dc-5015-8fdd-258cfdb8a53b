"""封包核心引擎：扫描 → 引用解析与校验 → 改写 → 可达性 → 产物复检。"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from lxml import etree, html as lxml_html

from . import cssscan, htmlscan
from .models import (AnalyzeResult, FileEntry, HtmlMeta, Notice, NoticeKind,
                     Problem, ProblemKind, Ref, RefRole)

ALLOWED_ANCHOR_SCHEMES = {"http", "https", "mailto", "tel", "ftp"}
BLOCKED_SCHEMES = {"data", "javascript", "vbscript", "file", "blob",
                   "about", "ws", "wss", "gopher", "filesystem"}


# ===========================================================================
# 第一步：HTML/CSS 扫描
# ===========================================================================
def scan_files(result: AnalyzeResult) -> None:
    for path, entry in result.files.items():
        if entry.kind == "html":
            _scan_one_html(result, path, entry.data.decode("utf-8"))
        elif entry.kind == "css":
            scan = cssscan.scan_css(entry.data.decode("utf-8"), path)
            result.refs.extend(scan.refs)
            result.problems.extend(scan.problems)


def _scan_one_html(result: AnalyzeResult, path: str, text: str) -> None:
    refs, anchors, problems, css_refs = _scan_html_text(path, text)
    result.refs.extend(refs)
    result.refs.extend(css_refs)
    result.problems.extend(problems)
    result.html_meta[path] = HtmlMeta(
        anchors=anchors,
        style_blocks=_collect_style_blocks(path, text))


def _collect_style_blocks(path: str, text: str) -> list[tuple[int, int]]:
    return htmlscan.scan_html(text, path).style_blocks


def _scan_html_text(path: str, text: str):
    """扫描一份 HTML 文本，返回 (html_refs, anchors, problems, css_refs)，
    含 lxml 独立计数交叉校验。初次分析与产物复检共用此函数。"""
    scan = htmlscan.scan_html(text, path)
    problems = list(scan.problems)

    # lxml 独立计数交叉校验。必须用 document_fromstring：fromstring 在
    # 片段为单个顶层 <a>/<img> 时会把该元素当作根，.//a 漏数自身。
    try:
        doc = lxml_html.document_fromstring(text)
        lxml_counts = _lxml_counts(doc)
        for key in sorted(set(lxml_counts) | set(scan.counts)):
            if lxml_counts.get(key, 0) != scan.counts.get(key, 0):
                problems.append(Problem(
                    ProblemKind.LOCATE_AMBIGUOUS,
                    f"扫描器与 lxml 对 {key} 计数不一致 "
                    f"（{scan.counts.get(key, 0)} vs {lxml_counts.get(key, 0)}），"
                    "引用无法唯一定位",
                    source=path))
    except etree.Error as exc:  # 极少数无法解析
        problems.append(Problem(
            ProblemKind.LOCATE_AMBIGUOUS,
            f"lxml 无法解析该 HTML（{exc}），引用无法保证唯一定位",
            source=path))

    css_refs = _scan_style_blocks(path, text, scan.style_blocks, problems)
    return scan.refs, set(scan.anchors), problems, css_refs


def _scan_style_blocks(path: str, text: str,
                       blocks: list[tuple[int, int]],
                       problems: list[Problem]) -> list[Ref]:
    refs: list[Ref] = []
    for s, e in blocks:
        block = text[s:e]
        line_offset = text.count("\n", 0, s)
        css = cssscan.scan_css(block, path, line_offset=line_offset,
                               coord_offset=s)
        refs.extend(css.refs)
        problems.extend(css.problems)
    return refs


def _lxml_counts(doc) -> dict[str, int]:
    def count(xpath: str) -> int:
        return len(doc.xpath(xpath))

    counts = {
        "script": count(".//script"),
        "form": count(".//form"),
        "a_href": count(".//a[@href]"),
        "img_src": count(".//img[@src]"),
        "link_href": count(".//link[@href]"),
        "base_href": count(".//base[@href]"),
        "style_attr": count(".//*[@style]"),
        "srcset_attr": count(".//*[@srcset]"),
        "meta_refresh": 0,
        "on_attr": 0,
        "other_url_attr": 0,
    }
    for el in doc.xpath(".//meta[@http-equiv]"):
        if (el.get("http-equiv") or "").strip().lower() == "refresh":
            counts["meta_refresh"] += 1
    for el in doc.iter():
        # document_fromstring 的树可能含注释/处理指令等非元素节点
        if not isinstance(el.tag, str):
            continue
        tag = etree.QName(el).localname
        for an in el.attrib:
            name = etree.QName(an).localname.lower()
            if name.startswith("on") and len(name) > 2:
                counts["on_attr"] += 1
            elif name in {"href", "src"} and not (
                    (tag == "a" and name == "href") or
                    (tag == "img" and name == "src") or
                    (tag == "link" and name == "href") or
                    (tag == "base" and name == "href")):
                counts["other_url_attr"] += 1
            elif name in {"action", "formaction", "cite", "longdesc",
                          "poster", "data", "codebase", "manifest",
                          "archive", "background", "profile", "usemap",
                          "icon", "ping", "lowsrc", "dynsrc", "srcdoc",
                          "xlink:href"}:
                counts["other_url_attr"] += 1
    return counts


# ===========================================================================
# 第二步：引用解析与校验
# ===========================================================================
@dataclass
class Parsed:
    external: bool = False
    external_keep: bool = False   # 外部普通超链接（a[href]）
    blocked: bool = False
    query: str = ""
    fragment: str = ""            # 原始片段（含 '#'，逐字）
    hier: str = ""                # 本地层级路径（仍百分号编码）


def parse_url(raw: str) -> Parsed:
    out = Parsed()
    if any(ord(c) < 0x20 for c in raw):
        out.blocked = True
        return out
    parts = urlsplit(raw)
    out.fragment = ("#" + parts.fragment) if parts.fragment else (
        "#" if raw.endswith("#") else "")
    out.query = parts.query
    scheme = parts.scheme.lower()
    # 协议相对 //host/...
    proto_rel = raw.startswith("//") or (not scheme and parts.netloc)
    if scheme or proto_rel:
        out.external = True
        out.external_keep = (scheme in ALLOWED_ANCHOR_SCHEMES
                             or proto_rel)
        if scheme in BLOCKED_SCHEMES or (
                scheme and not out.external_keep):
            out.blocked = True
        return out
    out.hier = parts.path
    return out


def resolve_local(hier: str, base_dir: str) -> tuple[str | None, str]:
    """按本地基准解析层级路径，返回 (归一化 ZIP 键 或 None=越界, 解码路径)。

    规则：
    * 根相对路径（以 '/' 起始）以 ZIP 根为基准；相对路径以 base_dir 为基准。
    * 只按字面 '/' 切分；每一段百分号解码【一次】，因此 %2F 不会变成
      分隔符，%2e%2e 仍能被识别为越界。
    * 任一段为 '..' 且栈已空 => 越界（不做浏览器式的根钳制）。
    """
    root_relative = hier.startswith("/")
    stack: list[str] = [] if root_relative else (
        base_dir.split("/") if base_dir else [])
    decoded_parts: list[str] = []
    for raw_seg in hier.split("/"):
        seg = unquote(raw_seg)  # 每段解码一次
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if not stack:
                return None, unquote(hier)
            stack.pop()
            decoded_parts.append("..")
            continue
        if "\\" in seg or "\x00" in seg or any(
                ord(c) < 0x20 for c in seg):
            return None, unquote(hier)
        stack.append(seg)
        decoded_parts.append(seg)
    norm = "/".join(stack)
    return norm, "/".join(decoded_parts)


def validate_refs(result: AnalyzeResult) -> None:
    # 先定每个 HTML 的有效 base 目录
    base_dir: dict[str, str] = {}
    for path, entry in result.files.items():
        if entry.kind == "html":
            base_dir[path] = posixpath.dirname(path)
    for ref in result.refs:
        if ref.role is RefRole.BASE:
            page = ref.source
            parsed = parse_url(ref.raw_url)
            # fragment/query 对 base 无意义
            if parsed.blocked or parsed.external:
                result.problems.append(Problem(
                    ProblemKind.UNSUPPORTED_SCHEME if parsed.blocked
                    else ProblemKind.EXTERNAL_RESOURCE,
                    "<base href> 必须指向站内页面",
                    source=page, line=ref.line, url=ref.raw_url))
                continue
            if parsed.query:
                result.problems.append(Problem(
                    ProblemKind.LOCAL_WITH_QUERY,
                    "带查询串的本地 <base href> 不允许",
                    source=page, line=ref.line, url=ref.raw_url))
                continue
            # base 的层级路径以【页面自身目录】为起点解析（base 自身
            # 不会被另一个 base 影响）。解析出的“目标路径”语义：
            #   以 '/' 结尾  -> 目录基准，直接采用
            #   否则         -> 文件基准，取其所在目录
            # 无论哪种，越界即拒绝。
            page_dir = posixpath.dirname(page)
            hier = parsed.hier
            target, decoded = resolve_local(hier, page_dir)
            if target is None:
                result.problems.append(Problem(
                    ProblemKind.PATH_TRAVERSAL,
                    "<base href> 越出站点根", source=page,
                    line=ref.line, url=ref.raw_url))
                continue

            if hier.endswith("/"):
                # 目录式（含 "./"、"d/"、"/"）。目标目录下应有文件，
                # 否则给出缺失提示（但不强制必须存在同名条目）。
                new_base = target
                if not any(
                        (f.startswith(new_base + "/") if new_base else True)
                        for f in result.files if f != page):
                    result.problems.append(Problem(
                        ProblemKind.MISSING_FILE,
                        f"<base href> 目录 {decoded!r} 下没有其它文件",
                        source=page, line=ref.line, url=ref.raw_url))
                    continue
                base_dir[page] = new_base
                continue

            if hier in (".", "./", ""):
                # 显式当前目录
                base_dir[page] = page_dir
                continue

            if target not in result.files:
                result.problems.append(Problem(
                    ProblemKind.MISSING_FILE,
                    "<base href> 指向的文件不存在", source=page,
                    line=ref.line, url=ref.raw_url))
                continue
            if result.files[target].kind != "html":
                result.problems.append(Problem(
                    ProblemKind.BAD_TARGET_TYPE,
                    f"<base href> 不能指向 {result.files[target].kind} 文件",
                    source=page, line=ref.line, url=ref.raw_url))
                continue
            # 文件式 base：基准是目标 HTML 所在目录
            base_dir[page] = posixpath.dirname(target)

    # 再解析其余引用
    for ref in result.refs:
        if ref.role is RefRole.BASE:
            continue
        parsed = parse_url(ref.raw_url)
        ref.external = parsed.external and not parsed.blocked
        if parsed.blocked:
            result.problems.append(Problem(
                ProblemKind.UNSUPPORTED_SCHEME,
                f"不允许的 URL 协议：{urlsplit(ref.raw_url).scheme or '(url)'!r}",
                source=ref.source, line=ref.line, url=ref.raw_url))
            continue
        if parsed.external:
            if ref.role is RefRole.A:
                result.notices.append(Notice(
                    NoticeKind.EXTERNAL_LINK,
                    "外部普通超链接，离线包中保留原样",
                    source=ref.source, line=ref.line, url=ref.raw_url))
                continue
            result.problems.append(Problem(
                ProblemKind.EXTERNAL_RESOURCE,
                "外部资源（非普通超链接），离线环境无法访问",
                source=ref.source, line=ref.line, url=ref.raw_url))
            continue
        if parsed.query:
            result.problems.append(Problem(
                ProblemKind.LOCAL_WITH_QUERY,
                "带查询串的本地 URL 不允许",
                source=ref.source, line=ref.line, url=ref.raw_url))
            continue
        # 基准目录：HTML 引用看有效 base；独立 CSS 看 CSS 文件目录；
        # 内嵌 <style> 的 url() 仍看所在 HTML 的有效 base。
        # 纯片段引用（hier 为空，如 "#frag"）始终指向当前文档本身——
        # 这与导出后 <base href="."> + "#frag" 的离线语义保持一致，
        # 不能消费基准目录栈（否则子目录页面上的锚点会被误判为目录）。
        frag_decoded = unquote(parsed.fragment[1:]) if parsed.fragment else ""
        if parsed.hier == "":
            if ref.role is RefRole.CSS_URL:
                result.problems.append(Problem(
                    ProblemKind.BAD_TARGET_TYPE,
                    "CSS url() 不能指向页面片段",
                    source=ref.source, line=ref.line, url=ref.raw_url))
                continue
            target = ref.source
            decoded = ""
        else:
            bdir = base_dir.get(ref.source, posixpath.dirname(ref.source))
            if ref.role is RefRole.CSS_URL and \
                    result.files[ref.source].kind == "css":
                bdir = posixpath.dirname(ref.source)
            target, decoded = resolve_local(parsed.hier, bdir)
            if target is None:
                result.problems.append(Problem(
                    ProblemKind.PATH_TRAVERSAL,
                    "引用路径越出站点根",
                    source=ref.source, line=ref.line, url=ref.raw_url))
                continue
        if target not in result.files:
            result.problems.append(Problem(
                ProblemKind.MISSING_FILE,
                f"本地引用的文件 {decoded!r} 不存在",
                source=ref.source, line=ref.line, url=ref.raw_url))
            continue
        if not _type_ok(result.files[target], ref.role):
            result.problems.append(Problem(
                ProblemKind.BAD_TARGET_TYPE,
                f"{ref.role.value} 不能指向 {result.files[target].kind} 文件",
                source=ref.source, line=ref.line, url=ref.raw_url))
            continue
        if parsed.fragment:
            if result.files[target].kind != "html":
                result.problems.append(Problem(
                    ProblemKind.BAD_ANCHOR,
                    "非 HTML 资源上的片段标识无法定位",
                    source=ref.source, line=ref.line, url=ref.raw_url))
                continue
            if frag_decoded != "" and frag_decoded not in \
                    result.html_meta[target].anchors:
                result.problems.append(Problem(
                    ProblemKind.BAD_ANCHOR,
                    f"目标页面不存在锚点 {('#'+frag_decoded)!r}",
                    source=ref.source, line=ref.line, url=ref.raw_url))
                continue
        ref.resolved_path = target
        ref.fragment = parsed.fragment


def _type_ok(entry: FileEntry, role: RefRole) -> bool:
    if role is RefRole.A:
        return entry.kind == "html"
    if role is RefRole.IMG or role is RefRole.CSS_URL:
        return entry.kind in ("png", "jpeg")
    if role is RefRole.STYLESHEET:
        return entry.kind == "css"
    return True


# ===========================================================================
# 第三步：可达性
# ===========================================================================
def compute_reachability(result: AnalyzeResult) -> None:
    start = "index.html"
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for ref in result.refs:
            if ref.source != cur or ref.resolved_path is None:
                continue
            if ref.resolved_path not in seen:
                seen.add(ref.resolved_path)
                stack.append(ref.resolved_path)
    result.reachable = seen
    for path in result.files:
        if path not in seen:
            result.notices.append(Notice(
                NoticeKind.UNREACHABLE,
                "从入口 index.html 不可达，不会导出", source=path))


# ===========================================================================
# 第四步：改写产物
# ===========================================================================
def build_product(result: AnalyzeResult) -> None:
    product: dict[str, bytes] = {}
    for path in result.reachable:
        entry = result.files[path]
        if entry.kind == "html":
            product[path] = _rewrite_html(result, path, entry.data)
        elif entry.kind == "css":
            product[path] = _rewrite_css(result, path, entry.data)
        else:
            product[path] = entry.data
    result.product = product


def _relative_url(source_file: str, target_file: str,
                  fragment: str) -> str:
    if target_file == source_file:
        rel = ""
    else:
        rel = posixpath.relpath(target_file, posixpath.dirname(source_file))
    rel = quote(rel, safe="/-._~")
    return rel + fragment


def _rewrite_html(result: AnalyzeResult, path: str, data: bytes) -> bytes:
    text = data.decode("utf-8")
    edits: list[tuple[int, int, str]] = []
    for ref in result.refs:
        if ref.source != path:
            continue
        if ref.role is RefRole.BASE:
            if text[ref.start:ref.end] != ".":
                edits.append((ref.start, ref.end, "."))
            continue
        if ref.resolved_path is None:
            continue  # 外部超链接：原样保留
        new_url = _relative_url(path, ref.resolved_path, ref.fragment)
        if ref.role is RefRole.CSS_URL:
            # 内嵌 <style> 中的坐标已是整份 HTML 文本坐标
            replacement = 'url("' + new_url.replace("\\", "\\\\").replace(
                '"', "%22") + '")'
        else:
            replacement = new_url
        old = text[ref.start:ref.end]
        if old != replacement:
            edits.append((ref.start, ref.end, replacement))
    return _apply_edits(text, edits).encode("utf-8")


def _rewrite_css(result: AnalyzeResult, path: str, data: bytes) -> bytes:
    text = data.decode("utf-8")
    edits: list[tuple[int, int, str]] = []
    for ref in result.refs:
        if ref.source != path or ref.role is not RefRole.CSS_URL:
            continue
        # 独立 .css 文件：坐标即文件坐标；<style> 内嵌不会出现在这里
        if ref.style_offset:
            continue
        if ref.resolved_path is None:
            continue
        new_url = _relative_url(path, ref.resolved_path, ref.fragment)
        replacement = 'url("' + new_url.replace("\\", "\\\\").replace(
            '"', "%22") + '")'
        if text[ref.start:ref.end] != replacement:
            edits.append((ref.start, ref.end, replacement))
    return _apply_edits(text, edits).encode("utf-8")


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    if not edits:
        return text
    edits.sort(key=lambda e: e[0])
    parts: list[str] = []
    cursor = 0
    for s, e, repl in edits:
        if s < cursor:
            # 区间重叠说明定位有歧义（复检会兜住）
            raise ValueError("overlapping edits")
        parts.append(text[cursor:s])
        parts.append(repl)
        cursor = e
    parts.append(text[cursor:])
    return "".join(parts)


# ===========================================================================
# 第五步：重扫产物（离线可达性自检）
# ===========================================================================
def rescan_product(result: AnalyzeResult) -> None:
    product = result.product
    if "index.html" not in product:
        result.problems.append(Problem(
            ProblemKind.RESCAN_FAIL, "产物缺少 index.html"))
        return
    re_refs: list[Ref] = []
    re_anchors: dict[str, set[str]] = {}
    for path, data in product.items():
        if path.endswith(".css") and not path.endswith(".html"):
            scan = cssscan.scan_css(data.decode("utf-8"), path)
            if scan.problems:
                result.problems.extend(scan.problems)
                continue
            re_refs.extend(scan.refs)
        elif path.endswith((".html", ".htm")):
            text = data.decode("utf-8")
            hrefs, anchors, problems, css_refs = _scan_html_text(path, text)
            if problems:
                result.problems.extend(problems)
                continue
            re_refs.extend(hrefs)
            re_refs.extend(css_refs)
            re_anchors[path] = anchors

    # 所有本地引用必须命中产物内文件，且锚点有效
    seen: set[str] = {"index.html"}
    stack = ["index.html"]
    pending = re_refs
    while stack:
        cur = stack.pop()
        for ref in pending:
            if ref.source != cur:
                continue
            # <base href> 已改写为 "."，仅定义基准、不构成内链
            if ref.role is RefRole.BASE:
                continue
            parsed = parse_url(ref.raw_url)
            if parsed.external:
                if parsed.blocked:
                    result.problems.append(Problem(
                        ProblemKind.RESCAN_FAIL,
                        "产物中出现被阻止的外部引用", source=cur,
                        line=ref.line, url=ref.raw_url))
                continue
            # 产物中 <base href> 已为 "."，基准即页面目录；
            # 纯片段引用（hier 为空）始终指向当前页。
            if parsed.hier == "":
                target = cur
            else:
                target, _ = resolve_local(
                    parsed.hier, posixpath.dirname(cur))
            if target not in product:
                result.problems.append(Problem(
                    ProblemKind.RESCAN_FAIL,
                    f"产物内链断链：{ref.raw_url!r} -> {target!r}",
                    source=cur, line=ref.line, url=ref.raw_url))
                continue
            frag = unquote(parsed.fragment[1:]) if parsed.fragment else ""
            if frag and (product_file_kind(product, target) != "html"
                         or frag not in re_anchors.get(target, set())):
                result.problems.append(Problem(
                    ProblemKind.RESCAN_FAIL,
                    f"产物锚点失效：{ref.raw_url!r}", source=cur,
                    line=ref.line, url=ref.raw_url))
            if target not in seen:
                seen.add(target)
                stack.append(target)

    missing = set(product) - seen
    if missing:
        result.problems.append(Problem(
            ProblemKind.RESCAN_FAIL,
            "产物中存在从入口不可达的文件：" + ", ".join(sorted(missing))))


def product_file_kind(product: dict[str, bytes], path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".css":
        return "css"
    if ext == ".png":
        return "png"
    return "jpeg"


# ===========================================================================
# 总入口
# ===========================================================================
def analyze(payload: bytes) -> AnalyzeResult:
    result = load(payload)
    if not _fatal_zip(result):
        scan_files(result)
        validate_refs(result)
        compute_reachability(result)
        if not result.problems:
            build_product(result)
            rescan_product(result)
    return result


def load(payload: bytes) -> AnalyzeResult:
    from .ziputil import load_zip
    return load_zip(payload)


def _fatal_zip(result: AnalyzeResult) -> bool:
    return bool(result.problems) or not result.files
