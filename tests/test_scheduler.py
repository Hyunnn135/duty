"""근무표 솔버 제약조건 검증 테스트."""
from __future__ import annotations

from app.models import (
    MinStaff,
    Nurse,
    RequestType,
    ScheduleRequest,
    Shift,
    ShiftRequest,
)
from app.scheduler import solve


def _nurses(n: int) -> list[Nurse]:
    return [Nurse(id=f"n{i}", name=f"간호사{i}") for i in range(n)]


def test_basic_feasible():
    req = ScheduleRequest(num_days=7, nurses=_nurses(6))
    res = solve(req)
    assert res.feasible
    # 모든 간호사가 7일치 배정을 받는다
    for sch in res.schedules:
        assert len(sch.shifts) == 7


def test_one_shift_per_day():
    req = ScheduleRequest(num_days=10, nurses=_nurses(8))
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        assert len(sch.shifts) == 10  # 하루당 정확히 하나


def test_min_staff_respected():
    ms = MinStaff(D=2, E=2, N=1)
    req = ScheduleRequest(num_days=7, nurses=_nurses(7), min_staff=ms)
    res = solve(req)
    assert res.feasible
    for d in range(7):
        day_shifts = [sch.shifts[d] for sch in res.schedules]
        assert day_shifts.count(Shift.DAY) >= 2
        assert day_shifts.count(Shift.EVENING) >= 2
        assert day_shifts.count(Shift.NIGHT) >= 1


def test_no_day_or_evening_after_night():
    req = ScheduleRequest(num_days=10, nurses=_nurses(7))
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        for d in range(len(sch.shifts) - 1):
            if sch.shifts[d] == Shift.NIGHT:
                assert sch.shifts[d + 1] not in (Shift.DAY, Shift.EVENING)


def test_no_day_after_evening():
    """대원칙 P3: E 다음날 D 직접 배정 금지 (역회전)."""
    req = ScheduleRequest(num_days=14, nurses=_nurses(8))
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        for d in range(len(sch.shifts) - 1):
            if sch.shifts[d] == Shift.EVENING:
                assert sch.shifts[d + 1] != Shift.DAY


def test_no_nod_pattern():
    """대원칙 P1: N-OFF-D 금지 (나이트 후 오프 1개만 두고 데이 복귀 금지)."""
    req = ScheduleRequest(num_days=14, nurses=_nurses(8))
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        for d in range(len(sch.shifts) - 2):
            trio = (sch.shifts[d], sch.shifts[d + 1], sch.shifts[d + 2])
            assert trio != (Shift.NIGHT, Shift.OFF, Shift.DAY)


def test_max_consecutive_days():
    req = ScheduleRequest(
        num_days=14, nurses=_nurses(8), max_consecutive_days=3
    )
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        streak = 0
        for s in sch.shifts:
            streak = streak + 1 if s != Shift.OFF else 0
            assert streak <= 3


def test_max_consecutive_nights():
    req = ScheduleRequest(
        num_days=14, nurses=_nurses(8), max_consecutive_nights=2
    )
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        streak = 0
        for s in sch.shifts:
            streak = streak + 1 if s == Shift.NIGHT else 0
            assert streak <= 2


def test_min_off_days():
    req = ScheduleRequest(num_days=14, nurses=_nurses(8), min_off_days=4)
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        assert sch.counts["O"] >= 4


def test_forbid_request_is_hard():
    req = ScheduleRequest(
        num_days=7,
        nurses=_nurses(7),
        requests=[
            ShiftRequest(
                nurse_id="n0", day=2, shift=Shift.OFF, type=RequestType.FORBID
            )
        ],
    )
    # n0는 2일차에 오프가 금지됨 -> 반드시 근무
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert n0.shifts[2] != Shift.OFF


def test_prefer_request_honored_when_possible():
    req = ScheduleRequest(
        num_days=7,
        nurses=_nurses(7),
        requests=[
            ShiftRequest(
                nurse_id="n0", day=3, shift=Shift.OFF, type=RequestType.PREFER
            )
        ],
    )
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert n0.shifts[3] == Shift.OFF
    assert res.unmet_preferences == 0


def test_infeasible_when_not_enough_nurses():
    # 하루 최소 인원 합 5 > 간호사 3명
    ms = MinStaff(D=2, E=2, N=1)
    req = ScheduleRequest(num_days=5, nurses=_nurses(3), min_staff=ms)
    res = solve(req)
    assert not res.feasible
    assert res.status == "INFEASIBLE"


# ---- Phase 1: 미드 · 사전배정 · 원티드 · 이월 · 나이트 제외 ----

