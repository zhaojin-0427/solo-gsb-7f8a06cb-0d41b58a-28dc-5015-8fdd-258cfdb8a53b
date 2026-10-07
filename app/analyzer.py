"""教程离线封包核心：ZIP 校验、引用解析、字节级重写与产物复检。

处理规则（与需求一一对应）：
- 只处理 HTML 的 a[href] / img[src] / link[rel=stylesheet][href] 与 CSS 的 url()；
- 脚本、表单、内联样式、srcset、CSS @import 一律列出文件与原因并禁止下载；
- 引用按所在 HTML 的本地 <base> 或所在 CSS 路径解析；站点根路径指向 ZIP 根；
- 路径与片段各百分号解码一次，保留锚点，带查询串的本地 URL 拒绝；
- 导出仅含从入口可达的文件；本地 base 改为 "./"（当前页目录的相对基准），
  其他本地引用改为从所在文件到原目标的相对 URL；
- 除 URL 属性值 / CSS URL 词元外，其余原文字节不变；无法唯一定位到原文字节时禁止导出；
- 缺失文件、越界路径、归一化重名、失效锚点、外部资源均显示来源与原 URL 并禁止下载；
- 外部普通超链接（http/https/ftp/mailto/tel 及协议相对）原样保留；
- 导出后对产物重新扫描，确认内链与资源都能从入口离线到达。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from html import unescape as html_unescape
from urllib.parse import quote, unquote

import tinycss2
from lxml import etree
from lxml import html as lxml_html

# ---- 限制 ----
MAX_ZIP_BYTES = 10 * 1024 * 1024        # 压缩包 ≤ 10 MB
MAX_UNPACKED_BYTES = 30 * 1024 * 1024   # 解压后 ≤ 30 MB
MAX_TEXT_FILES = 12                     # HTML/CSS ≤ 12 个
MAX_IMAGE_FILES = 30                    # PNG/JPEG ≤ 30 张

ENTRY = "index.html"
UTF8_BOM = b"\xef\xbb\xbf"

HTML_EXTS = {".html", ".htm"}
CSS_EXTS = {".css"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# a[href] 中原样保留的外部方案（普通超链接）
KEEP_LINK_SCHEMES = {"http", "https", "ftp", "mailto", "tel"}
# 视为脚本的方案
SCRIPT_SCHEMES = {"javascript", "vbscript"}

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9A-Fa-f]{1,6})\s?|(.))", re.DOTALL)
_CSS_URL_HEAD_RE = re.compile(r"[uU][rR][lL]\s*\(")
_CSS_WS = " \t\n\r\f"

_ATTR_RX: dict[str, re.Pattern] = {}


def attr_regex(name: str) -> re.Pattern:
    """匹配 HTML 属性（名字前后不能紧跟单词字符或连字符，避免误中 data-href 等）。"""
    rx = _ATTR_RX.get(name)
    if rx is None:
        rx = re.compile(
            r"(?<![\w\-])" + re.escape(name)
            + r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"'`=]+))""",
            re.IGNORECASE,
        )
        _ATTR_RX[name] = rx
    return rx


# ---------------------------------------------------------------- 路径工具

def split_ext(path: str) -> str:
    i = path.rfind(".")
    return path[i:].lower() if i >= 0 else ""


def norm_name(name: str):
    """ZIP 条目名归一化；越界返回 None，空名返回 ''。"""
    parts: list[str] = []
    for seg in name.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def norm_join(base_dir: str, url_path: str):
    """把已解码的 URL 路径解析到站点根下的归一化路径；越界返回 None。

    base_dir 为 ''（根）或 'a/b'（无尾斜杠）；'/...' 指向 ZIP 根。
    """
    if url_path.startswith("/"):
        joined = url_path[1:]
    elif base_dir:
        joined = base_dir + "/" + url_path
    else:
        joined = url_path
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def dir_of(path: str) -> str:
    i = path.rfind("/")
    return path[:i] if i >= 0 else ""


