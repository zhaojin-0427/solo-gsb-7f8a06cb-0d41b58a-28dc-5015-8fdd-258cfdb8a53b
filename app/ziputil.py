"""ZIP 读取与包级校验。

限制：
* 压缩包 <= 10 MB（上传层先拦一次，这里再按 central directory 复核）
* 解压后总字节 <= 30 MB
* 至多 12 个 UTF-8 HTML/CSS 文件、30 张 PNG/JPEG，不接受任何其他文件
* 必须有根目录 index.html
* 非法/越界路径、目录项、归一化重名一律拒绝
"""
from __future__ import annotations

import io
import posixpath
import unicodedata
import zipfile

from .models import AnalyzeResult, FileEntry, Problem, ProblemKind

MAX_COMPRESSED = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 30 * 1024 * 1024
MAX_TEXT_FILES = 12
MAX_IMAGES = 30

HTML_EXTS = {".html", ".htm"}
CSS_EXTS = {".css"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _classify(name: str) -> str | None:
    ext = posixpath.splitext(name)[1].lower()
    if ext in HTML_EXTS:
        return "html"
    if ext in CSS_EXTS:
        return "css"
    if ext == ".png":
        return "png"
    if ext in (".jpg", ".jpeg"):
        return "jpeg"
    return None


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _image_ok(data: bytes, kind: str) -> bool:
    if kind == "png":
        return data.startswith(PNG_MAGIC) and _png_iend_ok(data)
    if kind == "jpeg":
        return data.startswith(JPEG_MAGIC) and data.rstrip().endswith(b"\xd9")
    return False


def _png_iend_ok(data: bytes) -> bool:
    # 末尾布局：长度(4) + "IEND"(4) + CRC(4)
    return data[-8:-4] == b"IEND" if len(data) >= 12 else False


def load_zip(payload: bytes) -> AnalyzeResult:
    result = AnalyzeResult()
    p = result.problems.append

    if len(payload) > MAX_COMPRESSED:
        p(Problem(ProblemKind.ZIP_BROKEN,
                  f"压缩包超过 10 MB（实际 {len(payload)} 字节）"))
        return result

    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        p(Problem(ProblemKind.ZIP_BROKEN, f"无法打开 ZIP：{exc}"))
        return result

    # 完整性 + 总解压体积
    try:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            p(Problem(ProblemKind.TOO_MANY_FILES,
                      f"解压后体积超过 30 MB（实际 {total} 字节）"))
            return result
        bad = zf.testzip()
        if bad is not None:
            p(Problem(ProblemKind.ZIP_BROKEN, f"压缩数据损坏：{bad}"))
            return result
    except (zipfile.BadZipFile, OSError) as exc:
        p(Problem(ProblemKind.ZIP_BROKEN, f"读取 ZIP 目录失败：{exc}"))
        return result

    text_count = 0
    image_count = 0
    nfc_keys: dict[str, str] = {}
    casefold_keys: dict[str, str] = {}

    for info in infos:
        raw_name = info.filename
        # ZIP 规范：未置 UTF-8 标志位（bit 11）的名字按 cp437 解码。
        # ASCII 名字在 cp437 与 UTF-8 下字节一致，可安全接受；
        # 含非 ASCII 字节又未声明 UTF-8 的名字存在编码歧义，拒绝。
        if not (info.flag_bits & 0x800):
            if not _is_ascii(raw_name):
                p(Problem(ProblemKind.UNSAFE_ENTRY,
                          "非 ASCII 条目名缺少 UTF-8 编码标志位（ZIP bit 11）",
                          source=raw_name))
                continue
        name = raw_name
        if "\\" in name or name.startswith("/") or "//" in name:
            p(Problem(ProblemKind.UNSAFE_ENTRY,
                      "非法 ZIP 条目路径（反斜杠/绝对路径/重复分隔符）",
                      source=name))
            continue
        norm = posixpath.normpath(name)
        parts = norm.split("/")
        if norm in (".", "") or any(
                seg in ("", ".", "..") for seg in parts):
            if info.is_dir():
                continue  # 目录项不计数，直接忽略
            p(Problem(ProblemKind.UNSAFE_ENTRY,
                      "非法/越界 ZIP 条目路径", source=name))
            continue
        if info.is_dir():
            continue
        if norm != name.rstrip("/"):
            # ./a、a/../b 之类规范化后变化的写法直接拒绝，避免别名
            p(Problem(ProblemKind.UNSAFE_ENTRY,
                        f"条目路径需保持规范形态（{name!r}）", source=name))
            continue

        # 归一化重名：Unicode 组合形态冲突 + 大小写冲突
        nfc = unicodedata.normalize("NFC", norm)
        if nfc in nfc_keys and nfc_keys[nfc] != norm:
            p(Problem(ProblemKind.DUPLICATE_NAME,
                      f"Unicode 归一化后与 {nfc_keys[nfc]!r} 重名",
                      source=norm))
            continue
        low = nfc.casefold()
        if low in casefold_keys and casefold_keys[low] != nfc:
            p(Problem(ProblemKind.DUPLICATE_NAME,
                      f"大小写不敏感文件系统中与 {casefold_keys[low]!r} 重名",
                      source=norm))
            continue
        nfc_keys[nfc] = norm
        casefold_keys[low] = norm

        kind = _classify(norm)
        if kind is None:
            p(Problem(ProblemKind.UNSUPPORTED_FILE,
                      "仅允许 HTML/CSS 与 PNG/JPEG 文件", source=norm))
            continue
        if kind in ("html", "css"):
            text_count += 1
            if text_count > MAX_TEXT_FILES:
                p(Problem(ProblemKind.TOO_MANY_FILES,
                          f"HTML/CSS 文件超过 {MAX_TEXT_FILES} 个",
                          source=norm))
                continue
        else:
            image_count += 1
            if image_count > MAX_IMAGES:
                p(Problem(ProblemKind.TOO_MANY_FILES,
                          f"图片超过 {MAX_IMAGES} 张", source=norm))
                continue

        try:
            data = zf.read(info)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            p(Problem(ProblemKind.ZIP_BROKEN,
                      f"读取条目失败：{exc}", source=norm))
            continue

        if kind in ("html", "css"):
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                p(Problem(ProblemKind.BAD_UTF8,
                          f"不是合法 UTF-8：{exc}", source=norm))
                continue
        else:
            if not _image_ok(data, kind):
                p(Problem(ProblemKind.BAD_IMAGE,
                          f"文件头与声明的 {kind.upper()} 类型不符",
                          source=norm))
                continue

        if norm in result.files:
            p(Problem(ProblemKind.DUPLICATE_NAME,
                      "ZIP 内存在重复条目", source=norm))
            continue
        result.files[norm] = FileEntry(path=norm, data=data, kind=kind)

    if "index.html" not in result.files:
        p(Problem(ProblemKind.MISSING_INDEX,
                  "压缩包根目录缺少入口文件 index.html"))

    zf.close()
    return result