def test_mid_shift_assignable_and_no_mid_after_night():
    ms = MinStaff(D=1, E=1, N=1, M=1)
    req = ScheduleRequest(num_days=7, nurses=_nurses(7), min_staff=ms)
    res = solve(req)
    assert res.feasible
    for d in range(7):  # 매일 미드 최소 1명
        assert sum(1 for s in res.schedules if s.shifts[d] == Shift.MID) >= 1
    for sch in res.schedules:  # N 다음날 M 금지 (하드 전이)
        for d in range(6):
            if sch.shifts[d] == Shift.NIGHT:
                assert sch.shifts[d + 1] != Shift.MID


def test_pre_assigned_leave_is_fixed_and_labeled():
    from app.models import PreAssigned

    req = ScheduleRequest(
        num_days=7, nurses=_nurses(7),
        pre_assigned=[PreAssigned(nurse_id="n0", day=2, code="HY")],
    )
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert n0.shifts[2] == Shift.OFF          # 내부적으로 휴무
    assert n0.labels[2] == "HY"               # 표시는 연차 코드
    assert n0.counts.get("HY", 0) == 1        # 집계는 HY로 (O와 구분)


def test_wanted_off_range_honored():
    from app.models import WantedRequest

    req = ScheduleRequest(
        num_days=10, nurses=_nurses(8),
        wanted=[WantedRequest(nurse_id="n0", start_day=3, end_day=5, shift=Shift.OFF)],
    )
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert all(n0.shifts[d] == Shift.OFF for d in (3, 4, 5))
    assert res.unmet_wanted_off == 0


def test_night_ineligible_never_night():
    nurses = _nurses(8)
    nurses[0] = Nurse(id="n0", name="신규", night_eligible=False)
    req = ScheduleRequest(num_days=10, nurses=nurses)
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert Shift.NIGHT not in n0.shifts


def test_carry_over_limits_month_start():
    # n0: 전월 말 나이트 3연속 → 1일차 나이트 불가(연속 4 방지) + D/E/M 불가(P1)
    # n1: 전월 말 E → 1일차 D 불가(P3)
    # n2: 전월 말 [N, O] → 1일차 D 불가(N-OFF-D 경계)
    req = ScheduleRequest(
        num_days=7, nurses=_nurses(8),
        carry_over={
            "n0": ["N", "N", "N"],
            "n1": ["D", "E"],
            "n2": ["N", "O"],
        },
    )
    res = solve(req)
    assert res.feasible
    by = {s.nurse_id: s for s in res.schedules}
    assert by["n0"].shifts[0] in (Shift.OFF,)          # N 뒤라 D/E/M 금지 + 연속N 초과라 N도 금지
    assert by["n1"].shifts[0] != Shift.DAY
    assert by["n2"].shifts[0] != Shift.DAY


# ---- Phase 2: 팀별 인원 · 나이트 블록 · 월 상한 · 프리셉터 · 미드=저연차 ----

def test_team_min_staff_per_shift():
    nurses = [
        Nurse(id=f"n{i}", name=f"간호사{i}", team=(i % 3) + 1) for i in range(12)
    ]
    req = ScheduleRequest(num_days=7, nurses=nurses, team_min_staff=1)
    res = solve(req)
    assert res.feasible
    by_team = {1: [], 2: [], 3: []}
    for i, sch in enumerate(res.schedules):
        by_team[nurses[i].team].append(sch)
    for d in range(7):
        for t, members in by_team.items():
            for s in (Shift.DAY, Shift.EVENING, Shift.NIGHT):
                assert sum(1 for m in members if m.shifts[d] == s) >= 1, (
                    f"{d+1}일 {s.value}에 {t}팀 없음"
                )


def test_no_isolated_single_night():
    """T6a: 나이트 블록 ≥ 2 (말일 시작 블록 제외)."""
    req = ScheduleRequest(num_days=14, nurses=_nurses(8))
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        for d in range(14):
            if sch.shifts[d] != Shift.NIGHT:
                continue
            prev_n = d > 0 and sch.shifts[d - 1] == Shift.NIGHT
            next_n = d < 13 and sch.shifts[d + 1] == Shift.NIGHT
            if d == 13:  # 말일은 다음 달로 이어질 수 있음
                continue
            assert prev_n or next_n, f"{sch.name} {d+1}일 단일 나이트"


def test_max_nights_per_month():
    req = ScheduleRequest(num_days=14, nurses=_nurses(8), max_nights_per_month=3)
    res = solve(req)
    assert res.feasible
    for sch in res.schedules:
        assert sch.counts["N"] <= 3


