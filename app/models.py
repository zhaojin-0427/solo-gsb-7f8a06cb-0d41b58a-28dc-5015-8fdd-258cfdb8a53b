"""封包分析过程中的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# 问题（任一问题存在都禁止下载产物）
# ---------------------------------------------------------------------------
class ProblemKind(str, Enum):
    # ZIP / 包结构
    ZIP_BROKEN = "zip_broken"                 # 压缩包损坏或非 ZIP
    TOO_MANY_FILES = "too_many_files"         # 文件数量超限
    UNSUPPORTED_FILE = "unsupported_file"     # 不允许的文件类型
    BAD_UTF8 = "bad_utf8"                     # HTML/CSS 非 UTF-8
    BAD_IMAGE = "bad_image"                   # 图片不是合法 PNG/JPEG
    MISSING_INDEX = "missing_index"           # 入口 index.html 不存在
    DUPLICATE_NAME = "duplicate_name"         # 归一化重名
    UNSAFE_ENTRY = "unsafe_entry"             # 越界/非法 ZIP 条目路径
    # 禁止构造
    FORBIDDEN_SCRIPT = "forbidden_script"
    FORBIDDEN_FORM = "forbidden_form"
    FORBIDDEN_INLINE_STYLE = "forbidden_inline_style"
    FORBIDDEN_SRCSET = "forbidden_srcset"
    FORBIDDEN_CSS_IMPORT = "forbidden_css_import"
    FORBIDDEN_URL_ATTR = "forbidden_url_attr"
    # 引用问题
    MISSING_FILE = "missing_file"             # 本地引用的文件不存在
    PATH_TRAVERSAL = "path_traversal"         # 越界路径（跳出 ZIP 根）
    BAD_TARGET_TYPE = "bad_target_type"       # 引用关系与文件类型不符
    LOCAL_WITH_QUERY = "local_with_query"     # 带查询串的本地 URL
    UNSUPPORTED_SCHEME = "unsupported_scheme"  # data:/javascript: 等
    EXTERNAL_RESOURCE = "external_resource"   # 外部资源（非普通超链接）
    BAD_ANCHOR = "bad_anchor"                 # 失效锚点
    # 字节定位问题
    LOCATE_AMBIGUOUS = "locate_ambiguous"     # 引用无法唯一定位到原文字节
    # 复检失败（理论上不会发生）
    RESCAN_FAIL = "rescan_fail"


# 不阻塞导出，但要展示给管理员核对
class NoticeKind(str, Enum):
    EXTERNAL_LINK = "external_link"   # 外部普通超链接（保留）
    UNREACHABLE = "unreachable_file"  # 从入口不可达（不会导出）


@dataclass
class Problem:
    kind: ProblemKind
    message: str
    source: str | None = None          # 所在 ZIP 内文件路径
    line: int | None = None            # 1 基行号（HTML/CSS 文本）
    url: str | None = None             # 原始 URL（原文照抄）


@dataclass
class Notice:
    kind: NoticeKind
    message: str
    source: str | None = None
    line: int | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# 引用（HTML 属性 / CSS url() 统一描述）
# ---------------------------------------------------------------------------
class RefRole(str, Enum):
    A = "a_href"
    IMG = "img_src"
    STYLESHEET = "link_stylesheet_href"
    CSS_URL = "css_url"
    BASE = "base_href"


@dataclass
class Ref:
    role: RefRole
    source: str                 # 所在 HTML/CSS 的 ZIP 路径
    raw_url: str                # 原文中的 URL 文本
    line: int                   # 1 基行号
    # span：在所在文件【解码后文本】中的半开区间；HTML 为属性值内部，
    # CSS 为整个 url(...) 词元
    start: int
    end: int
    style_offset: int = 0       # <style> 内容相对整份 HTML 文本的偏移
    # 解析结果
    external: bool = False
    resolved_path: str | None = None   # 归一化后的 ZIP 内键
    fragment: str | None = None        # 原文片段（含 # 前缀的锚文本，逐字保留）

    def css_text_span(self) -> tuple[int, int]:
        """换成所在文件文本坐标（仅 CSS/role 使用 style_offset）。"""
        if self.role is RefRole.CSS_URL and self.style_offset:
            return self.style_offset + self.start, self.style_offset + self.end
        return self.start, self.end


@dataclass
class FileEntry:
    """通过校验的 ZIP 条目。"""
    path: str           # ZIP 内路径（zipfile 解码后）
    data: bytes
    kind: str           # "html" | "css" | "png" | "jpeg"


@dataclass
class AnalyzeResult:
    files: dict[str, FileEntry] = field(default_factory=dict)
    html_meta: dict[str, "HtmlMeta"] = field(default_factory=dict)
    css_meta: dict[str, "CssMeta"] = field(default_factory=dict)
    refs: list[Ref] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    # 仅通过校验后填充
    reachable: set[str] = field(default_factory=set)
    product: dict[str, bytes] = field(default_factory=dict)


@dataclass
class HtmlMeta:
    anchors: set[str] = field(default_factory=set)      # id / a[name] 解码值
    style_blocks: list[tuple[int, int]] = field(default_factory=list)  # 文本坐标


@dataclass
class CssMeta:
    pass
