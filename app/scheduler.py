"""OR-Tools CP-SAT 기반 간호사 근무표(듀티표) 자동 생성 엔진.

제약조건
--------
하드 (반드시 지킴):
  1. 각 간호사는 하루에 D/E/N/O 중 정확히 하나에 배정된다.
  2. 각 교대(D/E/N)는 하루에 최소 필요 인원 이상 배치된다.
  3. 나이트(N) 다음 날은 D 또는 E로 배정하지 않는다 (나이트 후 휴식).
  4. 연속 근무일이 max_consecutive_days 를 넘지 않는다.
  5. 연속 나이트가 max_consecutive_nights 를 넘지 않는다.
  6. 간호사별 오프가 min_off_days 이상이다.
  7. forbid 타입 개인 요청(예: 승인된 연차)은 반드시 지킨다.

소프트 (되도록 반영, 목적 함수로 최소화):
  - prefer 타입 개인 희망 미반영 (weight_preference)
  - 근무 배분 불공정: 나이트/총근무 편차 (weight_fairness)
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from .models import (
    WORK_SHIFTS,
    NurseSchedule,
    RequestType,
    ScheduleRequest,
    ScheduleResponse,
    Shift,
)

ALL_SHIFTS: list[Shift] = [Shift.DAY, Shift.EVENING, Shift.NIGHT, Shift.OFF]


def _preflight(req: ScheduleRequest) -> str | None:
    """명백히 풀 수 없는 입력을 사전 차단. 문제 있으면 사유 문자열 반환."""
    n = len(req.nurses)
    total_min = req.min_staff.D + req.min_staff.E + req.min_staff.N
    if total_min > n:
        return (
            f"하루 최소 필요 인원 합계({total_min})가 간호사 수({n})보다 많아 "
            f"배정이 불가능합니다."
        )
    # 오프 요구를 감안한 가용 인원 검사: 매일 최소 total_min 명이 근무해야 하므로
    # 기간 전체 근무 슬롯 하한이 인력으로 충족 가능한지 대략 확인한다.
    max_work_slots = n * (req.num_days - req.min_off_days)
    needed_work_slots = total_min * req.num_days
    if req.min_off_days > 0 and needed_work_slots > max_work_slots:
        return (
            "min_off_days 설정이 너무 커서 최소 인원을 채울 수 없습니다."
        )
    return None


def solve(req: ScheduleRequest) -> ScheduleResponse:
    """근무표를 생성한다."""
    problem = _preflight(req)
    if problem:
        return ScheduleResponse(
            status="INFEASIBLE",
            feasible=False,
            num_days=req.num_days,
            schedules=[],
            unmet_preferences=0,
            message=problem,
        )

    model = cp_model.CpModel()
    nurses = req.nurses
    days = range(req.num_days)
    N = len(nurses)
    idx = {nurse.id: i for i, nurse in enumerate(nurses)}

    # x[i, d, s] = 1 이면 간호사 i가 d일에 교대 s 근무
    x: dict[tuple[int, int, Shift], cp_model.IntVar] = {}
    for i in range(N):
        for d in days:
            for s in ALL_SHIFTS:
                x[i, d, s] = model.new_bool_var(f"x_{i}_{d}_{s.value}")

    # (1) 하루에 정확히 하나의 교대
    for i in range(N):
        for d in days:
            model.add_exactly_one(x[i, d, s] for s in ALL_SHIFTS)

    # (2) 교대별 최소 인원
    for d in days:
        for s in WORK_SHIFTS:
            model.add(
                sum(x[i, d, s] for i in range(N)) >= req.min_staff.get(s)
            )

    # (3) 나이트 다음 날은 D/E 금지
    for i in range(N):
        for d in range(req.num_days - 1):
            model.add(x[i, d + 1, Shift.DAY] == 0).only_enforce_if(x[i, d, Shift.NIGHT])
            model.add(x[i, d + 1, Shift.EVENING] == 0).only_enforce_if(
                x[i, d, Shift.NIGHT]
            )

    # 근무 여부 보조 변수: work[i,d] = 1 이면 그날 근무(오프 아님)
    work: dict[tuple[int, int], cp_model.IntVar] = {}
    for i in range(N):
        for d in days:
            work[i, d] = model.new_bool_var(f"work_{i}_{d}")
            model.add(work[i, d] == sum(x[i, d, s] for s in WORK_SHIFTS))

    # (4) 연속 근무일 제한: 임의의 (K+1)일 창에서 근무일 합 <= K
    K = req.max_consecutive_days
    for i in range(N):
        for start in range(req.num_days - K):
            model.add(sum(work[i, start + k] for k in range(K + 1)) <= K)

    # (5) 연속 나이트 제한
    KN = req.max_consecutive_nights
    for i in range(N):
        for start in range(req.num_days - KN):
            model.add(
                sum(x[i, start + k, Shift.NIGHT] for k in range(KN + 1)) <= KN
            )

    # (6) 최소 오프 일수
    if req.min_off_days > 0:
        for i in range(N):
            model.add(
                sum(x[i, d, Shift.OFF] for d in days) >= req.min_off_days
            )

    # (7)/(prefer) 개인 요청 처리
    penalty_terms: list[cp_model.IntVar] = []
    for r in req.requests:
        if r.nurse_id not in idx:
            continue
        i = idx[r.nurse_id]
        if not (0 <= r.day < req.num_days):
            continue
        var = x[i, r.day, r.shift]
        if r.type == RequestType.FORBID:
            model.add(var == 0)
        else:  # PREFER: 반영 안 되면 페널티
            miss = model.new_bool_var(f"miss_{i}_{r.day}_{r.shift.value}")
            # miss == 1 - var  (해당 교대에 배정되면 miss=0)
            model.add(miss == 1 - var)
            penalty_terms.append(miss)

    # ---- 공정성: 나이트 수 / 총 근무일 편차 최소화 ----
    fairness_terms: list[cp_model.IntVar] = []
    if req.weight_fairness > 0 and N > 1:
        night_counts = []
        work_counts = []
        for i in range(N):
            nc = model.new_int_var(0, req.num_days, f"night_{i}")
            model.add(nc == sum(x[i, d, Shift.NIGHT] for d in days))
            night_counts.append(nc)
            wc = model.new_int_var(0, req.num_days, f"workcnt_{i}")
            model.add(wc == sum(work[i, d] for d in days))
            work_counts.append(wc)

        n_max = model.new_int_var(0, req.num_days, "night_max")
        n_min = model.new_int_var(0, req.num_days, "night_min")
        model.add_max_equality(n_max, night_counts)
        model.add_min_equality(n_min, night_counts)
        night_spread = model.new_int_var(0, req.num_days, "night_spread")
        model.add(night_spread == n_max - n_min)

        w_max = model.new_int_var(0, req.num_days, "work_max")
        w_min = model.new_int_var(0, req.num_days, "work_min")
        model.add_max_equality(w_max, work_counts)
        model.add_min_equality(w_min, work_counts)
        work_spread = model.new_int_var(0, req.num_days, "work_spread")
        model.add(work_spread == w_max - w_min)

        fairness_terms = [night_spread, work_spread]

    # ---- 목적 함수 ----
    objective = []
    if penalty_terms:
        objective.append(req.weight_preference * sum(penalty_terms))
    if fairness_terms:
        objective.append(req.weight_fairness * sum(fairness_terms))
    if objective:
        model.minimize(sum(objective))

    # ---- 풀기 ----
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = req.time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)

    status_name = solver.status_name(status)
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    if not feasible:
        return ScheduleResponse(
            status=status_name,
            feasible=False,
            num_days=req.num_days,
            schedules=[],
            unmet_preferences=0,
            message="주어진 제약조건을 모두 만족하는 근무표를 찾지 못했습니다. "
            "최소 인원이나 연속근무 제한 등을 완화해 보세요.",
        )

    # ---- 결과 추출 ----
    schedules: list[NurseSchedule] = []
    for i, nurse in enumerate(nurses):
        shifts: list[Shift] = []
        for d in days:
            for s in ALL_SHIFTS:
                if solver.value(x[i, d, s]) == 1:
                    shifts.append(s)
                    break
        counts = {s.value: shifts.count(s) for s in ALL_SHIFTS}
        schedules.append(
            NurseSchedule(
                nurse_id=nurse.id, name=nurse.name, shifts=shifts, counts=counts
            )
        )

    unmet = sum(int(solver.value(t)) for t in penalty_terms) if penalty_terms else 0

    return ScheduleResponse(
        status=status_name,
        feasible=True,
        num_days=req.num_days,
        schedules=schedules,
        unmet_preferences=unmet,
        message="근무표 생성 완료" + (" (최적해)" if status == cp_model.OPTIMAL else ""),
    )