def test_preceptor_pairing_preferred():
    nurses = _nurses(8)
    nurses[7] = Nurse(id="n7", name="신규", is_new=True, night_eligible=False,
                      preceptor_id="n0")
    req = ScheduleRequest(num_days=7, nurses=nurses)
    res = solve(req)
    assert res.feasible
    by = {s.nurse_id: s for s in res.schedules}
    together = sum(
        1 for d in range(7)
        if by["n7"].shifts[d] != Shift.OFF
        and by["n7"].shifts[d] == by["n0"].shifts[d]
    )
    workdays = sum(1 for d in range(7) if by["n7"].shifts[d] != Shift.OFF)
    assert workdays == 0 or together / workdays >= 0.5  # 과반 동행


def test_mid_avoids_senior_ranks():
    ms = MinStaff(D=1, E=1, N=1, M=1)
    nurses = [
        Nurse(id=f"n{i}", name=f"간호사{i}", seniority_rank=i + 1) for i in range(8)
    ]
    req = ScheduleRequest(num_days=7, nurses=nurses, min_staff=ms)
    res = solve(req)
    assert res.feasible
    senior_mids = sum(
        res.schedules[i].counts["M"] for i in range(3)  # 랭크 1~3
    )
    assert senior_mids == 0  # 하위권으로 충분히 채울 수 있으면 상위권 미드 없음


def test_carry_over_work_streak():
    # 전월 말 5일 연속 근무 → 1일차는 반드시 휴무 (연속 6일 방지)
    req = ScheduleRequest(
        num_days=7, nurses=_nurses(8), max_consecutive_days=5,
        carry_over={"n0": ["D", "D", "E", "E", "E"]},
    )
    res = solve(req)
    assert res.feasible
    n0 = next(s for s in res.schedules if s.nurse_id == "n0")
    assert n0.shifts[0] == Shift.OFF


def test_trainee_excluded_from_staffing_and_paired():
    """F2: 트레이닝 신규는 정원 제외 + 교육자와 항상 같은 근무."""
    # 정원 D2/E2/N1 = 하루 5명 필요. 트레이너 포함 인원은 6명이지만
    # 트레이너(n0)와 트레이니(n5)는 한 몸처럼 움직이고 트레이니는 정원 미포함.
    ms = MinStaff(D=2, E=2, N=1)
    nurses = _nurses(9)
    nurses[8] = Nurse(id="n8", name="신규", is_trainee=True, trainer_id="n0")
    req = ScheduleRequest(num_days=7, nurses=nurses, min_staff=ms)
    res = solve(req)
    assert res.feasible
    by = {s.nurse_id: s for s in res.schedules}
    # 트레이니는 매일 교육자와 동일 근무
    for d in range(7):
        assert by["n8"].shifts[d] == by["n0"].shifts[d]
    # 정원 계산에서 트레이니 제외: 각 근무일 정원은 나머지 인원으로 충족
    for d in range(7):
        working = [s for s in res.schedules if s.nurse_id != "n8"]
        day = [w.shifts[d] for w in working]
        assert day.count(Shift.DAY) >= 2
        assert day.count(Shift.EVENING) >= 2
        assert day.count(Shift.NIGHT) >= 1


def test_trainee_requires_trainer():
    import pytest
    with pytest.raises(ValueError):
        ScheduleRequest(
            num_days=7,
            nurses=[Nurse(id="a", name="신규", is_trainee=True)],
        )


def test_team_wanted_off_no_overlap():
    """E4: 같은 팀 원티드 오프는 같은 날 겹치지 않는다."""
    from app.models import WantedRequest
    nurses = [Nurse(id=f"n{i}", name=f"간호사{i}", team=1) for i in range(4)]
    nurses += [Nurse(id=f"m{i}", name=f"타팀{i}", team=2) for i in range(4)]
    # 팀1의 n0, n1이 같은 날(3일) 오프 신청 → 하나만 승인 가능
    req = ScheduleRequest(
        num_days=7, nurses=nurses, min_staff=MinStaff(D=1, E=1, N=1),
        exclusive_team_wanted_off=True,  # 하드 E4 옵트인 (기본은 소프트로 변경됨)
        wanted=[
            WantedRequest(nurse_id="n0", start_day=3, shift=Shift.OFF),
            WantedRequest(nurse_id="n1", start_day=3, shift=Shift.OFF),
        ],
    )
    res = solve(req)
    assert res.feasible
    by = {s.nurse_id: s for s in res.schedules}
    offs = (by["n0"].shifts[3] == Shift.OFF) + (by["n1"].shifts[3] == Shift.OFF)
    assert offs <= 1  # 팀 내 겹침 금지


