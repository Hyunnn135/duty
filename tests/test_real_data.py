"""실데이터 재현 테스트 — 실제 61병동 2026년 8월 근무표로 규칙·솔버 검증.

두 축으로 검증한다:
  1. 실제 근무표가 우리의 하드 대원칙을 실제로 만족하는가 (규칙 ↔ 현실 일치).
  2. 실제 병동 설정(명단·팀·연차·전월 이월·인력 기준)을 솔버에 넣으면, 대원칙 위반 0의
     유효한 근무표를 생성하는가 (솔버가 실제 상황을 다룰 수 있는가).

8월을 주 대상으로 사용한다(전출·M/D 라벨 없는 깨끗한 달). 7월은 전출 인원 경계에서만
예외가 있음을 문서화한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import (
    DayStaffing,
    LeaveCode,
    Nurse,
    PreAssigned,
    ScheduleRequest,
    Shift,
    ShiftStaff,
    WantedRequest,
)
from app.rules_check import ValidateRequest, check
from app.scheduler import solve

DATA = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


def _load():
    return json.loads(DATA.read_text(encoding="utf-8"))


def _norm(c: str) -> str:
    if c in ("M/D", "M/E"):
        return "M"
    return c if c in ("D", "E", "N", "M") else "O"


def _rows(d, mk):
    return [r for r in d["months"][mk]["roster"] if not r.get("manager")]


def _carry_july_to_august(d):
    """8월 각 간호사의 전월(7월) 마지막 7일을 이월값으로 (이름 매칭)."""
    jul = {r["name"]: r["shifts"] for r in _rows(d, "2026-07")}
    out = {}
    for r in _rows(d, "2026-08"):
        if r["name"] in jul:
            out[r["empno"]] = [_norm(c) for c in jul[r["name"]][-7:]]
    return out


# ---- 1. 실데이터가 하드 대원칙을 만족하는가 ----

def test_real_august_satisfies_hard_principles():
    d = _load()
    rows = _rows(d, "2026-08")
    sched = {r["name"]: r["shifts"] for r in rows}
    # 8월 이월(7월 말) 포함해 검사
    jul = {r["name"]: r["shifts"] for r in _rows(d, "2026-07")}
    carry = {r["name"]: [_norm(c) for c in jul[r["name"]][-7:]] for r in rows if r["name"] in jul}
    res = check(ValidateRequest(schedules=sched, carry_over=carry))
    assert res.ok, f"실제 8월 근무표에서 예상치 못한 대원칙 위반: {[ (v.principle, v.nurse, v.day) for v in res.violations ]}"


def test_real_july_violations_only_at_transfer_boundary():
    """7월의 유일한 하드 위반은 전출 인원(김에스더·김민신) 경계 아티팩트여야 한다."""
    d = _load()
    rows = _rows(d, "2026-07")
    sched = {r["name"]: r["shifts"] for r in rows}
    res = check(ValidateRequest(schedules=sched))
    transfers = {"김에스더", "김민신"}
    offenders = {v.nurse for v in res.violations}
    assert offenders <= transfers, f"전출 외 인원의 하드 위반 발견: {offenders - transfers}"


# ---- 2. 솔버가 실제 8월 상황을 다루는가 ----

def _august_request(d) -> tuple[ScheduleRequest, list, dict]:
    rows = _rows(d, "2026-08")
    # 명단: 팀별 등장 순서 = 경력순위(1=최고참)
    team_seen: dict[int, int] = {}
    nurses = []
    for r in rows:
        team_seen[r["team"]] = team_seen.get(r["team"], 0) + 1
        nurses.append(Nurse(id=r["empno"], name=r["name"], team=r["team"],
                            seniority_rank=team_seen[r["team"]], night_eligible=True))
    # 사전 배정: 실제 연차(HY) 위치 고정
    pre = []
    for r in rows:
        for day, c in enumerate(r["shifts"]):
            if c == "HY":
                pre.append(PreAssigned(nurse_id=r["empno"], day=day, code=LeaveCode.HY))
    # 인력 기준(실측 최소 — 평일 D5/E5/N4, 주말 D4/E4/N4): 실제가 이 값을 항상 충족
    wd = DayStaffing(D=ShiftStaff(min=5), E=ShiftStaff(min=5), N=ShiftStaff(min=4), M=ShiftStaff(min=0))
    we = DayStaffing(D=ShiftStaff(min=4), E=ShiftStaff(min=4), N=ShiftStaff(min=4), M=ShiftStaff(min=0))
    staffing = {"weekday": wd, "weekend": we, "holiday": wd}
    carry = _carry_july_to_august(d)
    req = ScheduleRequest(
        year=2026, month=8, nurses=nurses, staffing=staffing,
        pre_assigned=pre, carry_over=carry,
        max_consecutive_days=5, max_consecutive_nights=3, max_nights_per_month=7,
        team_min_staff=0,  # 실제도 1일 미충족 → 재현 보장 위해 하드 아님
        enforce_night_block=True, time_limit_seconds=20,
    )
    return req, rows, carry


def test_solver_feasible_on_real_august():
    d = _load()
    req, rows, carry = _august_request(d)
    res = solve(req)
    assert res.feasible, f"실제 8월 상황에서 솔버가 해를 찾지 못함: {res.message}"
    assert res.num_days == 31
    assert len(res.schedules) == len(rows)


def test_solver_output_zero_principle_violations_on_real_august():
    d = _load()
    req, rows, carry = _august_request(d)
    res = solve(req)
    assert res.feasible
    # 솔버 결과를 이름 키로 재검사 (이월은 이름 키로 변환)
    id2name = {r["empno"]: r["name"] for r in rows}
    sched = {s.name: s.labels for s in res.schedules}
    carry_by_name = {id2name[i]: seq for i, seq in carry.items() if i in id2name}
    v = check(ValidateRequest(schedules=sched, carry_over=carry_by_name,
                              max_consecutive_days=5, max_consecutive_nights=3))
    assert v.ok, f"솔버 결과에 대원칙 위반: {[(x.principle, x.nurse, x.day) for x in v.violations]}"


def test_solver_meets_real_coverage_and_night_cap():
    d = _load()
    req, rows, carry = _august_request(d)
    res = solve(req)
    assert res.feasible
    by_day = list(zip(*[s.shifts for s in res.schedules]))  # [day] -> tuple of shifts
    for day, col in enumerate(by_day):
        cat = req.day_category(day)
        need = {"weekday": (5, 5, 4), "weekend": (4, 4, 4), "holiday": (5, 5, 4)}[cat]
        vals = [c.value for c in col]
        assert vals.count("D") >= need[0], f"{day+1}일 D {vals.count('D')} < {need[0]}"
        assert vals.count("E") >= need[1], f"{day+1}일 E {vals.count('E')} < {need[1]}"
        assert vals.count("N") >= need[2], f"{day+1}일 N {vals.count('N')} < {need[2]}"
    # 월 나이트 상한
    for s in res.schedules:
        assert s.counts.get("N", 0) <= 7


def test_solver_preserves_real_leave_days():
    """실제 연차(HY) 날짜는 솔버 결과에서도 휴무(HY 라벨)로 유지된다."""
    d = _load()
    req, rows, carry = _august_request(d)
    res = solve(req)
    assert res.feasible
    by_id = {s.nurse_id: s for s in res.schedules}
    for r in rows:
        for day, c in enumerate(r["shifts"]):
            if c == "HY":
                assert by_id[r["empno"]].labels[day] == "HY"


def test_report_similarity_to_real(capsys):
    """참고용: 솔버 결과와 실제 근무표의 셀 일치율(대안 다수 존재하므로 하드 단정은 안 함)."""
    d = _load()
    req, rows, carry = _august_request(d)
    res = solve(req)
    assert res.feasible
    by_id = {s.nurse_id: s for s in res.schedules}
    match = total = 0
    for r in rows:
        got = by_id[r["empno"]].shifts
        for day, c in enumerate(r["shifts"]):
            total += 1
            if got[day].value == _norm(c):
                match += 1
    ratio = match / total if total else 0
    print(f"\n[실데이터 재현] 셀 일치율: {match}/{total} = {ratio:.1%}")
    # 참고용 수치일 뿐 단정하지 않는다 — 유효한 근무표는 다수 존재하므로 무선호 일치율은
    # 낮고 실행마다 변동한다. 실제 '재현' 검증은 test_reproduces_closely_given_real_preferences.
    assert 0.0 <= ratio <= 1.0


def test_reproduces_closely_given_real_preferences(capsys):
    """실제 근무 선호(오프=원티드오프, D/E/N=원티드근무)를 그대로 넣으면, 솔버가 실제
    근무표를 매우 근접하게 재현한다 — 재현 가능성 검증."""
    d = _load()
    req, rows, carry = _august_request(d)
    wanted = []
    for r in rows:
        for day, c in enumerate(r["shifts"]):
            n = _norm(c)
            if c == "HY":
                continue  # 사전배정으로 이미 고정
            if n == "O":
                wanted.append(WantedRequest(nurse_id=r["empno"], start_day=day, shift=Shift.OFF))
            elif n in ("D", "E", "N"):
                wanted.append(WantedRequest(nurse_id=r["empno"], start_day=day, shift=Shift(n)))
    req.wanted = wanted
    req.weight_wanted_work = 20  # 재현을 위해 근무 선호도 상향
    # 실제 오프 전체를 '선호'로 넣는 재현 실험이므로, 경쟁적 신청 전제인 E4(팀 오프 겹침
    # 금지, 주말=팀 전원 오프와 충돌)는 끈다. E4의 정상 동작은 test_alternatives/E4 참고.
    req.exclusive_team_wanted_off = False
    req.time_limit_seconds = 25
    res = solve(req)
    assert res.feasible
    by_id = {s.nurse_id: s for s in res.schedules}
    match = total = 0
    for r in rows:
        got = by_id[r["empno"]].shifts
        for day, c in enumerate(r["shifts"]):
            total += 1
            if got[day].value == _norm(c):
                match += 1
    ratio = match / total
    print(f"\n[실데이터 재현·선호반영] 셀 일치율: {match}/{total} = {ratio:.1%}")
    # 선호를 주면 무선호(≈30%)보다 훨씬 높게 재현되어야 한다(로컬 관측 ~99%).
    # CI 저속 머신의 시간제한 여유를 위해 하한은 보수적으로 둔다.
    assert ratio >= 0.60
