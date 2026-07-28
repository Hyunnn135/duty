"""대원칙 대안 3안 생성기 테스트."""
from __future__ import annotations

from app.alternatives import AlternativesRequest, generate
from app.rules_check import ValidateRequest, check


def _violations(grid, carry=None):
    return check(ValidateRequest(schedules=grid, carry_over=carry or {})).violations


def test_fixes_p1_and_p3_minimally():
    # 간호사A: N→D (P1), 간호사B: E→D (P3). 나머지는 정상.
    grid = {
        "간호사A": ["N", "D", "O", "O", "O", "O", "O"],
        "간호사B": ["E", "D", "O", "O", "O", "O", "O"],
        "간호사C": ["D", "E", "N", "N", "O", "O", "D"],
        "간호사D": ["O", "O", "D", "E", "N", "N", "O"],
    }
    assert _violations(grid)  # 원본은 위반 있음
    res = generate(AlternativesRequest(schedules=grid, count=3, time_limit_seconds=5))
    assert res.ok and len(res.alternatives) >= 1
    # 모든 대안은 대원칙 위반이 0이어야 한다
    for alt in res.alternatives:
        assert _violations(alt.grid) == []
    # 최소 변경 대안은 소수의 칸만 바꾼다
    assert res.alternatives[0].changed <= 4
    # 변경 내역이 실제 그리드와 일치
    a0 = res.alternatives[0]
    assert a0.changed == len(a0.changes)


def test_alternatives_are_distinct():
    grid = {
        "A": ["N", "D", "O", "O", "O", "O", "O"],  # P1
        "B": ["D", "E", "N", "N", "O", "D", "E"],
        "C": ["O", "D", "E", "O", "N", "N", "O"],
        "D": ["E", "O", "D", "E", "O", "O", "N"],
    }
    res = generate(AlternativesRequest(schedules=grid, count=3, time_limit_seconds=5))
    assert res.ok
    # 생성된 대안들은 서로 다른 그리드여야 한다
    serialized = [tuple(tuple(v) for v in a.grid.values()) for a in res.alternatives]
    assert len(set(serialized)) == len(serialized)


def test_leave_cells_preserved():
    # 연차(HY)는 고정되어 그대로 유지되어야 한다
    grid = {
        "A": ["N", "D", "HY", "O", "O", "O", "O"],  # P1: N→D
        "B": ["D", "E", "N", "N", "O", "O", "D"],
        "C": ["O", "O", "D", "E", "N", "N", "O"],
    }
    res = generate(AlternativesRequest(schedules=grid, count=2, time_limit_seconds=5))
    assert res.ok
    for alt in res.alternatives:
        assert alt.grid["A"][2] == "HY"  # 연차 위치 보존


def test_carry_over_boundary_alternative():
    # 전월 말 나이트 → 1일차 D는 P1(N-OFF-D 아님, 직접 N→D는 이월경계) 위반
    grid = {"A": ["D", "O", "O", "O", "O"], "B": ["O", "D", "E", "N", "N"],
            "C": ["E", "N", "N", "O", "D"]}
    carry = {"A": ["N", "N"]}
    assert _violations(grid, carry)  # 이월 경계 위반 존재
    res = generate(AlternativesRequest(schedules=grid, carry_over=carry, count=2, time_limit_seconds=5))
    assert res.ok
    for alt in res.alternatives:
        assert _violations(alt.grid, carry) == []


def test_no_violation_returns_zero_change():
    # 이미 정상인 그리드 → 변경 0의 대안(원본과 동일)
    grid = {"A": ["D", "E", "N", "N", "O"], "B": ["O", "D", "E", "O", "N"],
            "C": ["N", "N", "O", "O", "D"]}
    assert _violations(grid) == []
    res = generate(AlternativesRequest(schedules=grid, count=1, time_limit_seconds=5))
    assert res.ok and res.alternatives[0].changed == 0
