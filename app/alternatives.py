"""대원칙 대안 3안 생성기 (PLAN 0.2 워크플로우의 '대안 3안' 단계).

수동 수정된 근무표가 대원칙(하드 규칙)을 위반할 때, **대원칙을 하드로 강제**하면서
**원본 그리드에서 최소한만 바꾼** 복구안을 최대 3개 생성한다. 각 대안은 서로 다른 해가
되도록(no-good) 반복 최적화하며, 변경 칸 수가 적은 순으로 정렬한다.

- 하드: 하루 1근무, 회전 금지(N→D/E/M·E→D), N-OFF-D, 연속근무≤K, 연속나이트≤KN,
  단일 나이트 금지(옵션), 나이트 불가자, 전월 이월 경계, (있으면) 교대 최소 인원.
- 연차 등 비근무 코드 셀은 그대로 고정(변경 대상 아님).
- 목적: 원본과 다른 칸 수 최소화.
"""
from __future__ import annotations

from ortools.sat.python import cp_model
from pydantic import BaseModel, Field

from .config import (
    HARD_FORBIDDEN_GAP_PATTERNS,
    HARD_FORBIDDEN_TRANSITIONS,
)
from .models import Shift

_WORK = [Shift.DAY, Shift.MID, Shift.EVENING, Shift.NIGHT]
_ALL = [*_WORK, Shift.OFF]
_WORKV = {s.value for s in _WORK}
_CELLV = {s.value for s in _ALL}  # D/M/E/N/O


def _norm(cell: str) -> str:
    """셀 값을 D/M/E/N/O로 정규화. M/D·M/E는 미드(M). 비근무 코드는 O."""
    if cell in ("M/D", "M/E"):
        return "M"
    return cell if cell in _CELLV else "O"


def _is_leave(cell: str) -> bool:
    """연차 등 비근무 코드(고정 셀)인지 — D/M/E/N/O·M/D·M/E가 아니면 고정 휴무."""
    return cell not in _CELLV and cell not in ("M/D", "M/E")


class AltChange(BaseModel):
    nurse: str
    day: int  # 1부터
    frm: str
    to: str


class Alternative(BaseModel):
    grid: dict[str, list[str]]
    changed: int
    changes: list[AltChange]


class AlternativesRequest(BaseModel):
    schedules: dict[str, list[str]] = Field(..., description="간호사 이름 → 날짜별 근무(수정본)")
    carry_over: dict[str, list[str]] = Field(default_factory=dict)
    num_days: int | None = None
    max_consecutive_days: int = Field(5, ge=1)
    max_consecutive_nights: int = Field(3, ge=1)
    min_staff: dict[str, int] | None = Field(
        None, description='교대 최소 인원 {"D":4,"E":4,"N":4,"M":0} — 모든 날 동일 적용'
    )
    night_ineligible: list[str] = Field(default_factory=list, description="나이트 불가 간호사 이름")
    enforce_night_block: bool = True
    count: int = Field(3, ge=1, le=5)
    time_limit_seconds: float = Field(5.0, gt=0, le=30)


class AlternativesResponse(BaseModel):
    ok: bool
    alternatives: list[Alternative]
    relaxed_staffing: bool = False
    message: str = ""


def _trailing(seq: list[str], pred) -> int:
    k = 0
    for s in reversed(seq):
        if pred(s):
            k += 1
        else:
            break
    return k


