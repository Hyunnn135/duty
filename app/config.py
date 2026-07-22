"""병동 기본값 · 회전 규칙 정의.

값의 출처: 실제 61병동 2026년 7·8월 근무표 실측 + 보건복지부 야간근무 가이드라인 +
간호관리학 실습 자료. ⚠️ 표시는 파트장 면담(docs/INTERVIEW_QUESTIONS.md)으로 확정 전의
추정 기본값이며, 확정되면 이 파일만 갱신하면 된다 (규칙=설정값 원칙).
"""
from __future__ import annotations

from .models import DayStaffing, Shift, ShiftStaff

# ---- 회전(전이) 규칙 ----------------------------------------------------
# 순방향 랭크: D(1) → M(2) → E(3) → N(4). 실측 전이 빈도로 하드/소프트 구분(PLAN 6.2).

# 하드 금지 전이 (전날 → 다음날). 실측 0건 — 대원칙 P1·P3.
HARD_FORBIDDEN_TRANSITIONS: list[tuple[Shift, Shift]] = [
    (Shift.NIGHT, Shift.DAY),
    (Shift.NIGHT, Shift.EVENING),
    (Shift.NIGHT, Shift.MID),
    (Shift.EVENING, Shift.DAY),
]

# 소프트 지양 전이 (실측 각 1건 — 감점 대상, Phase 2 목적함수에서 사용)
SOFT_DISCOURAGED_TRANSITIONS: list[tuple[Shift, Shift]] = [
    (Shift.MID, Shift.DAY),
    (Shift.EVENING, Shift.MID),
]

# 2일 갭 하드 금지 패턴: N-OFF-D (대원칙 P1, 실측 0건)
HARD_FORBIDDEN_GAP_PATTERNS: list[tuple[Shift, Shift, Shift]] = [
    (Shift.NIGHT, Shift.OFF, Shift.DAY),
]

# ---- 과로 방지 상한 -----------------------------------------------------
MAX_CONSECUTIVE_DAYS = 5    # 실측 최대 5 (대원칙 승격 후보 F)
MAX_CONSECUTIVE_NIGHTS = 3  # 실측 + 복지부 가이드라인 일치 (확정)

# ---- 61병동 인원 기준 (⚠️ 면담 B1·B5 확정 전 실측 기반 추정) -------------
# 실측(8월 하단 집계): 평일 대략 D5/E5/N4, 주말 D4/E4/N4, 미드는 간헐(0~1).
DEFAULT_STAFFING: dict[str, DayStaffing] = {
    "weekday": DayStaffing(
        D=ShiftStaff(min=4, target=5),
        E=ShiftStaff(min=4, target=5),
        N=ShiftStaff(min=3, target=4),
        M=ShiftStaff(min=0),
    ),
    "weekend": DayStaffing(
        D=ShiftStaff(min=3, target=4),
        E=ShiftStaff(min=3, target=4),
        N=ShiftStaff(min=3, target=4),
        M=ShiftStaff(min=0),
    ),
    "holiday": DayStaffing(
        D=ShiftStaff(min=3, target=4),
        E=ShiftStaff(min=3, target=4),
        N=ShiftStaff(min=3, target=4),
        M=ShiftStaff(min=0),
    ),
}

# 팀당 각 교대(D/E/N) 최소 인원 (실측: 61일×3교대×3팀 중 미커버 1건 → 사실상 하드)
TEAM_MIN_PER_SHIFT = 1  # Phase 2에서 하드 제약으로 연결

# ---- 나이트 정책 (⚠️ 면담 D1·D2 확정 전) --------------------------------
MAX_NIGHTS_PER_MONTH = 7        # 실습 자료 기준, 실측 최대 7과 부합
NIGHT_KEEPER_MAX_PER_MONTH = 15  # 야간전담 월 상한 (복지부 2023 개정 기준)
