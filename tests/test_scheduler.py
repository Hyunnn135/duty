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
