"""Phase 1 데이터 모델 검증 테스트 (솔버 호출 없음)."""
from __future__ import annotations

import pytest

from app.models import (
    DayStaffing,
    Nurse,
    PreAssigned,
    ScheduleRequest,
    Shift,
    ShiftStaff,
    WantedRequest,
)


def _nurses(n: int) -> list[Nurse]:
    return [Nurse(id=f"n{i}", name=f"간호사{i}") for i in range(n)]


def test_year_month_resolves_num_days():
    req = ScheduleRequest(year=2026, month=7, nurses=_nurses(3))
    assert req.num_days == 31
    req2 = ScheduleRequest(year=2026, month=2, nurses=_nurses(3))
    assert req2.num_days == 28


def test_num_days_or_year_month_required():
    with pytest.raises(ValueError):
        ScheduleRequest(nurses=_nurses(3))


def test_day_category_weekend_and_holiday():
    # 2026-07-01은 수요일 → 4일(토)·5일(일)이 주말
    req = ScheduleRequest(year=2026, month=7, nurses=_nurses(3), holidays=[6])
    assert req.day_category(0) == "weekday"   # 7/1 수
    assert req.day_category(3) == "weekend"   # 7/4 토
    assert req.day_category(4) == "weekend"   # 7/5 일
    assert req.day_category(5) == "holiday"   # 7/6 (수동 공휴일)


def test_day_category_without_calendar_defaults_weekday():
    req = ScheduleRequest(num_days=10, nurses=_nurses(3))
    assert all(req.day_category(d) == "weekday" for d in range(10))


def test_staffing_for_picks_category():
    wd = DayStaffing(D=ShiftStaff(min=4), N=ShiftStaff(min=3))
    we = DayStaffing(D=ShiftStaff(min=3), N=ShiftStaff(min=3))
    req = ScheduleRequest(
        year=2026, month=7, nurses=_nurses(3),
        staffing={"weekday": wd, "weekend": we},
    )
    assert req.staffing_for(0).D.min == 4   # 평일
    assert req.staffing_for(3).D.min == 3   # 토요일
    # holiday 미지정 → weekday 폴백
    req2 = ScheduleRequest(
        year=2026, month=7, nurses=_nurses(3), holidays=[2],
        staffing={"weekday": wd, "weekend": we},
    )
    assert req2.staffing_for(1).D.min == 4


def test_wanted_range_and_validation():
    w = WantedRequest(nurse_id="n0", start_day=3, end_day=5, shift=Shift.OFF)
    assert list(w.days()) == [3, 4, 5]
    with pytest.raises(ValueError):
        WantedRequest(nurse_id="n0", start_day=5, end_day=3)


def test_wanted_out_of_range_rejected():
    with pytest.raises(ValueError):
        ScheduleRequest(
            num_days=7, nurses=_nurses(3),
            wanted=[WantedRequest(nurse_id="n0", start_day=10)],
        )


def test_pre_assigned_out_of_range_rejected():
    with pytest.raises(ValueError):
        ScheduleRequest(
            num_days=7, nurses=_nurses(3),
            pre_assigned=[PreAssigned(nurse_id="n0", day=9, code="HY")],
        )


def test_carry_over_validation():
    with pytest.raises(ValueError):  # 명단에 없는 id
        ScheduleRequest(num_days=7, nurses=_nurses(2), carry_over={"nx": ["N"]})
    with pytest.raises(ValueError):  # 잘못된 근무 값
        ScheduleRequest(num_days=7, nurses=_nurses(2), carry_over={"n0": ["Z"]})
    req = ScheduleRequest(num_days=7, nurses=_nurses(2), carry_over={"n0": ["E", "N", "N"]})
    assert req.carry_over["n0"] == ["E", "N", "N"]


def test_nurse_defaults_backward_compatible():
    n = Nurse(id="a", name="가")
    assert n.team == 1 and n.night_eligible and not n.is_new
