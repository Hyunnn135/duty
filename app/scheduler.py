"""OR-Tools CP-SAT 기반 간호사 근무표(듀티표) 자동 생성 엔진.

제약조건 (Phase 1 시점 — 규칙은 config.py/요청 파라미터로 조정)
--------
하드 (반드시 지킴):
  1. 하루에 정확히 하나 배정 (대원칙 P2)
  2. 교대별 최소 인원 (요일별 staffing 또는 min_staff)
  3. 회전 하드 금지: N→D/E/M, E→D (대원칙 P1·P3, config.HARD_FORBIDDEN_TRANSITIONS)
  4. N-OFF-D 금지 (대원칙 P1)
  5. 연속 근무 ≤ max_consecutive_days (전월 이월 포함)
  6. 연속 나이트 ≤ max_consecutive_nights (전월 이월 포함)
  7. 간호사별 최소 오프
  8. forbid 요청 / 사전 배정(연차 등) 고정
  9. night_eligible=False 간호사 나이트 금지 (신규·임신)
 10. 전월 경계 회전: 이월 마지막 근무 → 1일차 전이에도 3번 규칙 적용

소프트 (목적함수 감점):
  - 원티드 오프 미반영 (최우선 가중치) / 원티드 D·E·N 미반영 (낮은 가중치)
  - prefer 요청 미반영
  - 나이트·근무일 편차 (공정성)

Phase 2 예정: 팀별 최소 인원, 적정(target) 인원 감점, T6a(N블록≥2)·T6b,
EOD·소프트 전이 감점, 달력주 오프≥2, 미드=저연차, 프리셉터 동행, 월 나이트 상한.
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from .config import HARD_FORBIDDEN_GAP_PATTERNS, HARD_FORBIDDEN_TRANSITIONS
from .models import (
    WORK_SHIFTS,
    NurseSchedule,
    RequestType,
    ScheduleRequest,
    ScheduleResponse,
    Shift,
)

ALL_SHIFTS: list[Shift] = [*WORK_SHIFTS, Shift.OFF]


def _preflight(req: ScheduleRequest) -> str | None:
    """명백히 풀 수 없는 입력을 사전 차단. 문제 있으면 사유 문자열 반환."""
    n = len(req.nurses)
    worst = 0
    for d in range(req.num_days):
        st = req.staffing_for(d)
        if st is not None:
            total = sum(st.of(s).min for s in WORK_SHIFTS)
        else:
            total = sum(req.min_staff.get(s) for s in WORK_SHIFTS)
        worst = max(worst, total)
    if worst > n:
        return (
            f"하루 최소 필요 인원 합계({worst})가 간호사 수({n})보다 많아 "
            f"배정이 불가능합니다."
        )
    if req.min_off_days > 0:
        max_work_slots = n * (req.num_days - req.min_off_days)
        if worst * req.num_days > max_work_slots:
            return "min_off_days 설정이 너무 커서 최소 인원을 채울 수 없습니다."
    return None


def _trailing_streak(seq: list[str], pred) -> int:
    """이월 근무 나열의 끝에서부터 pred를 만족하는 연속 길이."""
    k = 0
    for s in reversed(seq):
        if pred(s):
            k += 1
        else:
            break
    return k


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
    nd = req.num_days
    days = range(nd)
    N = len(nurses)
    idx = {nurse.id: i for i, nurse in enumerate(nurses)}

    # x[i, d, s] = 1 이면 간호사 i가 d일에 교대 s 근무
    x: dict[tuple[int, int, Shift], cp_model.IntVar] = {}
    for i in range(N):
        for d in days:
            for s in ALL_SHIFTS:
                x[i, d, s] = model.new_bool_var(f"x_{i}_{d}_{s.value}")

    # (1) 하루에 정확히 하나의 배정 (대원칙 P2)
    for i in range(N):
        for d in days:
            model.add_exactly_one(x[i, d, s] for s in ALL_SHIFTS)

    # (2) 교대별 최소 인원 (요일별 staffing 우선, 없으면 min_staff)
    for d in days:
        st = req.staffing_for(d)
        for s in WORK_SHIFTS:
            need = st.of(s).min if st is not None else req.min_staff.get(s)
            if need > 0:
                model.add(sum(x[i, d, s] for i in range(N)) >= need)

    # (3) 회전 하드 금지 전이 (대원칙 P1·P3 — config에서 로드)
    for i in range(N):
        for d in range(nd - 1):
            for prev_s, next_s in HARD_FORBIDDEN_TRANSITIONS:
                model.add(x[i, d + 1, next_s] == 0).only_enforce_if(x[i, d, prev_s])

    # (4) 2일 갭 하드 금지 패턴 (N-OFF-D, 대원칙 P1)
    for i in range(N):
        for d in range(nd - 2):
            for a, b, c in HARD_FORBIDDEN_GAP_PATTERNS:
                model.add_bool_or(
                    x[i, d, a].negated(),
                    x[i, d + 1, b].negated(),
                    x[i, d + 2, c].negated(),
                )

    # 근무 여부 보조 변수
    work: dict[tuple[int, int], cp_model.IntVar] = {}
    for i in range(N):
        for d in days:
            work[i, d] = model.new_bool_var(f"work_{i}_{d}")
            model.add(work[i, d] == sum(x[i, d, s] for s in WORK_SHIFTS))

    # (5) 연속 근무일 제한
    K = req.max_consecutive_days
    for i in range(N):
        for start in range(nd - K):
            model.add(sum(work[i, start + k] for k in range(K + 1)) <= K)

    # (6) 연속 나이트 제한
    KN = req.max_consecutive_nights
    for i in range(N):
        for start in range(nd - KN):
            model.add(sum(x[i, start + k, Shift.NIGHT] for k in range(KN + 1)) <= KN)

    # (9) 나이트 불가 간호사 (신규·임신 — 근로기준법 §70)
    for i, nurse in enumerate(nurses):
        if not nurse.night_eligible:
            for d in days:
                model.add(x[i, d, Shift.NIGHT] == 0)

    # (10) 전월 이월(carry-over): 월초 연속 제한 + 경계 회전
    for nid, seq in req.carry_over.items():
        i = idx[nid]
        # 연속 근무 이월: 끝에서 이어지는 근무일 수 t → 월초 (K-t+1)일 창에 휴무 1개 이상
        t = _trailing_streak(seq, lambda s: s != Shift.OFF.value)
        if t > 0:
            span = max(0, K - t + 1)
            if 0 < span <= nd:
                model.add(sum(work[i, d] for d in range(span)) <= span - 1)
        # 연속 나이트 이월
        tn = _trailing_streak(seq, lambda s: s == Shift.NIGHT.value)
        if tn > 0:
            span_n = max(0, KN - tn + 1)
            if 0 < span_n <= nd:
                model.add(
                    sum(x[i, d, Shift.NIGHT] for d in range(span_n)) <= span_n - 1
                )
        # 경계 회전: 전월 마지막 근무 → 1일차
        if seq:
            last = seq[-1]
            for prev_s, next_s in HARD_FORBIDDEN_TRANSITIONS:
                if last == prev_s.value:
                    model.add(x[i, 0, next_s] == 0)
            # N-OFF-D 경계: (…N,O | 1일차 D) / (…N | O,D)
            for a, b, c in HARD_FORBIDDEN_GAP_PATTERNS:
                if len(seq) >= 2 and seq[-2] == a.value and seq[-1] == b.value:
                    model.add(x[i, 0, c] == 0)
                if last == a.value and nd >= 2:
                    model.add_bool_or(
                        x[i, 0, b].negated(), x[i, 1, c].negated()
                    )

    # (7) 최소 오프
    if req.min_off_days > 0:
        for i in range(N):
            model.add(sum(x[i, d, Shift.OFF] for d in days) >= req.min_off_days)

    # (8a) 사전 배정 (연차 등): 해당 날 OFF로 하드 고정, 표시 라벨은 코드
    label_override: dict[tuple[int, int], str] = {}
    for p in req.pre_assigned:
        if p.nurse_id not in idx:
            continue
        i = idx[p.nurse_id]
        model.add(x[i, p.day, Shift.OFF] == 1)
        label_override[i, p.day] = p.code.value

    # (8b) 개인 요청 (prefer/forbid)
    pref_terms: list[cp_model.IntVar] = []
    for r in req.requests:
        if r.nurse_id not in idx or not (0 <= r.day < nd):
            continue
        i = idx[r.nurse_id]
        var = x[i, r.day, r.shift]
        if r.type == RequestType.FORBID:
            model.add(var == 0)
        else:
            miss = model.new_bool_var(f"miss_{i}_{r.day}_{r.shift.value}")
            model.add(miss == 1 - var)
            pref_terms.append(miss)

    # (소프트) 원티드 신청: 오프=최우선, D/E/N=낮은 가중치
    wanted_off_terms: list[cp_model.IntVar] = []
    wanted_work_terms: list[cp_model.IntVar] = []
    for w in req.wanted:
        if w.nurse_id not in idx:
            continue
        i = idx[w.nurse_id]
        for d in w.days():
            miss = model.new_bool_var(f"wmiss_{i}_{d}_{w.shift.value}")
            model.add(miss == 1 - x[i, d, w.shift])
            (wanted_off_terms if w.shift == Shift.OFF else wanted_work_terms).append(miss)

    # (소프트) 공정성: 나이트/총근무 편차
    fairness_terms: list[cp_model.IntVar] = []
    if req.weight_fairness > 0 and N > 1:
        night_counts, work_counts = [], []
        for i in range(N):
            nc = model.new_int_var(0, nd, f"night_{i}")
            model.add(nc == sum(x[i, d, Shift.NIGHT] for d in days))
            night_counts.append(nc)
            wc = model.new_int_var(0, nd, f"workcnt_{i}")
            model.add(wc == sum(work[i, d] for d in days))
            work_counts.append(wc)
        # 나이트 불가 간호사는 편차 계산에서 제외
        eligible = [night_counts[i] for i in range(N) if nurses[i].night_eligible]
        if len(eligible) > 1:
            n_max = model.new_int_var(0, nd, "night_max")
            n_min = model.new_int_var(0, nd, "night_min")
            model.add_max_equality(n_max, eligible)
            model.add_min_equality(n_min, eligible)
            spread = model.new_int_var(0, nd, "night_spread")
            model.add(spread == n_max - n_min)
            fairness_terms.append(spread)
        w_max = model.new_int_var(0, nd, "work_max")
        w_min = model.new_int_var(0, nd, "work_min")
        model.add_max_equality(w_max, work_counts)
        model.add_min_equality(w_min, work_counts)
        wspread = model.new_int_var(0, nd, "work_spread")
        model.add(wspread == w_max - w_min)
        fairness_terms.append(wspread)

    # ---- 목적 함수 ----
    objective = []
    if wanted_off_terms:
        objective.append(req.weight_wanted_off * sum(wanted_off_terms))
    if wanted_work_terms:
        objective.append(req.weight_wanted_work * sum(wanted_work_terms))
    if pref_terms:
        objective.append(req.weight_preference * sum(pref_terms))
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
            num_days=nd,
            schedules=[],
            unmet_preferences=0,
            message="주어진 제약조건을 모두 만족하는 근무표를 찾지 못했습니다. "
            "최소 인원이나 연속근무 제한 등을 완화해 보세요.",
        )

    # ---- 결과 추출 ----
    schedules: list[NurseSchedule] = []
    for i, nurse in enumerate(nurses):
        shifts: list[Shift] = []
        labels: list[str] = []
        for d in days:
            for s in ALL_SHIFTS:
                if solver.value(x[i, d, s]) == 1:
                    shifts.append(s)
                    labels.append(label_override.get((i, d), s.value))
                    break
        counts: dict[str, int] = {}
        for lab in labels:
            counts[lab] = counts.get(lab, 0) + 1
        for s in ALL_SHIFTS:  # 기본 키는 항상 존재하도록
            counts.setdefault(s.value, 0)
        schedules.append(
            NurseSchedule(
                nurse_id=nurse.id, name=nurse.name,
                shifts=shifts, labels=labels, counts=counts,
            )
        )

    unmet_pref = sum(int(solver.value(t)) for t in pref_terms) if pref_terms else 0
    unmet_woff = (
        sum(int(solver.value(t)) for t in wanted_off_terms) if wanted_off_terms else 0
    )

    return ScheduleResponse(
        status=status_name,
        feasible=True,
        num_days=nd,
        schedules=schedules,
        unmet_preferences=unmet_pref,
        unmet_wanted_off=unmet_woff,
        message="근무표 생성 완료" + (" (최적해)" if status == cp_model.OPTIMAL else ""),
    )
