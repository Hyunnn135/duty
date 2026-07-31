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
  9. night_eligible=False 간호사 나이트 금지 (임신 등, 면담 D3)
 10. 전월 경계 회전: 이월 마지막 근무 → 1일차 전이에도 3번 규칙 적용
 11. 팀별 최소 인원(team_min_staff), T6a 나이트 블록 ≥2, 월 나이트 상한
 12. F2 트레이닝 신규: 정원 제외 + 교육자와 항상 같은 근무(하드)
 13. E4 같은 팀 원티드 오프 겹침 금지(팀·날짜별 승인 오프 ≤1)

소프트 (목적함수 감점):
  - 원티드 오프 미반영 (최우선 가중치) / 원티드 D·E·N 미반영 (낮은 가중치)
  - prefer 요청 미반영 / 나이트·근무일 편차 (공정성)
  - 적정(target) 인원 부족, T6b 고립근무, EOD, 소프트 전이(M→D·E→M), 달력주 오프≥2
  - 미드(액팅)=저연차, 프리셉터 동행
  - E1 월 오프 수=목표(주말+공휴일), F1 연차 골고루(고/저연차 부재 감점),
    C3 나이트 블록 직후 오프<2 감점
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from .config import (
    HARD_FORBIDDEN_GAP_PATTERNS,
    HARD_FORBIDDEN_TRANSITIONS,
    SOFT_DISCOURAGED_TRANSITIONS,
)
from .models import (
    WORK_SHIFTS,
    NurseSchedule,
    RequestType,
    ScheduleRequest,
    ScheduleResponse,
    Shift,
)

ALL_SHIFTS: list[Shift] = [*WORK_SHIFTS, Shift.OFF]

# 결정적 타이브레이커용: 교대 선호 순서(작을수록 우선). D를 0으로 둬 기본 선호.
TIEBREAK_ORDER: dict[Shift, int] = {
    Shift.DAY: 0, Shift.MID: 1, Shift.EVENING: 2, Shift.NIGHT: 3, Shift.OFF: 4,
}
# 1차 목적을 사전식으로 우선시키는 배수(타이브레이크 최대치보다 크게).
_TIEBREAK_BIG = 2_000_000