def test_off_count_target_soft():
    """E1: year/month 지정 시 오프 수가 (주말+공휴일) 목표에 근접."""
    nurses = [Nurse(id=f"n{i}", name=f"간호사{i}") for i in range(10)]
    req = ScheduleRequest(
        year=2026, month=2, nurses=nurses, min_staff=MinStaff(D=2, E=2, N=1),
    )
    target = req.default_off_target()
    assert target is not None and target >= 8  # 2026-02 주말 8일
    res = solve(req)
    assert res.feasible
    # 대부분 간호사의 오프 수가 목표에서 크게 벗어나지 않는다
    for s in res.schedules:
        assert abs(s.counts["O"] - target) <= 2


def test_solve_candidates_distinct_equal_quality():
    """품질 수렴 집합: 동일 목적값의 서로 다른 후보 여러 개."""
    from app.scheduler import solve_candidates
    nurses = _nurses(9)
    req = ScheduleRequest(num_days=7, nurses=nurses, min_staff=MinStaff(D=2, E=2, N=1))
    cands = solve_candidates(req, count=3)
    assert len(cands) >= 2 and all(c.feasible for c in cands)
    grids = [tuple(tuple(s.shifts) for s in c.schedules) for c in cands]
    assert len(set(grids)) == len(grids)          # 서로 다른 배치
    assert len({c.objective_value for c in cands}) == 1  # 동일 품질(목적값)


def test_exact_mode_night_band_fairness():
    """exact_mode: 나이트 공정성을 하드 밴드로 → 밴드 폭 1, 편차 목적 없이도 균등."""
    nurses = _nurses(12)
    req = ScheduleRequest(num_days=14, nurses=nurses,
                          min_staff=MinStaff(D=2, E=2, N=2),
                          exact_mode=True, time_limit_seconds=15)
    res = solve(req)
    assert res.feasible
    nights = [s.counts.get("N", 0) for s in res.schedules]
    assert max(nights) - min(nights) <= 1   # 자동 밴드 [2,3] → 폭 1


def test_daily_patterns_exact_counts():
    """daily_patterns: 매일 인원이 허용 패턴과 '정확' 일치(초과·미달 불가)."""
    pats = [{"D": 2, "E": 2, "N": 2, "M": 1}, {"D": 3, "E": 3, "N": 2, "M": 0}]
    req = ScheduleRequest(num_days=10, nurses=_nurses(10), daily_patterns=pats,
                          time_limit_seconds=15)
    res = solve(req)
    assert res.feasible
    for d in range(10):
        cnt = {"D": 0, "E": 0, "N": 0, "M": 0}
        for s in res.schedules:
            v = s.shifts[d].value
            if v in cnt:
                cnt[v] += 1
        tup = (cnt["D"], cnt["E"], cnt["N"], cnt["M"])
        assert tup in {(2, 2, 2, 1), (3, 3, 2, 0)}, f"day{d}: {tup}"


def test_acting_days_hard_count():
    """acting_days: 액팅(M) 포함 패턴을 쓰는 날 수를 정확히 고정한다."""
    pats = [{"D": 2, "E": 2, "N": 2, "M": 1}, {"D": 3, "E": 3, "N": 2, "M": 0}]
    req = ScheduleRequest(num_days=10, nurses=_nurses(10), daily_patterns=pats,
                          acting_days=3, time_limit_seconds=15)
    res = solve(req)
    assert res.feasible
    act = 0
    for d in range(10):
        has_m = sum(1 for s in res.schedules if s.shifts[d].value == "M")
        if has_m:
            act += 1
    assert act == 3


def _short_night_returns(r):
    """짧은 텀(1~3일)을 오프만으로 잇고 나이트로 복귀(N-오프…오프-N)한 횟수 = 페널티 대상."""
    bad = 0
    for s in r.schedules:
        v = [x.value for x in s.shifts]
        for i in range(len(v)):
            if v[i] != "N":
                continue
            for g in (1, 2, 3):
                e = i + g + 1
                if e < len(v) and v[e] == "N" and all(v[i + k] == "O" for k in range(1, g + 1)):
                    bad += 1
    return bad


def test_night_gap_avoids_short_night_returns():
    """weight_night_gap_work를 켜면 여유 있는 편성에서 짧은-텀 나이트 복귀(N-오프-N)가 사라진다.

    결정적(단일 워커+시드) 편성으로 재현 가능하게 검증한다.
    """
    base = dict(num_days=10, nurses=_nurses(8), min_staff=MinStaff(D=1, E=1, N=1),
                max_consecutive_nights=3, time_limit_seconds=15,
                num_workers=1, random_seed=7)
    on = solve(ScheduleRequest(**base, weight_night_gap_work=300))
    assert on.feasible
    # 중간에 데이/이브닝 근무 없이 오프만 하고 나이트로 복귀하는 짧은 텀이 없어야 한다
    assert _short_night_returns(on) == 0
