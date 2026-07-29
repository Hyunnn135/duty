"""운영 모드(61병동 확정 규칙) 회귀 테스트.

실데이터(22명·2026-08·실제 원티드)로 운영 모드를 돌렸을 때, 사용자와 확정한 품질
계약이 유지되는지 고정한다. 대부분 하드 제약이라 feasible 해에서 항상 성립하며,
앞으로 알고리즘이 바뀌어도 이 품질이 깨지면 즉시 실패로 잡힌다.

확정 계약(대화 기준):
  - 매일 인원이 D4·E4·N4·M1(액팅) 또는 D5·E5·N4 중 하나와 '정확' 일치
  - 액팅(M) 근무일 = 정확히 8일(acting_days=8)
  - 인당 오프 ∈ {11, 12} (8/17 대체공휴일 반영, 목표 11)
  - 나이트 인당 편차 ≤ 1
  - 원티드 오프 100% 반영(unmet=0)
  - 대원칙(하드) 위반 0
"""
from __future__ import annotations

import collections

import pytest

from app.scheduler import solve

# 실데이터 하네스(명단·팀·이월·실제 원티드)를 그대로 재사용
experiment = pytest.importorskip("scripts.experiment")

PATTERNS = [{"D": 4, "E": 4, "N": 4, "M": 1}, {"D": 5, "E": 5, "N": 4, "M": 0}]
PATTERN_TUPLES = {(4, 4, 4, 1), (5, 5, 4, 0)}
ACTING_DAYS = 8
OFF_TARGET = 11  # 2026-08: 주말10 + 대체공휴일(8/17) 1


def _norm(v: str) -> str:
    return v if v in ("D", "E", "N", "M") else ("M" if v in ("M/D", "M/E") else "O")


def _day_counts(res, day):
    c = collections.Counter()
    for s in res.schedules:
        c[_norm(s.shifts[day].value)] += 1
    return c


@pytest.fixture(scope="module")
def ops_result():
    """운영 모드로 8월을 1회 생성해 모듈 전체에서 공유(느린 풀이 1회만)."""
    req = experiment.build_request(
        exact_mode=True, daily_patterns=PATTERNS, acting_days=ACTING_DAYS,
        time_limit_seconds=90,
    )
    return solve(req)


def test_ops_feasible(ops_result):
    assert ops_result.feasible, f"운영 모드가 실패함: {ops_result.status}"


def test_ops_exact_daily_patterns(ops_result):
    """매일 인원이 두 허용 패턴 중 하나와 정확히 일치(초과·미달 불가)."""
    nd = ops_result.num_days
    bad = []
    for d in range(nd):
        c = _day_counts(ops_result, d)
        tup = (c["D"], c["E"], c["N"], c["M"])
        if tup not in PATTERN_TUPLES:
            bad.append((d + 1, tup))
    assert not bad, f"패턴 위반 일자: {bad}"


def test_ops_acting_days_exact(ops_result):
    """액팅(M) 근무일이 정확히 acting_days(8)."""
    nd = ops_result.num_days
    acting = sum(1 for d in range(nd) if _day_counts(ops_result, d)["M"] > 0)
    assert acting == ACTING_DAYS, f"액팅일수={acting}, 기대={ACTING_DAYS}"


def test_ops_off_band(ops_result):
    """인당 오프는 목표 T와 T+1 사이(전원 11~12), 아무도 그 밖으로 못 나감."""
    offs = [sum(1 for x in s.shifts if x.value == "O") for s in ops_result.schedules]
    assert min(offs) >= OFF_TARGET, f"오프 최소 {min(offs)} < {OFF_TARGET}"
    assert max(offs) <= OFF_TARGET + 1, f"오프 최대 {max(offs)} > {OFF_TARGET + 1}"


def test_ops_night_spread(ops_result):
    """나이트 인당 편차 ≤ 1(하드 밴드)."""
    nights = [s.counts.get("N", 0) for s in ops_result.schedules]
    assert max(nights) - min(nights) <= 1, f"나이트 편차 {max(nights) - min(nights)}"


def test_ops_wanted_fully_reflected(ops_result):
    """원티드 오프 100% 반영."""
    assert ops_result.unmet_wanted_off == 0, (
        f"미반영 원티드 {ops_result.unmet_wanted_off}건")


def test_ops_no_hard_violations(ops_result):
    """대원칙(하드) 위반 0 — 검사기로 교차 검증."""
    grid = {s.name: [_norm(x.value) for x in s.shifts] for s in ops_result.schedules}
    m = experiment.metrics(grid)
    assert m["hard"] == 0, f"대원칙 위반 {m['hard']}건"
