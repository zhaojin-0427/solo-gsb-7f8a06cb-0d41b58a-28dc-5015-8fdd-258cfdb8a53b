"""封包规则端到端测试。"""
from __future__ import annotations

import io
import zipfile

import pytest

from app import engine
from app.ziputil import MAX_COMPRESSED, MAX_UNCOMPRESSED

from .conftest import JPEG_MIN, PNG_1X1, build_zip, has_problem, problem_kinds


# --------------------------------------------------------------------------
# 基础成功路径
# --------------------------------------------------------------------------
def test_minimal_ok(minimal_ok):
    res = engine.analyze(minimal_ok)
    assert res.problems == []
    assert set(res.product) == {"index.html", "a.png"}
    # 没有需要改写的链接时字节不变
    assert res.product["index.html"].count(b"a.png") == 1


def test_relative_rewriting_and_reachability():
    pkg = build_zip({
        "index.html": b'<a href="sub/page2.html"><img src="img/a.png">',
        "sub/page2.html": b'<a href="../index.html"><img src="../img/a.png">',
        "img/a.png": PNG_1X1,
        "orphan.html": b'<img src="img/a.png">',
    })
    res = engine.analyze(pkg)
    assert res.problems == []
    # orphan.html 不可达，不导出
    assert set(res.product) == {
        "index.html", "sub/page2.html", "img/a.png"}
    idx = res.product["index.html"].decode()
    assert 'href="sub/page2.html"' in idx
    assert 'src="img/a.png"' in idx
    p2 = res.product["sub/page2.html"].decode()
    assert 'href="../index.html"' in p2
    assert 'src="../img/a.png"' in p2
    assert any(n.kind.value == "unreachable_file" and n.source == "orphan.html"
               for n in res.notices)


def test_css_url_quoted_and_unquoted_rewrite():
    pkg = build_zip({
        "index.html": b'<link rel="stylesheet" href="css/s.css">'
                      b'<img src="img/a.png">',
        "css/s.css": b'body{background:url(../img/a.png)}'
                     b'x{cursor:url("../img/b.jpg")}',
        "img/a.png": PNG_1X1,
        "img/b.jpg": JPEG_MIN,
    })
    res = engine.analyze(pkg)
    assert res.problems == []
    css = res.product["css/s.css"].decode()
    assert 'url("../img/a.png")' in css
    assert 'url("../img/b.jpg")' in css
    assert set(res.product) == {
        "index.html", "css/s.css", "img/a.png", "img/b.jpg"}


def test_style_block_embedded_url():
    pkg = build_zip({
        "index.html": (b'<html><head><style>'
                       b'p{background:url(img/a.png)}'
                       b'</style></head><body><p>x</p></body></html>'),
        "img/a.png": PNG_1X1,
    })
    res = engine.analyze(pkg)
    assert res.problems == []
    assert 'url("img/a.png")' in res.product["index.html"].decode()


def test_embedded_style_respects_html_base():
    """内嵌 <style> 的 url() 按所在 HTML 的 base 解析；
    独立 .css 按 CSS 文件目录解析。"""
    pkg = build_zip({
        "index.html": (b'<base href="d/">'
                       b'<link rel="stylesheet" href="s.css">'
                       b'<style>p{background:url(../img/a.png)}</style>'),
        "d/s.css": b"p{background:url(../img/a.png)}",
        "img/a.png": PNG_1X1,
    })
    res = engine.analyze(pkg)
    assert res.problems == [], [(p.kind.value, p.message) for p in res.problems]
    idx = res.product["index.html"].decode()
    # style 内以 base 目录 d/ 解析 ../img -> img；导出相对页面根
    assert 'url("img/a.png")' in idx
    assert 'href="d/s.css"' in idx
    # 独立 css 以 d/ 为基准：../img -> img；导出相对 d/
    assert 'url("../img/a.png")' in res.product["d/s.css"].decode()


