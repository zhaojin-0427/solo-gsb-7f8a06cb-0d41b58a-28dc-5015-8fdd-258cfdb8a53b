"""FastAPI 入口：上传分析、查询报告、下载离线包。

分析结果保存在进程内（带 TTL 的内存表），不落盘。
"""
from __future__ import annotations

import io
import secrets
import threading
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import engine
from .models import AnalyzeResult
from .report import serialize
from .ziputil import MAX_COMPRESSED

JOB_TTL_SECONDS = 30 * 60
MAX_JOBS = 50

app = FastAPI(title="静态教程离线封包工具", version="1.0")

STATIC_DIR = Path(__file__).parent / "static"


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, tuple[float, AnalyzeResult]] = {}
        self._janitor_started = False

    def put(self, result: AnalyzeResult) -> str:
        with self._lock:
            self._gc_locked()
            if len(self._jobs) >= MAX_JOBS:
                # 淘汰最旧的一条
                oldest = min(self._jobs, key=lambda k: self._jobs[k][0])
                del self._jobs[oldest]
            job_id = secrets.token_urlsafe(12)
            self._jobs[job_id] = (time.time(), result)
            return job_id

    def get(self, job_id: str) -> AnalyzeResult | None:
        with self._lock:
            item = self._jobs.get(job_id)
            if item is None:
                return None
            _, result = item
            self._jobs[job_id] = (time.time(), result)
            return result

    def _gc_locked(self) -> None:
        now = time.time()
        dead = [k for k, (ts, _) in self._jobs.items()
                if now - ts > JOB_TTL_SECONDS]
        for k in dead:
            del self._jobs[k]


store = _Store()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/app.js")
def app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)) -> JSONResponse:
    payload = await file.read()
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 文件")
    if len(payload) > MAX_COMPRESSED:
        raise HTTPException(
            status_code=413,
            detail=f"压缩包超过 10 MB 限制（实际 {len(payload)} 字节）")
    if not payload:
        raise HTTPException(status_code=400, detail="上传内容为空")
    result = engine.analyze(payload)
    job_id = store.put(result)
    return JSONResponse(serialize(result, job_id))


@app.get("/api/report/{job_id}")
def get_report(job_id: str) -> JSONResponse:
    result = store.get(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return JSONResponse(serialize(result, job_id))


@app.get("/api/download/{job_id}")
def download(job_id: str):
    result = store.get(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if result.problems or not result.product:
        raise HTTPException(
            status_code=409,
            detail="该包存在阻塞问题或复检未通过，禁止下载")

    buf = io.BytesIO()
    # 固定入口排第一；其余按路径排序，保证产物可复现
    names = sorted(result.product, key=lambda n: (n != "index.html", n))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.flag_bits |= 0x800  # UTF-8 文件名
            zf.writestr(zi, result.product[name])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="tutorial-offline.zip"'})
