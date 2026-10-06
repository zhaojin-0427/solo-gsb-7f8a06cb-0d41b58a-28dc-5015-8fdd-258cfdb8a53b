"""分析结果 -> 前端 JSON 结构。"""
from __future__ import annotations

from .models import AnalyzeResult

KIND_LABELS_ZH = {
    "zip_broken": "压缩包损坏",
    "too_many_files": "数量/体积超限",
    "unsupported_file": "不支持的文件",
    "bad_utf8": "编码错误",
    "bad_image": "图片损坏",
    "missing_index": "缺少入口",
    "duplicate_name": "归一化重名",
    "unsafe_entry": "非法路径",
    "forbidden_script": "禁止的脚本",
    "forbidden_form": "禁止的表单",
    "forbidden_inline_style": "禁止内联样式",
    "forbidden_srcset": "禁止 srcset",
    "forbidden_css_import": "禁止 CSS @import",
    "forbidden_url_attr": "不允许的 URL 属性",
    "missing_file": "缺失文件",
    "path_traversal": "越界路径",
    "bad_target_type": "引用类型不符",
    "local_with_query": "本地 URL 带查询串",
    "unsupported_scheme": "不允许的协议",
    "external_resource": "外部资源",
    "bad_anchor": "失效锚点",
    "locate_ambiguous": "引用无法唯一定位",
    "rescan_fail": "产物复检失败",
    "external_link": "外部超链接（保留）",
    "unreachable_file": "不可达文件",
}


def serialize(result: AnalyzeResult, job_id: str) -> dict:
    problems = [{
        "kind": p.kind.value if hasattr(p.kind, "value") else str(p.kind),
        "kindLabel": KIND_LABELS_ZH.get(
            p.kind.value if hasattr(p.kind, "value") else str(p.kind),
            str(p.kind)),
        "message": p.message,
        "source": p.source,
        "line": p.line,
        "url": p.url,
    } for p in result.problems]
    notices = [{
        "kind": n.kind.value,
        "kindLabel": KIND_LABELS_ZH.get(n.kind.value, n.kind.value),
        "message": n.message,
        "source": n.source,
        "line": n.line,
        "url": n.url,
    } for n in result.notices]

    files = []
    for path in sorted(result.files):
        entry = result.files[path]
        files.append({
            "path": path,
            "kind": entry.kind,
            "size": len(entry.data),
            "reachable": path in result.reachable,
        })

    refs = [{
        "role": r.role.value,
        "source": r.source,
        "line": r.line,
        "url": r.raw_url,
        "target": r.resolved_path,
        "external": r.external,
    } for r in result.refs]

    blocked = bool(result.problems)
    return {
        "jobId": job_id,
        "blocked": blocked,
        "summary": {
            "totalFiles": len(result.files),
            "reachableFiles": len(result.reachable),
            "problemCount": len(problems),
            "noticeCount": len(notices),
            "downloadable": not blocked and bool(result.product),
        },
        "problems": problems,
        "notices": notices,
        "files": files,
        "refs": refs,
        "limits": {
            "maxCompressedMB": 10,
            "maxUncompressedMB": 30,
            "maxTextFiles": 12,
            "maxImages": 30,
        },
    }
