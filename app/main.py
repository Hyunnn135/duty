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

from .alternatives import AlternativesRequest, AlternativesResponse, generate
from .auth import UserInfo, get_current_user, require_roles, router as auth_router
from .models import CandidatesResponse, ScheduleRequest, ScheduleResponse
from .rules_check import ValidateRequest, ValidateResponse, check
from .scheduler import solve, solve_candidates
from .storage import router as storage_router

app = FastAPI(
    title="간호사 근무표 자동 생성",
    description="OR-Tools CP-SAT 기반 3교대(+미드) 듀티표 생성 API",
    version="0.2.0",
)

STATIC_DIR = Path(__file__).parent / "static"

app.include_router(auth_router)
app.include_router(storage_router)


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


@app.post("/api/schedule/candidates", response_model=CandidatesResponse)
def create_candidates(
    req: ScheduleRequest,
    _user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
    count: int = 3,
) -> CandidatesResponse:
    """동일 최적 품질의 서로 다른 근무표 후보 여러 개를 생성한다 (관리자·마스터).

    '단일 유일해' 수렴은 이 규모에서 계산적으로 비현실적이므로(EXPERIMENT_REPORT §3),
    품질이 동일한 후보들을 제시해 파트장이 선택하도록 한다(권장안).
    """
    count = max(1, min(count, 5))
    cands = solve_candidates(req, count=count)
    feasible = bool(cands and cands[0].feasible)
    real = [c for c in cands if c.feasible]
    msg = (f"{len(real)}개의 동일 품질 후보를 생성했습니다." if feasible
           else (cands[0].message if cands else "생성 실패"))
    return CandidatesResponse(feasible=feasible, count=len(real),
                              candidates=real if feasible else cands, message=msg)


@app.post("/api/validate", response_model=ValidateResponse)
def validate_schedule(
    req: ValidateRequest,
    _user: Annotated[UserInfo, Depends(get_current_user)],
) -> ValidateResponse:
    """수동 수정된 근무표의 대원칙/하드 규칙 위반을 검사한다 (PLAN 0.2 경고 단계)."""
    return check(req)


@app.post("/api/alternatives", response_model=AlternativesResponse)
def schedule_alternatives(
    req: AlternativesRequest,
    _user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> AlternativesResponse:
    """대원칙 위반을 최소 변경으로 해소하는 대안 3안을 생성한다 (PLAN 0.2, 관리자·마스터)."""
    return generate(req)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