def _build_and_solve(req, names, nd, orig_norm, fixed_leave, no_goods, use_staffing, time_limit):
    """모델 1회 빌드 후 풀기. 해(assignment dict) 또는 None 반환."""
    model = cp_model.CpModel()
    N = len(names)
    idx = {n: i for i, n in enumerate(names)}
    K = req.max_consecutive_days
    KN = req.max_consecutive_nights
    ineligible = set(req.night_ineligible)

    x = {}
    for i in range(N):
        for d in range(nd):
            for s in _ALL:
                x[i, d, s] = model.new_bool_var(f"x_{i}_{d}_{s.value}")

    for i in range(N):
        for d in range(nd):
            model.add_exactly_one(x[i, d, s] for s in _ALL)

    # 고정 휴무(연차 등)
    for (i, d) in fixed_leave:
        model.add(x[i, d, Shift.OFF] == 1)

    # 나이트 불가
    for i, n in enumerate(names):
        if n in ineligible:
            for d in range(nd):
                model.add(x[i, d, Shift.NIGHT] == 0)

    # 하드 회전 금지
    for i in range(N):
        for d in range(nd - 1):
            for a, b in HARD_FORBIDDEN_TRANSITIONS:
                model.add(x[i, d + 1, b] == 0).only_enforce_if(x[i, d, a])

    # N-OFF-D
    for i in range(N):
        for d in range(nd - 2):
            for a, b, c in HARD_FORBIDDEN_GAP_PATTERNS:
                model.add_bool_or(
                    x[i, d, a].negated(), x[i, d + 1, b].negated(), x[i, d + 2, c].negated()
                )

    # 근무 여부
    work = {}
    for i in range(N):
        for d in range(nd):
            work[i, d] = model.new_bool_var(f"w_{i}_{d}")
            model.add(work[i, d] == sum(x[i, d, s] for s in _WORK))

    # 연속 근무 / 연속 나이트
    for i in range(N):
        for st in range(nd - K):
            model.add(sum(work[i, st + k] for k in range(K + 1)) <= K)
        for st in range(nd - KN):
            model.add(sum(x[i, st + k, Shift.NIGHT] for k in range(KN + 1)) <= KN)

    # 전월 이월 경계
    for nid, seq in req.carry_over.items():
        if nid not in idx:
            continue
        i = idx[nid]
        seqn = [_norm(s) for s in seq]
        t = _trailing(seqn, lambda s: s in _WORKV)
        if t > 0:
            span = max(0, K - t + 1)
            if 0 < span <= nd:
                model.add(sum(work[i, d] for d in range(span)) <= span - 1)
        tn = _trailing(seqn, lambda s: s == "N")
        if tn > 0:
            span_n = max(0, KN - tn + 1)
            if 0 < span_n <= nd:
                model.add(sum(x[i, d, Shift.NIGHT] for d in range(span_n)) <= span_n - 1)
        if seqn:
            last = seqn[-1]
            for a, b in HARD_FORBIDDEN_TRANSITIONS:
                if last == a.value:
                    model.add(x[i, 0, b] == 0)
            for a, b, c in HARD_FORBIDDEN_GAP_PATTERNS:
                if len(seqn) >= 2 and seqn[-2] == a.value and seqn[-1] == b.value:
                    model.add(x[i, 0, c] == 0)
                if last == a.value and nd >= 2:
                    model.add_bool_or(x[i, 0, b].negated(), x[i, 1, c].negated())

    # 단일 나이트 금지(블록 ≥2)
    if req.enforce_night_block and nd >= 2:
        for i, n in enumerate(names):
            carry = [_norm(s) for s in req.carry_over.get(n, [])]
            carry_n = bool(carry) and carry[-1] == "N"
            if not carry_n:
                model.add(x[i, 1, Shift.NIGHT] == 1).only_enforce_if(x[i, 0, Shift.NIGHT])
            for d in range(1, nd - 1):
                model.add_bool_or(
                    x[i, d, Shift.NIGHT].negated(), x[i, d - 1, Shift.NIGHT], x[i, d + 1, Shift.NIGHT]
                )

    # 교대 최소 인원(옵션)
    if use_staffing and req.min_staff:
        for d in range(nd):
            for s in _WORK:
                need = int(req.min_staff.get(s.value, 0))
                if need > 0:
                    model.add(sum(x[i, d, s] for i in range(N)) >= need)

    # no-good: 이전 해와 동일 금지
    total = N * nd
    for assign in no_goods:
        model.add(
            sum(x[i, d, Shift(assign[i][d])] for i in range(N) for d in range(nd)) <= total - 1
        )

    # 목적: 원본과 다른 칸 최소화(고정 셀 제외)
    keep_terms = []
    for i in range(N):
        for d in range(nd):
            if (i, d) in fixed_leave:
                continue
            keep_terms.append(x[i, d, Shift(orig_norm[i][d])])
    # 유지 칸을 최대화 = 변경 최소화
    model.maximize(sum(keep_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    assign = []
    for i in range(N):
        row = []
        for d in range(nd):
            for s in _ALL:
                if solver.value(x[i, d, s]) == 1:
                    row.append(s.value)
                    break
        assign.append(row)
    return assign


def generate(req: AlternativesRequest) -> AlternativesResponse:
    names = list(req.schedules.keys())
    if not names:
        return AlternativesResponse(ok=False, alternatives=[], message="근무표가 비어 있습니다.")
    nd = req.num_days or max(len(v) for v in req.schedules.values())

    orig_label = [["O"] * nd for _ in names]
    orig_norm = [["O"] * nd for _ in names]
    fixed_leave: set[tuple[int, int]] = set()
    for i, n in enumerate(names):
        row = req.schedules[n]
        for d in range(nd):
            cell = row[d] if d < len(row) else "O"
            orig_label[i][d] = cell
            orig_norm[i][d] = _norm(cell)
            if _is_leave(cell):
                fixed_leave.add((i, d))

    relaxed = False
    no_goods: list[list[list[str]]] = []
    alts: list[Alternative] = []
    use_staffing = req.min_staff is not None

    for _k in range(req.count):
        assign = _build_and_solve(req, names, nd, orig_norm, fixed_leave, no_goods,
                                  use_staffing, req.time_limit_seconds)
        if assign is None and use_staffing and not no_goods:
            # 인원 제약 때문에 불가능하면 완화 후 재시도
            use_staffing = False
            relaxed = True
            assign = _build_and_solve(req, names, nd, orig_norm, fixed_leave, no_goods,
                                      use_staffing, req.time_limit_seconds)
        if assign is None:
            break
        no_goods.append(assign)
        # 변경 내역 계산
        changes: list[AltChange] = []
        grid: dict[str, list[str]] = {}
        for i, n in enumerate(names):
            out_row: list[str] = []
            for d in range(nd):
                if (i, d) in fixed_leave:
                    out_row.append(orig_label[i][d])  # 고정 셀 원본 유지
                    continue
                new_s = assign[i][d]
                if new_s == orig_norm[i][d]:
                    out_row.append(orig_label[i][d])  # 미변경 → 원 라벨(M/D 등) 유지
                else:
                    out_row.append(new_s)
                    changes.append(AltChange(nurse=n, day=d + 1, frm=orig_label[i][d], to=new_s))
            grid[n] = out_row
        alts.append(Alternative(grid=grid, changed=len(changes), changes=changes))

    alts.sort(key=lambda a: a.changed)
    if not alts:
        return AlternativesResponse(
            ok=False, alternatives=[], relaxed_staffing=relaxed,
            message="대원칙을 지키는 대안을 찾지 못했습니다. 제약(인원·연속 등)을 완화해 보세요.",
        )
    msg = f"{len(alts)}개의 대안을 찾았습니다 (변경 칸 최소순)."
    if relaxed:
        msg += " ※ 교대 최소 인원은 충족할 수 없어 완화했습니다."
    return AlternativesResponse(ok=True, alternatives=alts, relaxed_staffing=relaxed, message=msg)
