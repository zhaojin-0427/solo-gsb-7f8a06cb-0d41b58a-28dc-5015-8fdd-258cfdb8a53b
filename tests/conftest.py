"""测试辅助：在内存中构造 ZIP。"""
from __future__ import annotations

import io
import struct
import zlib

import pytest

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13) + b"IHDR"
    + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) & 0xffffffff)
    + struct.pack(">I", 6) + b"IDAT" + zlib.compress(b"\x00\xff\x00\x00")
    + struct.pack(">I", zlib.crc32(b"IDAT" + zlib.compress(b"\x00\xff\x00\x00")) & 0xffffffff)
    + struct.pack(">I", 0) + b"IEND"
    + struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)
)
JPEG_MIN = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xd9"


def build_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with __import__("zipfile").ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def has_problem(result, kind: str) -> bool:
    return any(p.kind.value == kind for p in result.problems)


def problem_kinds(result) -> set[str]:
    return {p.kind.value for p in result.problems}


@pytest.fixture
def minimal_ok():
    return build_zip({
        "index.html": (
            b'<!doctype html><html><body><h1 id="t">Hi</h1>'
            b'<a href="#t">x</a><img src="a.png"></body></html>'),
        "a.png": PNG_1X1,
    })