def _preflight(req: ScheduleRequest) -> str | None:
    """명백히 풀 수 없는 입력을 사전 차단. 문제 있으면 사유 문자열 반환."""
    # 트레이닝 신규는 정원에 포함되지 않으므로 가용 인원에서 제외 (면담 F2)
    n = sum(
        1 for nu in req.nurses
        if not (req.exclude_trainee_from_staffing and nu.is_trainee)
    )
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

    # 트레이닝 신규는 정원(4/4/4)에 포함하지 않는다 (면담 F2 — 1인분 미수행)
    trainee_idx = {
        i for i, nu in enumerate(nurses)
        if req.exclude_trainee_from_staffing and nu.is_trainee
    }
    counted = [i for i in range(N) if i not in trainee_idx]

    # (2) 교대별 인원.
    #   daily_patterns 지정 시: 매일 허용 패턴 중 정확히 하나와 '정확' 일치(초과·미달 불가).
    #   미지정 시: 기존 최소 인원(요일별 staffing 또는 min_staff) ≥ 제약.
    acting_day_terms: list = []  # 액팅(M) 포함 패턴이 안 뽑힌 날 = 소폭 페널티(액팅 소폭 우대)
    acting_sel_all: list = []     # 날짜별 '액팅 패턴 선택됨' 지시자(하드 일수 고정용)
    if req.daily_patterns:
        pats = req.daily_patterns
        acting_pat = [p for p in range(len(pats)) if int(pats[p].get("M", 0)) > 0]
        for d in days:
            sel = [model.new_bool_var(f"pat_{d}_{p}") for p in range(len(pats))]
            model.add_exactly_one(sel)
            for s in WORK_SHIFTS:
                model.add(
                    sum(x[i, d, s] for i in counted)
                    == sum(sel[p] * int(pats[p].get(s.value, 0)) for p in range(len(pats)))
                )
            if acting_pat:
                is_act = model.new_bool_var(f"act_{d}")
                model.add(is_act == sum(sel[p] for p in acting_pat))
                acting_sel_all.append(is_act)
                # 액팅 없는 날 = 소폭 페널티 → 최소화하면 액팅 날이 늘어남(소프트)
                if req.weight_acting_day > 0:
                    acting_day_terms.append(1 - is_act)
        # 액팅 일수 하드 고정(A/B 혼합 비율 제어)
        if req.acting_days is not None and acting_sel_all:
            model.add(sum(acting_sel_all) == req.acting_days)
    else:
        for d in days:
            st = req.staffing_for(d)
            for s in WORK_SHIFTS:
                need = st.of(s).min if st is not None else req.min_staff.get(s)
                if need > 0:
                    model.add(sum(x[i, d, s] for i in counted) >= need)

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

    # (11) 팀별 최소 인원: 각 교대(D/E/N)에 팀당 team_min_staff명 이상
    if req.team_min_staff > 0:
        teams: dict[int, list[int]] = {}
        for i, nurse in enumerate(nurses):
            teams.setdefault(nurse.team, []).append(i)
        for d in days:
            for s in (Shift.DAY, Shift.EVENING, Shift.NIGHT):
                for members in teams.values():
                    model.add(
                        sum(x[i, d, s] for i in members) >= req.team_min_staff
                    )

    # (11b) F2: 트레이닝 신규는 교육자와 항상 같은 근무 (하드). 신규가 사전배정된
    #       날(연차 등)은 제외한다.
    preassigned_days: dict[int, set[int]] = {}
    for p in req.pre_assigned:
        if p.nurse_id in idx:
            preassigned_days.setdefault(idx[p.nurse_id], set()).add(p.day)
    for j, nurse in enumerate(nurses):
        if not nurse.is_trainee or not nurse.trainer_id or nurse.trainer_id not in idx:
            continue
        t = idx[nurse.trainer_id]
        # 트레이닝 신규가 나이트 불가면 나이트는 미러링에서 제외(교육자가 나이트인 날
        # 강제 동행 시 나이트 금지와 충돌해 전체 INFEASIBLE이 되는 것을 방지).
        mirror_shifts = ALL_SHIFTS if nurse.night_eligible else [
            s for s in ALL_SHIFTS if s != Shift.NIGHT]
        for d in days:
            if d in preassigned_days.get(j, set()):
                continue
            for s in mirror_shifts:
                model.add(x[j, d, s] == x[t, d, s])

    # (12) T6a: 단일(고립) 나이트 금지 — 나이트 블록 ≥ 2 (실측: 단일 N 0건)
    if req.enforce_night_block and nd >= 2:
        for i, nurse in enumerate(nurses):
            carry = req.carry_over.get(nurse.id, [])
            carry_n = bool(carry) and carry[-1] == Shift.NIGHT.value
            # 1일차: 앞이 나이트 이월이 아니면, N이면 다음날도 N
            if not carry_n:
                model.add(x[i, 1, Shift.NIGHT] == 1).only_enforce_if(
                    x[i, 0, Shift.NIGHT]
                )
            # 중간: N[d]이면 N[d-1] 또는 N[d+1] (말일은 다음 달로 이어질 수 있어 제외)
            for d in range(1, nd - 1):
                model.add_bool_or(
                    x[i, d, Shift.NIGHT].negated(),
                    x[i, d - 1, Shift.NIGHT],
                    x[i, d + 1, Shift.NIGHT],
                )

    # (13) 월 나이트 상한
    if req.max_nights_per_month is not None:
        for i in range(N):
            model.add(
                sum(x[i, d, Shift.NIGHT] for d in days) <= req.max_nights_per_month
            )

    # (13b) 나이트 하드 밴드: 가능자별 나이트 수를 [min, max]로 고정 → 공정성을 목적함수가
    #       아닌 하드 제약으로 보장(최적 증명 가속 → 정확 최적). exact_mode면 자동 산출.
    lo = req.night_min_per_nurse
    hi = req.night_max_per_nurse
    if req.exact_mode and lo is None and hi is None:
        # 패턴별 N이 다를 수 있으므로 하한은 최소-N 합, 상한은 최대-N 합으로 밴드를 잡아
        # (균일-N이면 동일) 이질적 패턴에서 밴드가 과도하게 좁아 INFEASIBLE 되는 것을 방지.
        total_n_lo = 0
        total_n_hi = 0
        for d in days:
            if req.daily_patterns:
                ns = [int(p.get("N", 0)) for p in req.daily_patterns]
                total_n_lo += min(ns)
                total_n_hi += max(ns)
            else:
                st = req.staffing_for(d)
                need = st.of(Shift.NIGHT).min if st is not None else req.min_staff.get(Shift.NIGHT)
                total_n_lo += need
                total_n_hi += need
        elig = sum(1 for nu in nurses if nu.night_eligible)
        if elig > 0 and total_n_hi > 0:
            import math as _math
            lo = _math.floor(total_n_lo / elig)
            hi = _math.ceil(total_n_hi / elig)
            if lo == hi:  # 폭 0이면 살짝 넓혀 실현 가능성 확보
                hi = lo + 1
    if lo is not None or hi is not None:
        for i, nurse in enumerate(nurses):
            if not nurse.night_eligible:
                continue
            tot = sum(x[i, d, Shift.NIGHT] for d in days)
            if lo is not None:
                model.add(tot >= lo)
            if hi is not None:
                model.add(tot <= hi)

    # (13d) 오프 수 하드 밴드(exact_mode): 목표 오프 T가 있으면 인당 오프를 [T, T+1]로 고정.
    #   합-편차 최소화(offcount 소프트)는 "1명 14 + 3명 12"와 "6명 12"를 구분하지 못해 특정
    #   인원에게 오프가 몰릴 수 있다. 하드 밴드로 전원 11~12 같은 균등 분포를 보장한다.
    if req.exact_mode:
        for i, nurse in enumerate(nurses):
            if nurse.is_trainee:
                continue
            # 인당 목표(off_count_target 우선, 없으면 달력 기본) — 소프트 항과 동일 기준 사용.
            _T = req.off_target_for(nurse.id)
            if _T is None:
                continue
            pre = preassigned_days.get(i, set())
            pure_off = [x[i, d, Shift.OFF] for d in days if d not in pre]
            if not pure_off:
                continue
            off_i = sum(pure_off)
            model.add(off_i >= _T)
            model.add(off_i <= _T + 1)

    # (13c) D·E·N 균형: 각 간호사에게 데이/이브닝/나이트가 한쪽으로 쏠리지 않게.
    #   - 하드: 세 근무의 개수 격차 ≤ max_shift_spread (exact_mode면 기본 3 자동)
    #   - 소프트: |D-E| 페널티(비-exact 모드). 트레이닝 신규는 교육자를 따라가므로 제외.
    balance_terms: list = []
    eff_spread = req.max_shift_spread
    if req.exact_mode and eff_spread is None:
        eff_spread = 3
    want_soft_bal = req.weight_shift_balance > 0 and not req.exact_mode
    if eff_spread is not None or want_soft_bal:
        for i, nurse in enumerate(nurses):
            if req.exclude_trainee_from_staffing and nurse.is_trainee:
                continue
            dc = model.new_int_var(0, nd, f"dcnt_{i}")
            ec = model.new_int_var(0, nd, f"ecnt_{i}")
            nc = model.new_int_var(0, nd, f"ncnt_{i}")
            model.add(dc == sum(x[i, d, Shift.DAY] for d in days))
            model.add(ec == sum(x[i, d, Shift.EVENING] for d in days))
            model.add(nc == sum(x[i, d, Shift.NIGHT] for d in days))
            if eff_spread is not None:
                pairs = [(dc, ec), (dc, nc), (ec, nc)] if nurse.night_eligible else [(dc, ec)]
                for a, b in pairs:
                    model.add(a - b <= eff_spread)
                    model.add(b - a <= eff_spread)
            if want_soft_bal:
                diff = model.new_int_var(0, nd, f"debal_{i}")
                model.add(diff >= dc - ec)
                model.add(diff >= ec - dc)
                balance_terms.append(diff)

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

    # (E4) 같은 팀 원티드 오프 겹침: 하드(옵트인) 또는 소프트(기본).
    #      실데이터 검증상 실제로는 원티드 우선으로 겹침을 허용하므로 기본은 소프트 감점.
    team_day_off: dict[tuple[int, int], list[int]] = {}
    for w in req.wanted:
        if w.nurse_id not in idx or w.shift != Shift.OFF:
            continue
        i = idx[w.nurse_id]
        team = nurses[i].team
        for d in w.days():
            team_day_off.setdefault((team, d), []).append(i)
    teamoff_terms: list = []
    for (_team, d), members in team_day_off.items():
        members = list(set(members))
        if len(members) < 2:
            continue
        if req.exclusive_team_wanted_off:
            model.add(sum(x[i, d, Shift.OFF] for i in members) <= 1)  # 하드
        elif req.weight_team_off_overlap > 0:
            excess = model.new_int_var(0, len(members), f"toff_{_team}_{d}")
            model.add(excess >= sum(x[i, d, Shift.OFF] for i in members) - 1)
            teamoff_terms.append(excess)

    # (소프트) 공정성: 나이트/총근무 편차
    # simple_fairness=True면 max-min '편차'(add_max/min_equality: reification으로 최적 증명이
    # 느림) 대신 '최댓값 최소화'(minimize-max: 상한 제약만, reification 없음)로 근사한다.
    # → 나이트가 특정인에게 몰리지 않게 하며, 최적해 증명이 훨씬 빨라진다(수렴 실험 §참고).
    fairness_terms: list[cp_model.IntVar] = []
    if req.weight_fairness > 0 and N > 1 and not req.exact_mode:
        # exact_mode에서는 공정성을 (13b) 하드 밴드로 보장하므로 편차 목적을 넣지 않는다
        #   (편차/최댓값 목적은 최적 증명을 느리게 만들기 때문 — EXPERIMENT_REPORT §7).
        night_counts, work_counts = [], []
        for i in range(N):
            nc = model.new_int_var(0, nd, f"night_{i}")
            model.add(nc == sum(x[i, d, Shift.NIGHT] for d in days))
            night_counts.append(nc)
            wc = model.new_int_var(0, nd, f"workcnt_{i}")
            model.add(wc == sum(work[i, d] for d in days))
            work_counts.append(wc)
        eligible = [night_counts[i] for i in range(N) if nurses[i].night_eligible]
        if req.simple_fairness:
            if len(eligible) > 1:
                n_max = model.new_int_var(0, nd, "night_max")
                for nc in eligible:
                    model.add(n_max >= nc)
                fairness_terms.append(n_max)  # 최댓값 최소화 → 하향 평준화
            w_max = model.new_int_var(0, nd, "work_max")
            for wc in work_counts:
                model.add(w_max >= wc)
            fairness_terms.append(w_max)
        else:
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

    # (소프트) 적정 인원 부족 감점 (INRC S1 방식)
    target_terms: list = []
    if req.weight_target_staff > 0 and req.staffing:
        for d in days:
            st = req.staffing_for(d)
            if st is None:
                continue
            for s in WORK_SHIFTS:
                tgt = st.of(s).target
                if tgt is None or tgt <= st.of(s).min:
                    continue
                short = model.new_int_var(0, tgt, f"short_{d}_{s.value}")
                model.add(short >= tgt - sum(x[i, d, s] for i in range(N)))
                target_terms.append(short)

    # (소프트) T6b: OFF-근무-OFF 고립 근무일 감점 (실측 월 3~4건 수준으로 희소해야 함)
    isolated_terms: list = []
    if req.weight_isolated_work > 0:
        for i in range(N):
            for d in range(1, nd - 1):
                b = model.new_bool_var(f"iso_{i}_{d}")
                model.add(
                    b >= x[i, d - 1, Shift.OFF] + work[i, d] + x[i, d + 1, Shift.OFF] - 2
                )
                isolated_terms.append(b)

    # (소프트) 오프 페어링: 오프 한 개만 끼는 것(근무-오프-근무) 지양 → 가급적 2개씩.
    pairoff_terms: list = []
    if req.weight_paired_off > 0:
        for i in range(N):
            for d in range(1, nd - 1):
                b = model.new_bool_var(f"isooff_{i}_{d}")
                model.add(
                    b >= work[i, d - 1] + x[i, d, Shift.OFF] + work[i, d + 1] - 2
                )
                pairoff_terms.append(b)

    # (소프트) 긴 텀(≥4일 연속근무) 직후 오프 1개만 지양 → 오프 2개 강하게 우대.
    #   4일 연속근무(d-3..d) 뒤 d+1 오프인데 d+2 다시 근무면 페널티(고립오프와 중첩되어 더 큼).
    longoff_terms: list = []
    if req.weight_paired_off_after_long > 0:
        for i in range(N):
            for d in range(3, nd - 2):
                b = model.new_bool_var(f"longoff_{i}_{d}")
                model.add(
                    b >= work[i, d - 3] + work[i, d - 2] + work[i, d - 1] + work[i, d]
                    + x[i, d + 1, Shift.OFF] + work[i, d + 2] - 5
                )
                longoff_terms.append(b)

    # (소프트) T11b: E-OFF-D 감점 (지양하되 허용, 실측 1인 월 ≤1건 수준)
    eod_terms: list = []
    if req.weight_eod > 0:
        for i in range(N):
            for d in range(nd - 2):
                b = model.new_bool_var(f"eod_{i}_{d}")
                model.add(
                    b
                    >= x[i, d, Shift.EVENING]
                    + x[i, d + 1, Shift.OFF]
                    + x[i, d + 2, Shift.DAY]
                    - 2
                )
                eod_terms.append(b)

    # (소프트) 지양 전이 감점: M→D, E→M (실측 각 1건 — 드물게만 허용)
    softtrans_terms: list = []
    if req.weight_soft_transition > 0:
        for i in range(N):
            for d in range(nd - 1):
                for a, c in SOFT_DISCOURAGED_TRANSITIONS:
                    b = model.new_bool_var(f"st_{i}_{d}_{a.value}{c.value}")
                    model.add(b >= x[i, d, a] + x[i, d + 1, c] - 1)
                    softtrans_terms.append(b)

    # (소프트) 달력주(월~일) 오프 ≥ 2 선호 (T2 재정의 — year/month 지정 시에만)
    weekoff_terms: list = []
    fw = req.first_weekday()
    if req.weight_week_off > 0 and fw is not None:
        weeks: list[list[int]] = []
        cur: list[int] = []
        for d in days:
            if (fw + d) % 7 == 0 and cur:  # 월요일에 새 주
                weeks.append(cur)
                cur = []
            cur.append(d)
        if cur:
            weeks.append(cur)
        for wk in weeks:
            if len(wk) != 7:  # 부분 주는 제외
                continue
            for i in range(N):
                short = model.new_int_var(0, 2, f"wkoff_{i}_{wk[0]}")
                model.add(short >= 2 - sum(x[i, d, Shift.OFF] for d in wk))
                weekoff_terms.append(short)

    # (소프트) 미드=저연차: 팀 내 상위권(1~3위)에 미드 배정 시 감점 (실측 상위권 0회)
    midsenior_terms: list = []
    if req.weight_mid_senior > 0:
        for i, nurse in enumerate(nurses):
            if nurse.seniority_rank is not None and nurse.seniority_rank <= 3:
                for d in days:
                    midsenior_terms.append(x[i, d, Shift.MID])

    # (소프트) 프리셉터 동행: 프리셉티가 근무하는 날 프리셉터가 같은 교대가 아니면 감점
    preceptor_terms: list = []
    if req.weight_preceptor > 0:
        for j, nurse in enumerate(nurses):
            if not nurse.preceptor_id or nurse.preceptor_id not in idx:
                continue
            p = idx[nurse.preceptor_id]
            for d in days:
                same = model.new_bool_var(f"pair_{j}_{d}")
                both = []
                for s in WORK_SHIFTS:
                    b = model.new_bool_var(f"pair_{j}_{d}_{s.value}")
                    model.add_bool_and(x[j, d, s], x[p, d, s]).only_enforce_if(b)
                    model.add(b <= x[j, d, s])
                    model.add(b <= x[p, d, s])
                    both.append(b)
                model.add_max_equality(same, both)
                miss = model.new_bool_var(f"pairmiss_{j}_{d}")
                model.add(miss >= work[j, d] - same)
                preceptor_terms.append(miss)

    # (소프트) E1: 월 오프 수 = 목표(주말+공휴일). 순수 오프(사전배정 연차 등 제외) 기준.
    #         트레이닝 신규는 교육자를 따라가므로 제외.
    offcount_terms: list = []
    if req.weight_off_count > 0:
        for i, nurse in enumerate(nurses):
            if nurse.is_trainee:
                continue
            target = req.off_target_for(nurse.id)
            if target is None:
                continue
            pre_days = preassigned_days.get(i, set())
            pure_off = [x[i, d, Shift.OFF] for d in days if d not in pre_days]
            if not pure_off:
                continue
            dev = model.new_int_var(0, nd, f"offdev_{i}")
            total_off = sum(pure_off)
            model.add(dev >= total_off - target)
            model.add(dev >= target - total_off)
            offcount_terms.append(dev)

    # (소프트) F1: 한 교대에 저연차만/고연차만 몰리지 않게 — 상위권(rank≤2)·하위권(rank≥4)
    #         부재 시 감점. (연차 정보가 있는 간호사에 한함)
    seniority_terms: list = []
    if req.weight_seniority_mix > 0:
        seniors = [i for i, nu in enumerate(nurses)
                   if nu.seniority_rank is not None and nu.seniority_rank <= 2]
        juniors = [i for i, nu in enumerate(nurses)
                   if nu.seniority_rank is not None and nu.seniority_rank >= 4]
        for d in days:
            for s in (Shift.DAY, Shift.EVENING, Shift.NIGHT):
                if seniors:
                    no_sen = model.new_bool_var(f"nosen_{d}_{s.value}")
                    model.add(sum(x[i, d, s] for i in seniors) == 0).only_enforce_if(no_sen)
                    model.add(sum(x[i, d, s] for i in seniors) >= 1).only_enforce_if(no_sen.negated())
                    seniority_terms.append(no_sen)
                if juniors:
                    no_jun = model.new_bool_var(f"nojun_{d}_{s.value}")
                    model.add(sum(x[i, d, s] for i in juniors) == 0).only_enforce_if(no_jun)
                    model.add(sum(x[i, d, s] for i in juniors) >= 1).only_enforce_if(no_jun.negated())
                    seniority_terms.append(no_jun)

    # (소프트) C3: 나이트 블록(≥2) 직후 오프 2개 원칙. 오프 1개도 허용하되 지양.
    nightkeep_terms: list = []
    if req.weight_night_keep > 0:
        for i in range(N):
            for d in range(1, nd - 1):
                # d에서 ≥2 나이트 블록이 끝남: N[d-1]=1, N[d]=1, N[d+1]=0
                be = model.new_bool_var(f"nblk_{i}_{d}")
                model.add(be <= x[i, d - 1, Shift.NIGHT])
                model.add(be <= x[i, d, Shift.NIGHT])
                model.add(be <= 1 - x[i, d + 1, Shift.NIGHT])
                model.add(
                    be >= x[i, d - 1, Shift.NIGHT] + x[i, d, Shift.NIGHT]
                    + (1 - x[i, d + 1, Shift.NIGHT]) - 2
                )
                for k in (1, 2):
                    if d + k < nd:
                        p = model.new_bool_var(f"nkeep_{i}_{d}_{k}")
                        model.add(p >= be - x[i, d + k, Shift.OFF])
                        nightkeep_terms.append(p)

    # (소프트) 근무 텀 5일(최대치) 지양 — 3~4일 텀 선호 (웹리서치: 연속근무 짧을수록 피로↓)
    longblock_terms: list = []
    if req.weight_long_block > 0:
        for i in range(N):
            for d in range(nd - 4):
                b = model.new_bool_var(f"long5_{i}_{d}")
                model.add(b >= sum(work[i, d + k] for k in range(5)) - 4)
                longblock_terms.append(b)

    # (소프트) 나이트 블록 사이 텀 확보 — 나이트 후 오프만 하고 바로 다시 나이트로 복귀(N…O…N)를
    #   지양. 중간에 데이/이브닝 근무가 끼면 페널티 없음(권장 패턴). 짧은 텀일수록 페널티 가중.
    nightgap_terms: list = []
    if req.weight_night_gap_work > 0:
        for i, nurse in enumerate(nurses):
            if not nurse.night_eligible:
                continue
            for d in range(nd):
                for g in (1, 2, 3):
                    e = d + g + 1
                    if e >= nd:
                        continue
                    # N[d]=1, d+1..d+g 모두 OFF, N[e]=1 이면 페널티(중간 근무 없이 나이트 복귀)
                    p = model.new_bool_var(f"ngap_{i}_{d}_{g}")
                    terms = [x[i, d, Shift.NIGHT], x[i, e, Shift.NIGHT]]
                    terms += [x[i, d + k, Shift.OFF] for k in range(1, g + 1)]
                    model.add(p >= sum(terms) - (len(terms) - 1))
                    # 텀이 짧을수록(4 - g) 가중 → 1일 텀이 가장 큰 페널티
                    for _w in range(4 - g):
                        nightgap_terms.append(p)

    # (소프트) 주말 오프 공정성 — 주말(토·일) 오프 수의 인당 편차 최소화 (웹리서치: 주말 공정 분배)
    weekendfair_terms: list = []
    fw2 = req.first_weekday()
    if req.weight_weekend_fair > 0 and fw2 is not None and N > 1:
        wke_counts = []
        for i in range(N):
            wc = model.new_int_var(0, nd, f"wke_{i}")
            model.add(wc == sum(x[i, d, Shift.OFF] for d in days if (fw2 + d) % 7 >= 5))
            wke_counts.append(wc)
        wmax = model.new_int_var(0, nd, "wke_max")
        wmin = model.new_int_var(0, nd, "wke_min")
        model.add_max_equality(wmax, wke_counts)
        model.add_min_equality(wmin, wke_counts)
        wspread = model.new_int_var(0, nd, "wke_spread")
        model.add(wspread == wmax - wmin)
        weekendfair_terms.append(wspread)

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
    if target_terms:
        objective.append(req.weight_target_staff * sum(target_terms))
    if isolated_terms:
        objective.append(req.weight_isolated_work * sum(isolated_terms))
    if pairoff_terms:
        objective.append(req.weight_paired_off * sum(pairoff_terms))
    if longoff_terms:
        objective.append(req.weight_paired_off_after_long * sum(longoff_terms))
    if eod_terms:
        objective.append(req.weight_eod * sum(eod_terms))
    if softtrans_terms:
        objective.append(req.weight_soft_transition * sum(softtrans_terms))
    if weekoff_terms:
        objective.append(req.weight_week_off * sum(weekoff_terms))
    if midsenior_terms:
        objective.append(req.weight_mid_senior * sum(midsenior_terms))
    if preceptor_terms:
        objective.append(req.weight_preceptor * sum(preceptor_terms))
    if offcount_terms:
        objective.append(req.weight_off_count * sum(offcount_terms))
    if seniority_terms:
        objective.append(req.weight_seniority_mix * sum(seniority_terms))
    if nightkeep_terms:
        objective.append(req.weight_night_keep * sum(nightkeep_terms))
    if teamoff_terms:
        objective.append(req.weight_team_off_overlap * sum(teamoff_terms))
    if longblock_terms:
        objective.append(req.weight_long_block * sum(longblock_terms))
    if nightgap_terms:
        objective.append(req.weight_night_gap_work * sum(nightgap_terms))
    if acting_day_terms:
        objective.append(req.weight_acting_day * sum(acting_day_terms))
    if weekendfair_terms:
        objective.append(req.weight_weekend_fair * sum(weekendfair_terms))
    if balance_terms:
        objective.append(req.weight_shift_balance * sum(balance_terms))
    # 결정적 타이브레이커(선택): 동일 품질(1차 목적) 해가 여럿일 때 유일해로 수렴시키기 위해
    #   1차 목적을 큰 배수로 우선(사전식), 그 아래 셀 위치·교대 순서로 정해진 미세 비용을 더한다.
    #   → '인위적'이지만 재현·수렴을 위한 결정적 규칙 (PLAN·보고서에 명시).
    obj_expr = sum(objective) if objective else None
    if req.deterministic_tiebreak:
        tb_terms = []
        for i in range(N):
            base = i * nd
            for d in days:
                wgt = base + d + 1
                for s in ALL_SHIFTS:
                    ord_s = TIEBREAK_ORDER[s]
                    if ord_s:
                        tb_terms.append((wgt * ord_s) * x[i, d, s])
        tb = sum(tb_terms) if tb_terms else 0
        if req.primary_max is not None:
            # 품질(1차 목적)이 primary_max로 이미 하드 고정됨 → 타이브레이커만 최소화(빠름·정확)
            obj_expr = tb
        else:
            # 단일 solve 사전식(느릴 수 있음): 품질을 큰 배수로 우선.
            #   배수는 tb의 이론적 최댓값보다 커야 사전식(1차 품질 우선)이 보장된다.
            #   하루 한 근무이므로 (i,d)당 최대 기여는 wgt*max_ord → 합의 상한이 tb 최댓값.
            max_ord = max(TIEBREAK_ORDER.values()) if TIEBREAK_ORDER else 1
            tb_max = sum(i * nd + d + 1 for i in range(N) for d in days) * max_ord
            big = tb_max + 1
            obj_expr = big * (sum(objective) if objective else 0) + tb

    # (실험) 1차 목적(품질) 상한 — 2단계 사전식 풀이: 품질을 최적값으로 고정 후 타이브레이크만
    if req.primary_max is not None and objective:
        model.add(sum(objective) <= req.primary_max)

    # (실험) 목적함수 상한 고정 — '동일 품질의 다른 해'만 탐색
    if req.objective_max is not None and obj_expr is not None:
        model.add(obj_expr <= req.objective_max)

    # (실험) 이전 해 금지(no-good) — 서로 다른 해 열거
    total_cells = N * nd
    for sol in req.forbidden_solutions:
        lits = []
        for nid, seq in sol.items():
            if nid not in idx:
                continue
            i = idx[nid]
            for d in days:
                if d < len(seq):
                    try:
                        lits.append(x[i, d, Shift(seq[d])])
                    except ValueError:
                        pass  # 알 수 없는 값은 무시
        if lits:
            model.add(sum(lits) <= total_cells - 1)

    if obj_expr is not None:
        model.minimize(obj_expr)

    # ---- 풀기 ----
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = req.time_limit_seconds
    solver.parameters.num_search_workers = req.num_workers
    if req.random_seed is not None:
        solver.parameters.random_seed = req.random_seed
        solver.parameters.randomize_search = True
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

    obj_val = int(round(solver.objective_value)) if obj_expr is not None else 0

    return ScheduleResponse(
        status=status_name,
        feasible=True,
        num_days=nd,
        schedules=schedules,
        unmet_preferences=unmet_pref,
        unmet_wanted_off=unmet_woff,
        objective_value=obj_val,
        message="근무표 생성 완료" + (" (최적해)" if status == cp_model.OPTIMAL else ""),
    )


def _forbid(res: ScheduleResponse) -> dict[str, list[str]]:
    return {s.nurse_id: [x.value for x in s.shifts] for s in res.schedules}


def solve_candidates(req: ScheduleRequest, count: int = 3) -> list[ScheduleResponse]:
    """동일 최적 품질의 '서로 다른' 근무표 후보를 최대 count개 생성한다 (파트장 선택용).

    첫 해의 목적값 O*를 상한으로 고정하고, 앞서 나온 해를 no-good으로 금지하며 재풀이한다.
    → 품질(목적함수)은 동일하고 배치만 다른 후보들. 더 못 만들면 개수가 줄어든다(품질 수렴).
    """
    first = solve(req)
    if not first.feasible:
        return [first]
    ostar = first.objective_value
    out = [first]
    forbidden = [_forbid(first)]
    for j in range(1, max(1, count)):
        nxt = solve(req.model_copy(update={
            "objective_max": ostar,
            "forbidden_solutions": list(forbidden),
            "random_seed": j * 7 + 1,
        }))
        if not nxt.feasible:
            break
        out.append(nxt)
        forbidden.append(_forbid(nxt))
    return out
