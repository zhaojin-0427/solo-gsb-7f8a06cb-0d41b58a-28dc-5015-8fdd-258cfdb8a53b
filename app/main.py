"""教程离线封包工具 — FastAPI 入口。"""
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from analyzer import analyze

app = FastAPI(title="教程离线封包工具")

# 分析会话：token -> Report（内存保存，限制数量防膨胀）
SESSIONS: dict[str, object] = {}
MAX_SESSIONS = 50


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    data = await file.read()
    report = analyze(data)
    token = uuid.uuid4().hex
    if len(SESSIONS) >= MAX_SESSIONS:
        SESSIONS.pop(next(iter(SESSIONS)))
    SESSIONS[token] = report
    payload = report.to_dict()
    payload["token"] = token
    return payload


@app.get("/api/export/{token}")
def api_export(token: str):
    report = SESSIONS.get(token)
    if report is None:
        raise HTTPException(404, "分析会话不存在或已过期，请重新上传")
    if report.export_bytes is None:
        raise HTTPException(409, "存在阻断问题，禁止下载")
    return Response(
        content=report.export_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=offline-tutorial.zip"},
    )


static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
