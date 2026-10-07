#!/usr/bin/env python3
"""规则冒烟测试：直接调用 analyzer.analyze 验证各类场景。"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from analyzer import analyze  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

PASSED = []
FAILED = []


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buf.getvalue()


def read_export(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    return {i.filename: zf.read(i) for i in zf.infolist()}


def case(name):
    def deco(fn):
        cases.append((name, fn))
        return fn
    return deco


cases = []


@case("基本封包：链接/图片/样式重写，外链保留，不可达排除")
def _():
    files = {
        "index.html": (
            '<!DOCTYPE html>\n<html><head>\n'
            '<link rel="stylesheet" href="css/style.css">\n'
            '</head><body>\n'
            '<h1 id="top">教程</h1>\n'
            '<a href="docs/page2.html">下一页</a>\n'
            '<img src="images/pic.png">\n'
            '<a href="https://example.com/?a=1&b=2">外部链接</a>\n'
            '<a href="#top">回顶部</a>\n'
            '</body></html>'
        ),
        "docs/page2.html": (
            '<!DOCTYPE html>\n<html><body>\n'
            '<a href="../index.html#top">回首页</a>\n'
            '</body></html>'
        ),
        "css/style.css": (
            "body { background: url(\"../images/bg.png\"); }\n"
            ".a { background: url(../images/pic.png) }\n"
        ),
        "images/pic.png": PNG,
        "images/bg.png": PNG,
        "unreachable.html": "<html><body>孤立页</body></html>",
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    assert rep.rescan_ok is True
    out = read_export(rep.export_bytes)
    assert set(out) == {"index.html", "docs/page2.html", "css/style.css",
                        "images/pic.png", "images/bg.png"}, sorted(out)
    idx = out["index.html"].decode()
    assert 'href="css/style.css"' in idx
    assert 'href="docs/page2.html"' in idx
    assert 'src="images/pic.png"' in idx
    assert 'href="https://example.com/?a=1&b=2"' in idx  # 外链原样保留
    assert 'href="#top"' in idx  # 纯锚点保留
    p2 = out["docs/page2.html"].decode()
    assert 'href="../index.html#top"' in p2, p2
    css = out["css/style.css"].decode()
    assert 'url("../images/bg.png")' in css, css
    assert 'url("../images/pic.png")' in css, css
    # 不可达文件被排除且有提示
    assert any(i.file == "unreachable.html" for i in rep.infos)


@case("本地 base：按 base 解析，导出时改为 ./")
def _():
    files = {
        "index.html": '<html><body><a href="sub/page.html">go</a></body></html>',
        "sub/page.html": (
            '<html><head><base href="../"></head>\n'
            '<body><img src="images/pic.png">'
            '<a href="docs/next.html">next</a></body></html>'
        ),
        "docs/next.html": "<html><body><a href=\"/index.html\">top</a></body></html>",
        "images/pic.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    page = out["sub/page.html"].decode()
    assert '<base href="./">' in page, page
    assert 'src="../images/pic.png"' in page, page
    assert 'href="../docs/next.html"' in page, page
    nxt = out["docs/next.html"].decode()
    assert 'href="../index.html"' in nxt, nxt  # 根路径 / 指向 ZIP 根


@case("脚本/表单/内联样式/srcset/style 块均禁止下载")
def _():
    bad = {
        "script": '<html><body><script src="x.js"></script></body></html>',
        "form": '<html><body><form action="x"></form></body></html>',
        "style_attr": '<html><body><p style="color:red">x</p></body></html>',
        "srcset": '<html><body><img src="a.png" srcset="a.png 1x"></body></html>',
        "style_tag": '<html><head><style>body{color:red}</style></head></html>',
    }
    for key, html in bad.items():
        files = {"index.html": html, "a.png": PNG}
        rep = analyze(make_zip(files))
        assert not rep.ok and rep.export_bytes is None, key
        assert any("index.html" == i.file for i in rep.errors), key


@case("CSS @import 禁止下载")
def _():
    files = {
        "index.html": '<html><head><link rel="stylesheet" href="a.css"></head></html>',
        "a.css": '@import "b.css";\nbody { color: red; }',
        "b.css": "body { color: blue; }",
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert any("@import" in i.reason and i.file == "a.css" for i in rep.errors)


@case("缺失文件：显示来源与原 URL")
def _():
    files = {
        "index.html": '<html><body><img src="images/nope.png"></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    e = rep.errors[0]
    assert "缺失" in e.reason and e.url == "images/nope.png" and e.file == "index.html"


@case("外部资源（img/link/css url）禁止，外部超链接保留")
def _():
    files = {
        "index.html": (
            '<html><head><link rel="stylesheet" href="https://cdn.x.com/a.css"></head>'
            '<body><img src="//cdn.x.com/a.png"><a href="https://x.com">ok</a></body></html>'
        ),
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    reasons = " ".join(i.reason for i in rep.errors)
    assert "外部资源" in reasons
    assert sum(1 for i in rep.errors if "外部资源" in i.reason) == 2
    files2 = {
        "index.html": '<html><body><a href="https://x.com/a?b=1">ok</a>'
                      '<a href="mailto:a@b.c">mail</a></body></html>',
    }
    rep2 = analyze(make_zip(files2))
    assert rep2.ok, [i.to_dict() for i in rep2.errors]


@case("本地 URL 带查询串拒绝")
def _():
    files = {
        "index.html": '<html><body><a href="page2.html?x=1">q</a></body></html>',
        "page2.html": "<html><body>p2</body></html>",
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert any("查询串" in i.reason and i.url == "page2.html?x=1" for i in rep.errors)


@case("失效锚点报错，有效锚点放行")
def _():
    files = {
        "index.html": '<html><body><a href="page2.html#sec2">go</a></body></html>',
        "page2.html": '<html><body><h2 id="sec1">a</h2></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert any("失效锚点" in i.reason and i.url == "page2.html#sec2" for i in rep.errors)
    files["page2.html"] = '<html><body><h2 id="sec2">a</h2></body></html>'
    rep2 = analyze(make_zip(files))
    assert rep2.ok, [i.to_dict() for i in rep2.errors]


@case("路径越界（.. 逃出站点根）报错")
def _():
    files = {
        "index.html": '<html><body><a href="sub/p.html">go</a></body></html>',
        "sub/p.html": '<html><body><a href="../../etc/x.html">esc</a></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert any("越界" in i.reason for i in rep.errors)


@case("百分号解码一次：空格文件名与 %25 字面量")
def _():
    files = {
        "index.html": (
            '<html><body><img src="images/my%20pic.png">'
            '<img src="images/a%2520b.png"></body></html>'
        ),
        "images/my pic.png": PNG,
        "images/a%20b.png": PNG,  # %2520 解码一次得到 a%20b.png
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    idx = out["index.html"].decode()
    assert 'src="images/my%20pic.png"' in idx
    assert 'src="images/a%2520b.png"' in idx


@case("归一化重名报错")
def _():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", "<html><body>x</body></html>")
        zf.writestr("a/b.png", PNG)
        zf.writestr("a/./b.png", PNG)
    rep = analyze(buf.getvalue())
    assert not rep.ok
    assert any("归一化重名" in i.reason for i in rep.errors)


@case("缺少入口 / 非 UTF-8 / 类型不支持 / 数量超限")
def _():
    rep = analyze(make_zip({"a.html": "<html></html>"}))
    assert not rep.ok and any("index.html" in i.reason for i in rep.errors)

    rep = analyze(make_zip({"index.html": b"<html>\xff\xfe</html>"}))
    assert not rep.ok and any("UTF-8" in i.reason for i in rep.errors)

    rep = analyze(make_zip({"index.html": "<html></html>", "x.txt": "hi"}))
    assert not rep.ok and any("不支持的文件类型" in i.reason for i in rep.errors)

    many = {"index.html": "<html></html>"}
    for i in range(12):
        many[f"f{i}.css"] = "body{}"
    rep = analyze(make_zip(many))
    assert not rep.ok and any("12" in i.reason for i in rep.errors)

    many = {"index.html": "<html></html>"}
    for i in range(31):
        many[f"p{i}.png"] = PNG
    rep = analyze(make_zip(many))
    assert not rep.ok and any("30" in i.reason for i in rep.errors)


@case("资源类型不匹配（img 指向 css / link 指向图片）")
def _():
    files = {
        "index.html": (
            '<html><head><link rel="stylesheet" href="a.png"></head>'
            '<body><img src="a.css"></body></html>'
        ),
        "a.css": "body{}",
        "a.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert sum(1 for i in rep.errors if "类型不匹配" in i.reason) == 2


@case("CSS url() 外部资源与图片外链禁止")
def _():
    files = {
        "index.html": '<html><head><link rel="stylesheet" href="a.css"></head></html>',
        "a.css": "body { background: url(https://cdn.x.com/bg.png); }",
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    assert any("外部资源" in i.reason and i.url == "https://cdn.x.com/bg.png"
               for i in rep.errors)


@case("javascript: 链接禁止，超链接到本地图片放行")
def _():
    files = {
        "index.html": '<html><body><a href="javascript:alert(1)">x</a></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok and any("脚本" in i.reason for i in rep.errors)

    files = {
        "index.html": '<html><body><a href="images/big.jpg">看图</a></body></html>',
        "images/big.jpg": JPG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    assert "images/big.jpg" in out


@case("字节保持：除 URL 外原文不变（含重复相同链接）")
def _():
    # 两个完全相同的链接，应各自定位、各自重写
    files = {
        "index.html": (
            '<!DOCTYPE html>\n<html><body>\n'
            '<a href="p2.html">一</a><a href="p2.html">二</a>\n'
            '<a href=\'p2.html\'>三</a>\n'
            '</body></html>\n'
        ),
        "p2.html": "<html><body>p2</body></html>",
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    idx = out["index.html"].decode()
    assert idx.count('href="p2.html"') == 2
    assert "href='p2.html'" in idx
    # 除重写片段外逐字节一致：本例相对 URL 未变化，整体应完全相等
    assert idx == files["index.html"]


@case("深层相对重写：css 在子目录、页面跨目录")
def _():
    files = {
        "index.html": '<html><head><link rel="stylesheet" href="assets/css/main.css">'
                      '</head><body><a href="chapters/c1/p1.html">c1</a></body></html>',
        "assets/css/main.css": ".x { background: url(../../img/icon.png) }",
        "chapters/c1/p1.html": '<html><body><img src="../../img/icon.png">'
                               '<a href="../../index.html">home</a></body></html>',
        "img/icon.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    p1 = out["chapters/c1/p1.html"].decode()
    assert 'src="../../img/icon.png"' in p1
    assert 'href="../../index.html"' in p1
    css = out["assets/css/main.css"].decode()
    assert 'url("../../img/icon.png")' in css


@case("CSS 边界：@media/@font-face 内 url、引号空白、重复 url、转义")
def _():
    files = {
        "index.html": '<html><head><link rel="stylesheet" href="a.css"></head></html>',
        "a.css": (
            "@media (min-width: 1px) { .m { background: url( \"img/m.png\" ) } }\n"
            "@font-face { src: url(img/f.png); }\n"
            ".a { background: url(img/dup.png) }\n"
            ".b { background: url(img/dup.png) }\n"
            ".c { background: URL(img/upper.png) }\n"
        ),
        "img/m.png": PNG, "img/f.png": PNG, "img/dup.png": PNG, "img/upper.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    css = out["a.css"].decode()
    assert 'url("img/m.png")' in css
    assert 'url("img/f.png")' in css
    assert css.count('url("img/dup.png")') == 2, css
    assert 'url("img/upper.png")' in css


@case("HTML 边界：实体文件名、大写属性、单引号、无引号、a[name] 锚点")
def _():
    files = {
        "index.html": (
            '<HTML><BODY>\n'
            '<A HREF="p2.html">大写</A>\n'
            "<a href='p2.html'>单引号</a>\n"
            '<a href=p2.html>无引号</a>\n'
            '<img SRC="a&amp;b.png">\n'
            '<a href="p2.html#old">锚</a>\n'
            '</BODY></HTML>'
        ),
        "p2.html": '<html><body><a name="old">x</a></body></html>',
        "a&b.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    idx = out["index.html"].decode()
    assert 'HREF="p2.html"' in idx
    assert "href='p2.html'" in idx
    assert 'href=p2.html' in idx
    assert 'SRC="a%26b.png"' in idx, idx


@case("UTF-8 BOM 保留、非 ASCII 文件名百分号往返")
def _():
    files = {
        "index.html": "﻿<html><body><a href=\"页面2.html\">二</a>"
                      "<img src=\"图 片.png\"></body></html>",
        "页面2.html": "<html><body>二</body></html>",
        "图 片.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    raw = out["index.html"]
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM 保留
    idx = raw.decode("utf-8")
    assert 'href="%E9%A1%B5%E9%9D%A22.html"' in idx, idx
    assert 'src="%E5%9B%BE%20%E7%89%87.png"' in idx, idx


@case("空 href 与仅锚点不改写；img 空 src 报错")
def _():
    files = {
        "index.html": '<html><body><a href="">self</a><a href="#">top</a></body></html>',
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    idx = out["index.html"].decode()
    assert 'href=""' in idx and 'href="#"' in idx

    files = {"index.html": '<html><body><img src=""></body></html>'}
    rep = analyze(make_zip(files))
    assert not rep.ok


@case("link rel 大小写与多值；非 stylesheet 的 link 不处理")
def _():
    files = {
        "index.html": (
            '<html><head>'
            '<LINK REL="StyleSheet" HREF="a.css">'
            '<link rel="icon" href="favicon.png">'
            '</head></html>'
        ),
        "a.css": "body{}",
        "favicon.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok, [i.to_dict() for i in rep.errors]
    out = read_export(rep.export_bytes)
    idx = out["index.html"].decode()
    assert 'HREF="a.css"' in idx
    assert 'href="favicon.png"' in idx  # 未处理，原样保留
    # favicon 不可达（非处理引用类型），不导出
    assert "favicon.png" not in out


@case("data: URL 与未知方案拒绝")
def _():
    files = {
        "index.html": '<html><body><img src="data:image/png;base64,AAAA"></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok
    files = {
        "index.html": '<html><body><a href="skype:someone">x</a></body></html>',
    }
    rep = analyze(make_zip(files))
    assert not rep.ok and any("方案" in i.reason for i in rep.errors)


@case("产物复检：导出包自身可通过完整校验")
def _():
    files = {
        "index.html": (
            '<html><head><base href="./"><link rel="stylesheet" href="s/a.css"></head>'
            '<body><a href="d/p.html#x">p</a><img src="i/1.png"></body></html>'
        ),
        "d/p.html": '<html><body><h1 id="x">x</h1><a href="../index.html">b</a></body></html>',
        "s/a.css": ".a{background:url(../i/1.png)}",
        "i/1.png": PNG,
    }
    rep = analyze(make_zip(files))
    assert rep.ok and rep.rescan_ok is True, [i.to_dict() for i in rep.errors]
    # 对产物再次完整分析，应零错误零提示（无不可达文件）
    rep2 = analyze(rep.export_bytes)
    assert rep2.ok, [i.to_dict() for i in rep2.errors]
    assert not rep2.infos, [i.to_dict() for i in rep2.infos]


def main():
    for name, fn in cases:
        try:
            fn()
            PASSED.append(name)
            print(f"  PASS  {name}")
        except AssertionError as ex:
            FAILED.append((name, ex))
            print(f"  FAIL  {name}: {ex!r}")
        except Exception as ex:  # noqa: BLE001
            FAILED.append((name, ex))
            print(f"  ERROR {name}: {ex!r}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