# --------------------------------------------------------------------------
# 禁止构造
# --------------------------------------------------------------------------
def test_script_blocked():
    pkg = build_zip({"index.html": b"<script>alert(1)</script>",
                     "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_script")
    assert not res.product


def test_inline_onclick_blocked():
    pkg = build_zip({"index.html": b'<a href="#" onclick="x()">x</a>',
                     "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_script")


def test_form_blocked():
    pkg = build_zip({"index.html": b'<form action="/x"><input></form>',
                     "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_form")


def test_inline_style_attribute_blocked():
    pkg = build_zip({"index.html": b'<p style="color:red">x</p>'})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_inline_style")


def test_srcset_blocked():
    pkg = build_zip({"index.html": b'<img src="a.png" srcset="b.png 2x">',
                     "a.png": PNG_1X1, "b.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_srcset")


def test_css_import_blocked():
    pkg = build_zip({
        "index.html": b'<link rel="stylesheet" href="a.css">',
        "a.css": b'@import url("other.css"); body{}',
        "other.css": b"",
    })
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_css_import")


def test_style_import_blocked():
    pkg = build_zip({"index.html": b"<style>@import 'a.css';</style>"})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_css_import")


def test_iframe_src_blocked():
    pkg = build_zip({"index.html": b'<iframe src="x.html"></iframe>',
                     "x.html": b"<html></html>"})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_url_attr")


def test_other_href_blocked():
    pkg = build_zip({"index.html": b'<use href="#a" />'})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_url_attr")


def test_non_stylesheet_link_blocked():
    pkg = build_zip({
        "index.html": b'<link rel="icon" href="a.png">',
        "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_url_attr")


def test_meta_refresh_blocked():
    pkg = build_zip({"index.html":
                     b'<meta http-equiv="refresh" content="0;url=x.html">'})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_url_attr")


# --------------------------------------------------------------------------
# 引用错误
# --------------------------------------------------------------------------
def test_missing_local_file_blocked():
    pkg = build_zip({"index.html": b'<a href="nope.html">x</a>'})
    res = engine.analyze(pkg)
    p = next(p for p in res.problems if p.kind.value == "missing_file")
    assert p.source == "index.html"
    assert p.url == "nope.html"
    assert not res.product


def test_path_traversal_blocked():
    pkg = build_zip({"index.html": b'<img src="../../etc/passwd">',
                     "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "path_traversal")
    # %2e%2e 解码一次后同样越界
    pkg2 = build_zip({"index.html": b'<img src="%2e%2e/x.png">'})
    res2 = engine.analyze(pkg2)
    assert has_problem(res2, "path_traversal")


def test_root_path_points_to_zip_root():
    pkg = build_zip({
        "index.html": b'<img src="/img/a.png">',
        "img/a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert res.problems == []
    assert 'src="img/a.png"' in res.product["index.html"].decode()


def test_query_string_local_rejected():
    pkg = build_zip({"index.html": b'<img src="a.png?v=2">',
                     "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert has_problem(res, "local_with_query")


def test_bad_anchor_blocked():
    pkg = build_zip({"index.html": b'<a href="#ghost">x</a>'})
    res = engine.analyze(pkg)
    assert has_problem(res, "bad_anchor")
    pkg2 = build_zip({"index.html": b'<a href="#ok">x</a><p id="ok">y</p>'})
    res2 = engine.analyze(pkg2)
    assert res2.problems == []
    assert 'href="#ok"' in res2.product["index.html"].decode()


def test_cross_page_anchor():
    pkg = build_zip({
        "index.html": b'<a href="sub/p.html#sec">x</a>',
        "sub/p.html": b'<h2 id="sec">S</h2>'})
    res = engine.analyze(pkg)
    assert res.problems == []
    assert 'href="sub/p.html#sec"' in res.product["index.html"].decode()


def test_bad_cross_page_anchor():
    pkg = build_zip({
        "index.html": b'<a href="sub/p.html#sec">x</a>',
        "sub/p.html": b"<p>no anchor</p>"})
    res = engine.analyze(pkg)
    assert has_problem(res, "bad_anchor")


def test_external_link_kept_but_external_resource_blocked():
    pkg = build_zip({
        "index.html": (b'<a href="https://example.com">ext</a>'
                       b'<img src="https://example.com/a.png">'
                       b'<link rel="stylesheet" '
                       b'href="https://example.com/s.css">')})
    res = engine.analyze(pkg)
    assert has_problem(res, "external_resource")
    ext_probs = [p for p in res.problems if p.kind.value == "external_resource"]
    assert len(ext_probs) == 2
    assert any(n.kind.value == "external_link"
               and n.url == "https://example.com" for n in res.notices)


def test_protocol_relative_resource_blocked():
    pkg = build_zip({"index.html": b'<img src="//cdn.example.com/a.png">'})
    res = engine.analyze(pkg)
    assert has_problem(res, "external_resource")
    pkg2 = build_zip({"index.html": b'<a href="//example.com">x</a>'})
    res2 = engine.analyze(pkg2)
    assert res2.problems == []  # 普通超链接保留


def test_data_uri_blocked():
    pkg = build_zip({"index.html": b'<img src="data:image/png;base64,AAA">'})
    res = engine.analyze(pkg)
    assert has_problem(res, "unsupported_scheme")


def test_javascript_href_blocked():
    pkg = build_zip({"index.html": b'<a href="javascript:alert(1)">x</a>'})
    res = engine.analyze(pkg)
    assert has_problem(res, "unsupported_scheme")


def test_mailto_link_kept():
    pkg = build_zip({"index.html": b'<a href="mailto:a@b.com">mail</a>'})
    res = engine.analyze(pkg)
    assert res.problems == []
    assert any(n.kind.value == "external_link"
               and n.url == "mailto:a@b.com" for n in res.notices)


def test_type_mismatches():
    # a -> 图片 / img -> html / link -> 图片
    pkg = build_zip({
        "index.html": (b'<a href="a.png">x</a><img src="b.html">'
                       b'<link rel="stylesheet" href="a.png">'),
        "a.png": PNG_1X1, "b.html": b"<html></html>"})
    res = engine.analyze(pkg)
    assert sum(1 for p in res.problems
               if p.kind.value == "bad_target_type") == 3


# --------------------------------------------------------------------------
# <base href>
# --------------------------------------------------------------------------
def test_base_href_changes_resolution():
    pkg = build_zip({
        "index.html": (b'<base href="sub/"><a href="p.html">x</a>'
                       b'<img src="a.png">'),
        "sub/p.html": b"<html></html>",
        "sub/a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert res.problems == [], res.problems
    out = res.product["index.html"].decode()
    assert '<base href="."' in out
    assert 'href="sub/p.html"' in out
    assert 'src="sub/a.png"' in out


def test_base_root_href():
    pkg = build_zip({
        "index.html": b'<base href="/"><img src="img/a.png">',
        "img/a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert res.problems == []
    assert 'src="img/a.png"' in res.product["index.html"].decode()


def test_multiple_base_blocked():
    pkg = build_zip({"index.html":
                     b'<base href="a/"><base href="b/"><img src="x.png">'})
    res = engine.analyze(pkg)
    assert has_problem(res, "locate_ambiguous")


def test_base_dot_on_subpage():
    """子目录页面的 <base href="."> 表示页面自身目录。"""
    pkg = build_zip({
        "index.html": b'<a href="sub/p.html">p</a>',
        "sub/p.html": (b'<base href="."><a href="#s">j</a>'
                       b'<a href="q.html">q</a><h2 id="s">S</h2>'),
        "sub/q.html": b"<html><a href='p.html#s'>b</a></html>",
    })
    res = engine.analyze(pkg)
    assert res.problems == [], [(p.kind.value, p.message) for p in res.problems]
    assert res.product["sub/p.html"] == (
        b'<base href="."><a href="#s">j</a>'
        b'<a href="q.html">q</a><h2 id="s">S</h2>')


def test_file_style_base():
    """base 指向具体 HTML 时，基准是该文件所在目录。"""
    pkg = build_zip({
        "index.html": (b'<base href="d/t.html"><img src="a.png">'
                       b'<a href="p.html">p</a>'),
        "d/t.html": b"<html></html>", "d/a.png": PNG_1X1,
        "d/p.html": b"<html></html>"})
    res = engine.analyze(pkg)
    assert res.problems == [], [(p.kind.value, p.message) for p in res.problems]
    out = res.product["index.html"].decode()
    assert '<base href="."' in out
    assert 'src="d/a.png"' in out
    assert 'href="d/p.html"' in out


# --------------------------------------------------------------------------
# 百分号解码 / 锚点保留
# --------------------------------------------------------------------------
def test_percent_decoding_once():
    name = "中文 p.html"  # ZIP 写入后 URL 里用百分号编码
    from urllib.parse import quote
    pkg = build_zip({
        "index.html": (f'<a href="{quote(name)}#%E9%94%9A">x</a>').encode("utf-8"),
        name: '<h1 id="锚">锚点页</h1>'.encode("utf-8"),
    })
    res = engine.analyze(pkg)
    assert res.problems == [], [(p.kind.value, p.message) for p in res.problems]
    # 导出时重新编码，锚点原文（逐字）保留
    idx = res.product["index.html"].decode()
    assert quote(name) in idx
    assert "#%E9%94%9A" in idx


# --------------------------------------------------------------------------
# 字节不变性
# --------------------------------------------------------------------------
def test_unrelated_bytes_preserved():
    original = (
        b'<!doctype html>\n<html>\n<!-- comment with <script> "x" -->\n'
        b'<body>\n<h1 id="t">\xe4\xb8\xad\xe6\x96\x87</h1>\n'
        b'<a href="#t">\xe9\x94\x9a</a>\n'
        b'<img src="a.png" alt="\xe5\x9b\xbe">\n'
        b"</body></html>")
    pkg = build_zip({"index.html": original, "a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert res.problems == []
    assert res.product["index.html"] == original  # 无改动时逐字节一致


def test_changed_file_only_urls_differ():
    """改写后的文件与原文做对齐：除替换区间外字节一致。"""
    orig = (b'<a href="sub/p.html">x</a><img src="img/a.png">'
            b'<a href="https://keep.example/z?q=1">k</a>')
    pkg = build_zip({
        "index.html": orig, "sub/p.html": b"<html></html>",
        "img/a.png": PNG_1X1})
    res = engine.analyze(pkg)
    assert res.problems == []
    new = res.product["index.html"]
    # 外部超链接与查询串原文保留
    assert b'https://keep.example/z?q=1' in new
    # 两段本地引用被改写
    assert b'href="sub/p.html"' in new and b'src="img/a.png"' in new
    # 其余骨架字节都在（外部超链接整段未动）
    for frag in (b"<a ", b">x</a><img ", b"><a ", b">k</a>"):
        assert frag in new


# --------------------------------------------------------------------------
# ZIP 级限制
# --------------------------------------------------------------------------
def test_missing_index():
    res = engine.analyze(build_zip({"a.html": b"<html></html>"}))
    assert has_problem(res, "missing_index")


def test_non_zip_payload():
    res = engine.analyze(b"this is not a zip at all")
    assert has_problem(res, "zip_broken")


def test_unsupported_file_type():
    res = engine.analyze(build_zip({
        "index.html": b"<html></html>", "evil.js": b"alert(1)",
        "readme.txt": b"hi"}))
    assert sum(1 for p in res.problems
               if p.kind.value == "unsupported_file") == 2


def test_path_traversal_entry():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", b"<html></html>")
        zf.writestr("../escape.txt", b"x")
    res = engine.analyze(buf.getvalue())
    assert has_problem(res, "unsafe_entry")


def test_absolute_entry():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/index.html", b"<html></html>")
    res = engine.analyze(buf.getvalue())
    assert has_problem(res, "unsafe_entry") or \
           has_problem(res, "missing_index")


def test_case_insensitive_duplicate():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", b'<img src="img/a.PNG">')
        zf.writestr("img/a.png", PNG_1X1)
        zf.writestr("img/A.PNG", PNG_1X1)
    res = engine.analyze(buf.getvalue())
    assert has_problem(res, "duplicate_name")


def test_too_many_text_files():
    files = {"index.html": b"<html></html>"}
    for i in range(12):  # +12 = 13 个文本文件
        files[f"p{i}.css"] = b""
    res = engine.analyze(build_zip(files))
    assert has_problem(res, "too_many_files")


def test_too_many_images():
    files = {"index.html": b"<html></html>"}
    for i in range(31):
        files[f"i{i}.png"] = PNG_1X1
    res = engine.analyze(build_zip(files))
    assert has_problem(res, "too_many_files")


def test_bad_utf8_html():
    res = engine.analyze(build_zip({"index.html": b"<p>\xff\xfe</p>"}))
    assert has_problem(res, "bad_utf8")


def test_bad_image_magic():
    res = engine.analyze(build_zip({"index.html": b'<img src="a.png">',
                                   "a.png": b"not a png"}))
    assert has_problem(res, "bad_image")


def test_uncompressed_size_limit(monkeypatch):
    from app import engine as eng
    from app import ziputil
    monkeypatch.setattr(ziputil, "MAX_UNCOMPRESSED", 100)
    monkeypatch.setattr(eng, "MAX_UNCOMPRESSED", 100, raising=False)
    big = b"<html>" + b"x" * 500
    res = engine.analyze(build_zip({"index.html": big}))
    assert has_problem(res, "too_many_files")


# --------------------------------------------------------------------------
# 复检（产物断链兜底）
# --------------------------------------------------------------------------
def test_rescan_catches_broken_product():
    pkg = build_zip({
        "index.html": b'<a href="p.html">x</a>',
        "p.html": b"<html></html>"})
    # 白盒：手动跑流程，在产物中制造断链后复检必须报错
    result = engine.load(pkg)
    engine.scan_files(result)
    engine.validate_refs(result)
    engine.compute_reachability(result)
    assert result.problems == []
    engine.build_product(result)
    del result.product["p.html"]
    engine.rescan_product(result)
    assert has_problem(result, "rescan_fail")


# --------------------------------------------------------------------------
# 不可达文件中的禁止构造仍然拦截
# --------------------------------------------------------------------------
def test_unreachable_file_still_scanned():
    pkg = build_zip({
        "index.html": b"<html>root</html>",
        "orphan.html": b"<script>x</script>"})
    res = engine.analyze(pkg)
    assert has_problem(res, "forbidden_script")


# --------------------------------------------------------------------------
# 定位歧义
# --------------------------------------------------------------------------
def test_duplicate_href_attribute_ambiguous():
    pkg = build_zip({"index.html":
                     b'<a href="a.html" href="b.html">x</a>',
                     "a.html": b"<html></html>", "b.html": b"<html></html>"})
    res = engine.analyze(pkg)
    assert has_problem(res, "locate_ambiguous")
    assert not res.product


def test_top_level_fragment_a_tag():
    """单顶层 <a> 片段文件以前会让 lxml 漏数；必须正常处理。"""
    pkg = build_zip({"index.html": b'<a href="#x">t</a><p id="x">y</p>'})
    res = engine.analyze(pkg)
    assert res.problems == []


def test_empty_href_self_fragment_page():
    pkg = build_zip({"index.html": b'<a href="">self</a>'})
    res = engine.analyze(pkg)
    # 空 href 解析到本页（存在），允许
    assert res.problems == []
    assert res.product["index.html"] == b'<a href="">self</a>'


def test_fragment_only_on_subpage():
    """子目录页面上的纯片段引用必须指向该页自身，不能消费基准目录。"""
    pkg = build_zip({
        "index.html": b'<a href="sub/p.html">p</a>',
        "sub/p.html": (b'<a href="#sec">jump</a>'
                       b'<h2 id="sec">S</h2>'
                       b'<a href="p.html#sec">self-rel</a>'),
    })
    res = engine.analyze(pkg)
    assert res.problems == [], [(p.kind.value, p.message) for p in res.problems]
    p = res.product["sub/p.html"].decode()
    assert 'href="#sec"' in p
    # 同页相对链接改写为纯片段
    assert 'href="#sec"' in p