def rel_url(target: str, from_dir: str) -> str:
    """target 相对 from_dir 的相对 URL（逐段百分号编码）。"""
    t = target.split("/")
    f = from_dir.split("/") if from_dir else []
    while f and t and f[0] == t[0]:
        f.pop(0)
        t.pop(0)
    segs = [".."] * len(f) + t
    return "/".join(quote(s, safe="") for s in segs)


def classify_url(raw: str):
    """返回 ('self'|'fragment'|'external'|'scheme'|'local', detail)。"""
    u = raw.strip()
    if u == "":
        return ("self", None)
    if u.startswith("#"):
        return ("fragment", u[1:])
    m = _SCHEME_RE.match(u)
    if m:
        return ("scheme", m.group(1).lower())
    if u.startswith("//"):
        return ("external", None)
    return ("local", u)


# ---------------------------------------------------------------- CSS 工具

def css_unescape(s: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        return m.group(2)
    return _CSS_ESCAPE_RE.sub(repl, s)


def parse_raw_url(text: str, off: int):
    """从 off 处解析原始 url(...) 词元，返回 (end_offset, 解码后的值) 或 None。"""
    m = _CSS_URL_HEAD_RE.match(text, off)
    if not m:
        return None
    i = m.end()
    n = len(text)
    while i < n and text[i] in _CSS_WS:
        i += 1
    if i < n and text[i] in "\"'":
        q = text[i]
        i += 1
        buf: list[str] = []
        closed = False
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                em = _CSS_ESCAPE_RE.match(text, i)
                buf.append(em.group(0))
                i = em.end()
                continue
            if c == q:
                i += 1
                closed = True
                break
            buf.append(c)
            i += 1
        if not closed:
            return None
        value = css_unescape("".join(buf))
    else:
        start = i
        while i < n and text[i] not in ")" + _CSS_WS:
            i += 1
        value = css_unescape(text[start:i])
    while i < n and text[i] in _CSS_WS:
        i += 1
    if i >= n or text[i] != ")":
        return None
    return (i + 1, value)


def iter_tokens(tokens):
    """递归遍历 tinycss2 词元（含 at-rule / 规则 / 函数 / 块的内部）。"""
    for t in tokens:
        yield t
        for attr in ("prelude", "content", "arguments"):
            sub = getattr(t, attr, None)
            if sub:
                yield from iter_tokens(sub)


def css_token_value(tok):
    """提取 url 词元的值：URLToken 直接取值；url("...") 函数取字符串参数。"""
    if tok.type == "url":
        return tok.value
    parts = [t for t in tok.arguments if t.type not in ("whitespace", "comment")]
    if len(parts) == 1 and parts[0].type == "string":
        return parts[0].value
    return None


def css_url_tokens(tokens):
    """递归找出所有 CSS url() 词元（跳过 @import 子树，已另行报错）。"""
    for t in tokens:
        if t.type == "at-rule" and t.lower_at_keyword == "import":
            continue
        if t.type == "url":
            yield t
            continue
        if t.type == "function" and t.lower_name == "url":
            yield t
            continue
        for attr in ("prelude", "content", "arguments"):
            sub = getattr(t, attr, None)
            if sub:
                yield from css_url_tokens(sub)


# ---------------------------------------------------------------- 数据结构

@dataclass
class Issue:
    reason: str
    file: str | None = None
    source: str | None = None
    url: str | None = None

    def to_dict(self):
        d = {"reason": self.reason}
        if self.file:
            d["file"] = self.file
        if self.source:
            d["source"] = self.source
        if self.url is not None:
            d["url"] = self.url
        return d


@dataclass
class Entry:
    path: str           # 归一化路径
    kind: str           # 'html' | 'css' | 'image'
    data: bytes
    text: str = ""      # html/css 的解码文本（已去 BOM）
    bom: bool = False


@dataclass
class Report:
    errors: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    export_bytes: bytes | None = None
    rescan_ok: bool | None = None
    unreachable: list = field(default_factory=list)
    exported: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.export_bytes is not None

    def to_dict(self):
        return {
            "ok": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "infos": [i.to_dict() for i in self.infos],
            "stats": self.stats,
            "rescan_ok": self.rescan_ok,
            "export_ready": self.export_bytes is not None,
            "exported": self.exported,
        }


class HtmlDoc:
    """已解析的 HTML 文档，带原文定位游标与待应用的编辑。"""

    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text
        self.root = lxml_html.document_fromstring(text)
        self.line_off = [0]
        for m in re.finditer("\n", text):
            self.line_off.append(m.end())
        self.ids = set()
        self.anchor_names = set()
        for el in self.root.iter():
            if not isinstance(el.tag, str):
                continue
            v = el.get("id")
            if v:
                self.ids.add(v)
            if el.tag == "a":
                v = el.get("name")
                if v:
                    self.anchor_names.add(v)
        self.edits: list[tuple[int, int, str]] = []
        self.cursor = 0

    def locate_attr(self, el, name: str):
        """按文档顺序在原文中定位属性值，返回 (start, end)；失败返回 None。

        元素按文档顺序处理，游标单调前进，因此相同属性值出现多次也能
        各自唯一定位；定位不到（如解析器规范化导致原文不一致）则失败。
        """
        want = el.get(name)
        if want is None or el.sourceline is None:
            return None
        line = el.sourceline - 1
        base = self.line_off[line] if 0 <= line < len(self.line_off) else 0
        pos = max(base, self.cursor)
        rx = attr_regex(name)
        while True:
            m = rx.search(self.text, pos)
            if m is None:
                return None
            gi = 1 if m.group(1) is not None else (2 if m.group(2) is not None else 3)
            if html_unescape(m.group(gi)) == want:
                self.cursor = m.end(gi)
                return (m.start(gi), m.end(gi))
            pos = m.end()


# ---------------------------------------------------------------- 分析器

class Analyzer:
    def __init__(self):
        self.errors: list[Issue] = []
        self.infos: list[Issue] = []
        self.files: dict[str, Entry] = {}
        self.docs: dict[str, HtmlDoc | None] = {}
        self.css_edits: dict[str, list] = {}
        self.css_line_off: dict[str, list] = {}
        self.reachable: set[str] = set()
        self.queue: list[str] = []

    def err(self, reason, file=None, source=None, url=None):
        self.errors.append(Issue(reason, file, source, url))

    # ---- 阶段 A：ZIP 结构与文件级校验 ----

    def load_zip(self, data: bytes) -> bool:
        if len(data) > MAX_ZIP_BYTES:
            self.err(f"压缩包大小 {len(data)} 字节，超过 10 MB 限制")
            return False
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            self.err("无法读取 ZIP 文件（文件损坏或格式不符）")
            return False

        names: dict[str, str] = {}
        entries: list[tuple[str, zipfile.ZipInfo]] = []
        total = 0
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            total += zi.file_size
            norm = norm_name(zi.filename)
            if norm is None:
                self.err("条目路径越界（.. 逃出根目录）", file=zi.filename)
                continue
            if norm == "":
                self.err("无效的空条目名", file=zi.filename)
                continue
            if norm in names:
                self.err("归一化重名", file=norm,
                         source=f"条目 {names[norm]!r} 与 {zi.filename!r} 归一化后相同")
                continue
            names[norm] = zi.filename
            entries.append((norm, zi))
        if self.errors:
            return False
        if total > MAX_UNPACKED_BYTES:
            self.err(f"解压后大小 {total} 字节，超过 30 MB 限制")
            return False

        kinds: dict[str, str] = {}
        n_text = n_img = 0
        for norm, _zi in entries:
            ext = split_ext(norm)
            if ext in HTML_EXTS:
                kinds[norm] = "html"
                n_text += 1
            elif ext in CSS_EXTS:
                kinds[norm] = "css"
                n_text += 1
            elif ext in IMAGE_EXTS:
                kinds[norm] = "image"
                n_img += 1
            else:
                self.err("不支持的文件类型（仅允许 HTML/CSS/PNG/JPEG）", file=norm)
        if n_text > MAX_TEXT_FILES:
            self.err(f"HTML/CSS 文件共 {n_text} 个，超过 12 个限制")
        if n_img > MAX_IMAGE_FILES:
            self.err(f"图片共 {n_img} 张，超过 30 张限制")
        if self.errors:
            return False

        for norm, zi in entries:
            try:
                b = zf.read(zi)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as ex:
                self.err(f"无法读取条目：{ex}", file=norm)
                continue
            kind = kinds[norm]
            if kind in ("html", "css"):
                bom = b.startswith(UTF8_BOM)
                raw = b[len(UTF8_BOM):] if bom else b
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    self.err("不是有效的 UTF-8 文件", file=norm)
                    continue
                self.files[norm] = Entry(norm, kind, b, text, bom)
            else:
                if not (b.startswith(PNG_MAGIC) or b.startswith(JPEG_MAGIC)):
                    self.err("图片内容不是有效的 PNG/JPEG", file=norm)
                    continue
                self.files[norm] = Entry(norm, kind, b)

        if ENTRY not in self.files:
            self.err("缺少入口文件 index.html（须位于 ZIP 根目录）")
        return not self.errors

    # ---- 阶段 A2：全部 HTML/CSS 的禁用构造扫描 ----

    def get_doc(self, path: str):
        if path in self.docs:
            return self.docs[path]
        doc = None
        try:
            doc = HtmlDoc(path, self.files[path].text)
        except (etree.ParserError, ValueError) as ex:
            self.err(f"HTML 解析失败：{ex}", file=path)
        self.docs[path] = doc
        return doc

    def scan_forbidden(self):
        for path, e in sorted(self.files.items()):
            if e.kind == "html":
                doc = self.get_doc(path)
                if doc is not None:
                    self._scan_html_forbidden(path, doc)
            elif e.kind == "css":
                self._scan_css_forbidden(path, e.text)

    def _scan_html_forbidden(self, path: str, doc: HtmlDoc):
        for el in doc.root.iter():
            if not isinstance(el.tag, str):
                continue
            line = el.sourceline or "?"
            if el.tag == "script":
                self.err("包含脚本 <script>", file=path, source=f"第 {line} 行")
            elif el.tag == "form":
                self.err("包含表单 <form>", file=path, source=f"第 {line} 行")
            elif el.tag == "style":
                self.err("包含内嵌样式块 <style>", file=path, source=f"第 {line} 行")
            if el.get("style") is not None:
                self.err("包含内联样式 style 属性", file=path,
                         source=f"<{el.tag}> 第 {line} 行")
            if el.get("srcset") is not None:
                self.err("包含 srcset 属性", file=path,
                         source=f"<{el.tag}> 第 {line} 行")

    def _scan_css_forbidden(self, path: str, text: str):
        try:
            rules = tinycss2.parse_stylesheet(text, skip_comments=True,
                                              skip_whitespace=True)
        except Exception as ex:  # tinycss2 极少抛错，兜底
            self.err(f"CSS 解析失败：{ex}", file=path)
            return
        for tok in iter_tokens(rules):
            if tok.type == "at-rule" and tok.lower_at_keyword == "import":
                self.err("包含 CSS @import", file=path,
                         source=f"第 {tok.source_line} 行")
            elif tok.type == "error" and ("url" in tok.kind or "string" in tok.kind):
                self.err(f"CSS 词法错误（{tok.kind}）", file=path,
                         source=f"第 {tok.source_line} 行")

    # ---- 阶段 B：从入口可达性遍历与引用解析 ----

    def enqueue(self, target: str):
        if target not in self.reachable:
            self.reachable.add(target)
            self.queue.append(target)

    def process(self):
        if ENTRY not in self.files:
            return
        self.enqueue(ENTRY)
        while self.queue:
            path = self.queue.pop(0)
            e = self.files[path]
            if e.kind == "html":
                self.process_html(path, e)
            elif e.kind == "css":
                self.process_css_refs(path, e)

    def process_html(self, path: str, entry: Entry):
        doc = self.get_doc(path)
        if doc is None:
            return
        page_dir = dir_of(path)
        base_dir = page_dir
        self_target = path
        base_seen = False
        # 按文档顺序处理，保证原文定位游标单调前进
        for el in doc.root.iter():
            if not isinstance(el.tag, str):
                continue
            tag = el.tag
            if tag == "base":
                href = el.get("href")
                if href is None:
                    continue
                if base_seen:  # 仅第一个 <base href> 生效，其余仅推进游标
                    doc.locate_attr(el, "href")
                    continue
                base_seen = True
                res = self.resolve_base(doc, el, path, page_dir)
                if res is not None:
                    base_dir, self_target = res
            elif tag == "a" and el.get("href") is not None:
                self.ref_hyperlink(doc, el, path, base_dir, self_target)
            elif tag == "img" and el.get("src") is not None:
                self.ref_resource(doc, el, "src", path, base_dir, "image")
            elif tag == "link" and el.get("href") is not None:
                rel = (el.get("rel") or "").lower().split()
                if "stylesheet" in rel:
                    self.ref_resource(doc, el, "href", path, base_dir, "css")
                else:
                    doc.locate_attr(el, "href")  # 不处理，仅推进游标

    def resolve_base(self, doc: HtmlDoc, el, path: str, page_dir: str):
        """解析本地 <base href>，返回 (base_dir, self_target)；失败返回 None。

        无论成败都会把 base 重写为 './'（当前页目录的相对基准）。
        """
        raw = el.get("href")
        src = f"<base> 第 {el.sourceline or '?'} 行"
        result = None
        kind, _detail = classify_url(raw)
        if kind in ("external", "scheme"):
            self.err("base href 必须是本地路径", file=path, source=src, url=raw)
        else:
            u = raw.strip()
            pathpart = u.partition("#")[0]
            try:
                ppath = unquote(pathpart, encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                self.err("百分号编码不是有效的 UTF-8", file=path, source=src, url=raw)
                ppath = None
            if ppath is not None:
                if "?" in ppath:
                    self.err("本地 URL 不允许带查询串", file=path, source=src, url=raw)
                else:
                    r = norm_join(page_dir, ppath)
                    if r is None:
                        self.err("base 路径越界（.. 逃出站点根）", file=path,
                                 source=src, url=raw)
                    elif ppath == "" or ppath.endswith("/"):
                        result = (r, path)
                    else:
                        result = (dir_of(r), r)
        span = doc.locate_attr(el, "href")
        if span is None:
            self.err("base 无法唯一定位到原文字节，禁止导出", file=path,
                     source=src, url=raw)
        else:
            doc.edits.append((span[0], span[1], "./"))
        if result is not None and result[1] != path and result[1] in self.files:
            self.enqueue(result[1])
        return result

    def ref_hyperlink(self, doc, el, path, base_dir, self_target):
        raw = el.get("href")
        src = f"<a> 第 {el.sourceline or '?'} 行"
        kind, detail = classify_url(raw)
        if kind == "self":
            return
        if kind == "fragment":
            try:
                frag = unquote(detail, encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                self.err("百分号编码不是有效的 UTF-8", file=path, source=src, url=raw)
                return
            if self_target not in self.files:
                self.err("引用的文件缺失", file=path, source=src, url=raw)
                return
            self.check_anchor(path, self_target, frag, src, raw)
            return
        if kind == "external":
            return  # 协议相对的外部普通超链接，原样保留
        if kind == "scheme":
            if detail in KEEP_LINK_SCHEMES:
                return  # 外部普通超链接，原样保留
            if detail in SCRIPT_SCHEMES:
                self.err(f"包含脚本类链接（{detail}:）", file=path, source=src, url=raw)
            else:
                self.err(f"不允许的 URL 方案：{detail}:", file=path, source=src, url=raw)
            return
        # 本地链接
        target, frag = self.resolve_local(path, base_dir, raw, src)
        if target is None:
            return
        self.enqueue(target)
        self.check_anchor(path, target, frag, src, raw)
        self.rewrite_attr(doc, el, "href", path, target, frag)

    def ref_resource(self, doc, el, attr, path, base_dir, want):
        raw = el.get(attr)
        src = f"<{el.tag}> 第 {el.sourceline or '?'} 行"
        kind, _detail = classify_url(raw)
        if kind in ("external", "scheme"):
            self.err("引用了外部资源", file=path, source=src, url=raw)
            return
        if kind in ("self", "fragment"):
            self.err("资源引用为空或仅含锚点", file=path, source=src, url=raw)
            return
        target, frag = self.resolve_local(path, base_dir, raw, src)
        if target is None:
            return
        if self.files[target].kind != want:
            want_cn = "图片（PNG/JPEG）" if want == "image" else "CSS 文件"
            self.err(f"资源类型不匹配（应为{want_cn}）", file=path, source=src, url=raw)
            return
        self.enqueue(target)
        self.rewrite_attr(doc, el, attr, path, target, frag)

    def resolve_local(self, src_path, base_dir, raw, src_desc):
        """本地 URL → (目标路径, 解码片段或 None)；失败记录错误返回 (None, None)。"""
        u = raw.strip()
        pathpart, sep, frag = u.partition("#")
        if "?" in pathpart:
            self.err("本地 URL 不允许带查询串", file=src_path, source=src_desc, url=raw)
            return None, None
        try:
            ppath = unquote(pathpart, encoding="utf-8", errors="strict")
            frag_dec = unquote(frag, encoding="utf-8", errors="strict") if sep else None
        except UnicodeDecodeError:
            self.err("百分号编码不是有效的 UTF-8", file=src_path,
                     source=src_desc, url=raw)
            return None, None
        target = norm_join(base_dir, ppath)
        if target is None:
            self.err("路径越界（.. 逃出站点根）", file=src_path, source=src_desc, url=raw)
            return None, None
        if target == "" or target not in self.files:
            self.err("引用的文件缺失", file=src_path, source=src_desc, url=raw)
            return None, None
        return target, frag_dec

    def check_anchor(self, src_path, target, frag, src, raw):
        if not frag:
            return
        e = self.files.get(target)
        if e is None or e.kind != "html":
            return
        tdoc = self.get_doc(target)
        if tdoc is None:
            return
        if frag not in tdoc.ids and frag not in tdoc.anchor_names:
            self.err(f"失效锚点：{target} 中不存在 id/name 为 {frag!r} 的元素",
                     file=src_path, source=src, url=raw)

    def rewrite_attr(self, doc, el, attr, src_path, target, frag):
        new_url = rel_url(target, dir_of(src_path))
        if frag is not None:
            new_url += "#" + quote(frag, safe="")
        span = doc.locate_attr(el, attr)
        if span is None:
            self.err("引用无法唯一定位到原文字节，禁止导出", file=src_path,
                     source=f"<{el.tag}> 第 {el.sourceline or '?'} 行",
                     url=el.get(attr))
            return
        doc.edits.append((span[0], span[1], new_url))

    # ---- CSS 引用 ----

    def process_css_refs(self, path: str, entry: Entry):
        text = entry.text
        line_off = self.css_line_off.get(path)
        if line_off is None:
            line_off = [0]
            for m in re.finditer("\n", text):
                line_off.append(m.end())
            self.css_line_off[path] = line_off
        css_dir = dir_of(path)
        edits = []
        try:
            rules = tinycss2.parse_stylesheet(text, skip_comments=True,
                                              skip_whitespace=True)
        except Exception:
            return  # 词法问题已在禁用扫描中报告
        for tok in css_url_tokens(rules):
            raw = css_token_value(tok)
            src = f"CSS 第 {tok.source_line} 行"
            if raw is None:
                self.err("CSS url() 语法无效", file=path, source=src)
                continue
            kind, _detail = classify_url(raw)
            if kind in ("external", "scheme"):
                self.err("CSS url() 引用了外部资源", file=path, source=src, url=raw)
                continue
            if kind in ("self", "fragment"):
                self.err("CSS url() 不是有效的本地资源引用", file=path, source=src,
                         url=raw)
                continue
            target, frag = self.resolve_local(path, css_dir, raw, src)
            if target is None:
                continue
            if self.files[target].kind != "image":
                self.err("CSS url() 只能引用 PNG/JPEG 图片", file=path, source=src,
                         url=raw)
                continue
            self.enqueue(target)
            li = tok.source_line - 1
            if li >= len(line_off):
                self.err("CSS url() 无法唯一定位到原文字节，禁止导出", file=path,
                         source=src, url=raw)
                continue
            off = line_off[li] + tok.source_column - 1
            parsed = parse_raw_url(text, off)
            if parsed is None or parsed[1] != raw:
                self.err("CSS url() 无法唯一定位到原文字节，禁止导出", file=path,
                         source=src, url=raw)
                continue
            new_url = rel_url(target, css_dir)
            if frag is not None:
                new_url += "#" + quote(frag, safe="")
            edits.append((off, parsed[0], f'url("{new_url}")'))
        self.css_edits[path] = edits

    # ---- 阶段 C：导出 ----

    @staticmethod
    def apply_edits(text: str, edits) -> str:
        for start, end, repl in sorted(edits, key=lambda x: x[0], reverse=True):
            text = text[:start] + repl + text[end:]
        return text

    def build_export(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(self.reachable):
                e = self.files[path]
                if e.kind == "html":
                    doc = self.docs.get(path)
                    text = self.apply_edits(e.text, doc.edits if doc else [])
                    data = (UTF8_BOM if e.bom else b"") + text.encode("utf-8")
                elif e.kind == "css":
                    text = self.apply_edits(e.text, self.css_edits.get(path, []))
                    data = (UTF8_BOM if e.bom else b"") + text.encode("utf-8")
                else:
                    data = e.data
                zf.writestr(path, data)
        return buf.getvalue()


# ---------------------------------------------------------------- 入口

def analyze(data: bytes, is_rescan: bool = False) -> Report:
    a = Analyzer()
    unreachable: list[str] = []
    if a.load_zip(data):
        a.scan_forbidden()
        a.process()
        unreachable = sorted(p for p in a.files if p not in a.reachable)
        for p in unreachable:
            a.infos.append(Issue("未从入口可达，导出时排除", file=p))

    export_bytes = None
    rescan_ok = None
    exported: list[str] = []
    if not a.errors and not is_rescan:
        candidate = a.build_export()
        # 阶段 D：重扫产物，确认内链与资源都能从入口离线到达
        inner = analyze(candidate, is_rescan=True)
        if inner.errors or inner.unreachable:
            details = [i.reason for i in inner.errors[:5]]
            if inner.unreachable:
                details.append("存在不可达文件：" + ", ".join(inner.unreachable[:5]))
            a.err("产物复检未通过：" + "；".join(details))
            rescan_ok = False
        else:
            export_bytes = candidate
            rescan_ok = True
            exported = sorted(a.reachable)

    stats = {
        "zip_bytes": len(data),
        "text_files": sum(1 for e in a.files.values() if e.kind in ("html", "css")),
        "image_files": sum(1 for e in a.files.values() if e.kind == "image"),
        "reachable_files": len(a.reachable),
        "export_bytes": len(export_bytes) if export_bytes else 0,
    }
    return Report(errors=a.errors, infos=a.infos, stats=stats,
                  export_bytes=export_bytes, rescan_ok=rescan_ok,
                  unreachable=unreachable, exported=exported)
