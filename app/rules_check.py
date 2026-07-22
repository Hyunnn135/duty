"""근무표 대원칙/하드 규칙 위반 검사기.

용도: 파트장이 수동으로 수정한 근무표(그리드)를 검사해 위반을 리포트한다
(PLAN 0.2 워크플로우의 '경고' 단계). 자동 생성 결과는 솔버가 하드 제약으로
보장하므로 통과가 정상이다. '대안 3안 제시'는 후속(Phase 3/4).

입력 그리드 값: D/M/E/N/O 또는 비근무 코드(HY 등 — 휴무로 취급).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .config import (
    HARD_FORBIDDEN_GAP_PATTERNS,
    HARD_FORBIDDEN_TRANSITIONS,
    MAX_CONSECUTIVE_DAYS,
    MAX_CONSECUTIVE_NIGHTS,
)
from .models import Shift

WORK_VALUES = {s.value for s in Shift if s != Shift.OFF}


class Violation(BaseModel):
    """위반 1건."""

    principle: str  # 예: "P1", "P3", "T8"
    rule: str       # 사람이 읽는 규칙 이름
    nurse: str      # 간호사 이름(또는 id)
    day: int        # 1부터 시작하는 날짜
    detail: str


class ValidateRequest(BaseModel):
    """검사 요청: 간호사별 근무 나열."""

    schedules: dict[str, list[str]] = Field(
        ..., description="간호사 이름/id → 날짜별 근무 (D/M/E/N/O 또는 비근무 코드)"
    )
    max_consecutive_days: int = Field(MAX_CONSECUTIVE_DAYS, ge=1)
    max_consecutive_nights: int = Field(MAX_CONSECUTIVE_NIGHTS, ge=1)
    carry_over: dict[str, list[str]] = Field(default_factory=dict)


class ValidateResponse(BaseModel):
    ok: bool
    violations: list[Violation]
    message: str


def _norm(cell: str) -> str:
    """셀 값을 D/M/E/N/O 로 정규화. 비근무 코드·미지 값은 O(휴무) 취급."""
    if cell in ("M/D", "M/E"):
        return "M"  # 실제 근무는 미드 (병원 정책 — PLAN §2)
    return cell if cell in WORK_VALUES else "O"


def check(req: ValidateRequest) -> ValidateResponse:
    v: list[Violation] = []
    tr_name = {
        ("N", "D"): ("P1", "나이트 후 데이 금지"),
        ("N", "E"): ("P1", "나이트 후 이브닝 금지"),
        ("N", "M"): ("P1", "나이트 후 미드 금지"),
        ("E", "D"): ("P3", "역회전(이브닝→데이) 금지"),
    }

    for name, raw in req.schedules.items():
        seq = [_norm(c) for c in raw]
        carry = [_norm(c) for c in req.carry_over.get(name, [])]
        full = carry + seq          # 이월 포함 시퀀스
        off = len(carry)            # 이번 달 시작 인덱스

        # 전이 검사 (이월 경계 포함)
        for d in range(len(full) - 1):
            a, b = full[d], full[d + 1]
            key = (a, b)
            if key in {(p.value, n.value) for p, n in HARD_FORBIDDEN_TRANSITIONS}:
                p, rname = tr_name[key]
                day = d + 1 - off + 1  # 이번 달 기준 (0 이하면 이월 내부 — 보고 제외)
                if day >= 1:
                    v.append(Violation(principle=p, rule=rname, nurse=name,
                                       day=day, detail=f"{a}→{b}"))

        # 2일 갭 패턴 (N-OFF-D)
        for d in range(len(full) - 2):
            trio = (full[d], full[d + 1], full[d + 2])
            for a, b, c in HARD_FORBIDDEN_GAP_PATTERNS:
                if trio == (a.value, b.value, c.value):
                    day = d + 2 - off + 1
                    if day >= 1:
                        v.append(Violation(principle="P1", rule="N-OFF-D 금지",
                                           nurse=name, day=day,
                                           detail="나이트 후 오프 하나만 쉬고 데이 복귀"))

        # 연속 근무 상한
        streak = 0
        for d, c in enumerate(full):
            streak = streak + 1 if c in WORK_VALUES else 0
            if streak == req.max_consecutive_days + 1:
                day = d - off + 1
                if day >= 1:
                    v.append(Violation(principle="F", rule=f"연속 근무 ≤{req.max_consecutive_days}",
                                       nurse=name, day=day,
                                       detail=f"{streak}일째 연속 근무"))

        # 연속 나이트 상한
        nstreak = 0
        for d, c in enumerate(full):
            nstreak = nstreak + 1 if c == "N" else 0
            if nstreak == req.max_consecutive_nights + 1:
                day = d - off + 1
                if day >= 1:
                    v.append(Violation(principle="T8", rule=f"연속 나이트 ≤{req.max_consecutive_nights}",
                                       nurse=name, day=day,
                                       detail=f"{nstreak}일째 연속 나이트"))

        # T6a: 단일 나이트 (말일 시작은 다음 달로 이어질 수 있어 제외)
        n = len(full)
        for d in range(n):
            if full[d] != "N":
                continue
            prev_n = d > 0 and full[d - 1] == "N"
            next_n = d < n - 1 and full[d + 1] == "N"
            if d == n - 1:
                continue
            if not (prev_n or next_n):
                day = d - off + 1
                if day >= 1:
                    v.append(Violation(principle="T6a", rule="단일 나이트 금지(블록 ≥2)",
                                       nurse=name, day=day, detail="고립된 나이트 1개"))

    ok = len(v) == 0
    msg = "대원칙·하드 규칙 위반 없음" if ok else (
        f"위반 {len(v)}건 — 수정하거나, 위배를 감수하고 진행할 수 있습니다 "
        "(PLAN 0.2 워크플로우)"
    )
    return ValidateResponse(ok=ok, violations=v, message=msg)
