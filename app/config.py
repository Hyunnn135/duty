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
MAX_CONSECUTIVE_DAYS = 5    # 대원칙 확정 (면담 C1: "5일은 절대 원칙. 이후 불가능")
MAX_CONSECUTIVE_NIGHTS = 3  # 대원칙 확정 (면담 C2 + 복지부 가이드라인 일치)

# ---- 61병동 인원 기준 (면담 B1·B5 확정) ---------------------------------
# 확정: 각 교대 D/E/N = 4명 = 팀당 1명(3팀) + 액팅 1명. 4/4/4는 '절대 최소'(B5).
# 평일에는 여유 인원으로 D/E가 5까지 갈 수 있어 target=5(소프트)로 둔다(실측 8월).
# 공휴일은 평일과 구분하지 않는다(면담 E2) → holiday = weekday.
# 트레이닝 신규는 이 정원에서 제외된다(면담 F2, solver에서 처리).
DEFAULT_STAFFING: dict[str, DayStaffing] = {
    "weekday": DayStaffing(
        D=ShiftStaff(min=4, target=5),
        E=ShiftStaff(min=4, target=5),
        N=ShiftStaff(min=4, target=4),
        M=ShiftStaff(min=0),
    ),
    "weekend": DayStaffing(
        D=ShiftStaff(min=4, target=4),
        E=ShiftStaff(min=4, target=4),
        N=ShiftStaff(min=4, target=4),
        M=ShiftStaff(min=0),
    ),
    # 공휴일 = 평일 취급 (면담 E2). 오프 카운트(E1)에는 공휴일도 오프 quota로 반영.
    "holiday": DayStaffing(
        D=ShiftStaff(min=4, target=5),
        E=ShiftStaff(min=4, target=5),
        N=ShiftStaff(min=4, target=4),
        M=ShiftStaff(min=0),
    ),
}

# 팀당 각 교대(D/E/N) 최소 인원. 확정: 각 팀 한 명씩 배치(면담 B1) → 하드.
TEAM_MIN_PER_SHIFT = 1

# ---- 나이트 정책 (면담 D1·D2·D4 확정) ----------------------------------
MAX_NIGHTS_PER_MONTH = 7        # 확정 (면담 D1: "최대 7개, 2개월 합 상한 없음")
NIGHT_KEEPER_MAX_PER_MONTH = 15  # 야간전담 월 상한. 확정: 61병동엔 야간전담 없음(면담 D2)
