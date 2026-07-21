"""간호사 근무표 자동 생성 웹 서버 (FastAPI)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import ScheduleRequest, ScheduleResponse
from .scheduler import solve

app = FastAPI(
    title="간호사 근무표 자동 생성",
    description="OR-Tools CP-SAT 기반 3교대 듀티표 생성 API",
    version="0.1.0",
)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/schedule", response_model=ScheduleResponse)
def create_schedule(req: ScheduleRequest) -> ScheduleResponse:
    """근무표를 생성해 반환한다."""
    return solve(req)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
