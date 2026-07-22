"""간호사 근무표 자동 생성 웹 서버 (FastAPI).

권한(RBAC):
  - POST /api/schedule  : admin(파트장)·master만 — 근무표 생성
  - POST /api/validate  : 로그인 사용자 — 수동 수정 대원칙 검사
  - /api/auth/*         : 회원가입·로그인·역할 관리 (auth.py)
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import UserInfo, get_current_user, require_roles, router as auth_router
from .models import ScheduleRequest, ScheduleResponse
from .rules_check import ValidateRequest, ValidateResponse, check
from .scheduler import solve

app = FastAPI(
    title="간호사 근무표 자동 생성",
    description="OR-Tools CP-SAT 기반 3교대(+미드) 듀티표 생성 API",
    version="0.2.0",
)

STATIC_DIR = Path(__file__).parent / "static"

app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/schedule", response_model=ScheduleResponse)
def create_schedule(
    req: ScheduleRequest,
    _user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> ScheduleResponse:
    """근무표를 생성해 반환한다 (관리자·마스터 전용)."""
    return solve(req)


@app.post("/api/validate", response_model=ValidateResponse)
def validate_schedule(
    req: ValidateRequest,
    _user: Annotated[UserInfo, Depends(get_current_user)],
) -> ValidateResponse:
    """수동 수정된 근무표의 대원칙/하드 규칙 위반을 검사한다 (PLAN 0.2 경고 단계)."""
    return check(req)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
