"""실데이터 기반 근무표 수렴 실험 하네스.

실제 8월 원티드(오프) 신청 + 실제 명단/팀/경력/전월이월/인력기준을 넣고, 현재 알고리즘으로
서로 다른 근무표를 최대 k개 생성한 뒤, 실제 8월 근무표와 정량 비교한다. 알고리즘을
라운드마다 수정하며, '동일 품질의 다른 최적해'가 더는 없을 때(유일해) 수렴한다.

사용: python -m scripts.experiment            (한 라운드 실행·비교 출력)
      python -m scripts.experiment --k 5 --tl 10
연차(HY)는 예외로 제외한다(사용자 결정 1) — 실제 HY는 비교 시 오프로 간주.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from app.models import (
    DayStaffing, Nurse, ScheduleRequest, Shift, ShiftStaff, WantedRequest,
)
from app.rules_check import ValidateRequest, check
from app.scheduler import solve

ROOT = Path(__file__).resolve().parent.parent
D = json.loads((ROOT / "data" / "schedule.json").read_text(encoding="utf-8"))
W = json.loads((ROOT / "data" / "wanted_2026_08.json").read_text(encoding="utf-8"))
YEAR, MONTH, NDAYS, HOLIDAYS = 2026, 8, 31, [15, 17]  # 광복절 + 대체공휴일(8/17)
TIEBREAK = False  # 사전식 타이브레이커는 규모상 비현실적(보고서 §6) → 기본 다양화/품질 모드
SIMPLE_FAIR = False  # 공정성을 minimize-max로 근사(최적 증명 가속) — 수렴 실험에서 True


def norm(c: str) -> str:
    return "M" if c in ("M/D", "M/E") else (c if c in ("D", "E", "N", "M") else "O")


def aug_rows():
    return [r for r in D["months"]["2026-08"]["roster"] if not r.get("manager")]


def real_grid():
    return {r["name"]: [norm(c) for c in r["shifts"]] for r in aug_rows()}


def july_carry_by_id():
    jul = {r["name"]: r["shifts"] for r in D["months"]["2026-07"]["roster"] if not r.get("manager")}
    return {r["empno"]: [norm(c) for c in jul[r["name"]][-7:]] for r in aug_rows() if r["name"] in jul}


def carry_by_name():
    jul = {r["name"]: r["shifts"] for r in D["months"]["2026-07"]["roster"] if not r.get("manager")}
    return {r["name"]: [norm(c) for c in jul[r["name"]][-7:]] for r in aug_rows() if r["name"] in jul}


def first_weekday_mon() -> int:
    from datetime import date
    return (date(YEAR, MONTH, 1).weekday())  # 0=Mon


def is_weekend(day0: int) -> bool:
    return (first_weekday_mon() + day0) % 7 >= 5


def off_target() -> int:
    wk = sum(1 for d in range(NDAYS) if is_weekend(d))
    extra = sum(1 for h in HOLIDAYS if not is_weekend(h - 1))
    return wk + extra


# 단독근무 능력 (파트장 피드백): 각 팀 막내 신규는 팀 듀티를 혼자 감당하지 못한다.
#   모두 불가: 한예은(1팀)·최연우·정현서(3팀) / 나이트만 단독 가능: 윤시우(1팀)·전시은(2팀)
SOLO_NONE = {"한예은", "최연우", "정현서"}
SOLO_NIGHT_ONLY = {"윤시우", "전시은"}


def build_request(**over) -> ScheduleRequest:
    rows = aug_rows()
    team_seen: dict[int, int] = {}
    nurses = []
    for r in rows:
        team_seen[r["team"]] = team_seen.get(r["team"], 0) + 1
        nm = r["name"]
        sd = se = sn = True
        if nm in SOLO_NONE:
            sd = se = sn = False
        elif nm in SOLO_NIGHT_ONLY:
            sd = se = False
        nurses.append(Nurse(id=r["empno"], name=nm, team=r["team"],
                            seniority_rank=team_seen[r["team"]], night_eligible=True,
                            solo_day=sd, solo_evening=se, solo_night=sn))
    wd = DayStaffing(D=ShiftStaff(min=5, target=5), E=ShiftStaff(min=5, target=5),
                     N=ShiftStaff(min=4, target=4), M=ShiftStaff(min=0))
    we = DayStaffing(D=ShiftStaff(min=4, target=4), E=ShiftStaff(min=4, target=4),
                     N=ShiftStaff(min=4, target=4), M=ShiftStaff(min=0))
    staffing = {"weekday": wd, "weekend": we, "holiday": wd}
    wanted = [WantedRequest(nurse_id=req["empno"], start_day=day - 1, shift=Shift.OFF)
              for req in W["requests"] for day in req["off_days"]]
    kw = dict(year=YEAR, month=MONTH, nurses=nurses, staffing=staffing, holidays=HOLIDAYS,
              carry_over=july_carry_by_id(), max_consecutive_days=5, max_consecutive_nights=3,
              max_nights_per_month=7, team_min_staff=1, enforce_night_block=True,
              wanted=wanted, deterministic_tiebreak=TIEBREAK, simple_fairness=SIMPLE_FAIR,
              time_limit_seconds=10)
    kw.update(over)
    return ScheduleRequest(**kw)


def grid_of(res):
    return {s.name: [x.value for x in s.shifts] for s in res.schedules}


def forbid_of(res):
    return {s.nurse_id: [x.value for x in s.shifts] for s in res.schedules}


def generate_distinct(k=5, tl=10, **cfg):
    """서로 다른 해를 최대 k개 열거. TIEBREAK 여부로 방식이 다르다.
    반환: (grids, O*, is_unique, status)."""
    if not TIEBREAK:
        # 다양화 모드: 동일 1차 최적값에서 서로 다른 해를 열거
        r0 = solve(build_request(time_limit_seconds=tl, **cfg))
        if not r0.feasible:
            return [], None, True, r0.status
        ostar = r0.objective_value
        grids = [grid_of(r0)]
        forbidden = [forbid_of(r0)]
        for j in range(1, k):
            rj = solve(build_request(time_limit_seconds=tl, objective_max=ostar,
                                     forbidden_solutions=list(forbidden), random_seed=j * 7 + 3, **cfg))
            if not rj.feasible:
                break
            grids.append(grid_of(rj))
            forbidden.append(forbid_of(rj))
        return grids, ostar, len(grids) == 1, r0.status

    # 수렴 모드(2단계 사전식). 증명 가능한 유일성을 위해 넉넉한 시간 사용.
    ctl = max(tl, 30)
    #  A. 타이브레이크 OFF로 1차 품질 최적값 P* 확보 (OPTIMAL 증명 필요 → 넉넉히)
    rA = solve(build_request(time_limit_seconds=max(ctl, 60), deterministic_tiebreak=False))
    pstar = rA.objective_value
    #  B. 품질을 P*로 고정하고 타이브레이커만 최소화 → 유일 후보 C
    rB = solve(build_request(time_limit_seconds=ctl, primary_max=pstar))
    C = grid_of(rB); Fstar = rB.objective_value
    #  C. 같은 품질·같은 풀오브젝트에서 C 말고 다른 해가 있나? 없으면 유일해.
    rC = solve(build_request(time_limit_seconds=ctl, primary_max=pstar,
                             objective_max=Fstar, forbidden_solutions=[forbid_of(rB)]))
    unique = (rC.status == "INFEASIBLE")  # 증명된 유일성만 인정
    print(f"[수렴판정] A(품질)status={rA.status} P*={pstar} | "
          f"B(타이브레이크)status={rB.status} F*={Fstar} | "
          f"C(2번째해 존재?)status={rC.status} → 유일해={unique}")
    grids = [C] if unique else [C, grid_of(rC)]
    return grids, pstar, unique, rB.status


# ---------------- 지표 ----------------

WANTED = [(req["name"], day) for req in W["requests"] for day in req["off_days"]]
TEAM = {r["name"]: r["team"] for r in aug_rows()}
RANK = {}
_seen: dict[int, int] = {}
for _r in aug_rows():
    _seen[_r["team"]] = _seen.get(_r["team"], 0) + 1
    RANK[_r["name"]] = _seen[_r["team"]]


def _runs(seq):
    out, run = [], 0
    for c in seq:
        if c != "O":
            run += 1
        elif run:
            out.append(run); run = 0
    if run:
        out.append(run)
    return out


def metrics(grid: dict[str, list[str]]) -> dict:
    names = list(grid.keys())
    nd = len(next(iter(grid.values())))
    cby = carry_by_name()
    viol = check(ValidateRequest(schedules=grid, carry_over=cby,
                                 max_consecutive_days=5, max_consecutive_nights=3)).violations
    wf = sum(1 for (n, day) in WANTED if grid.get(n) and grid[n][day - 1] == "O")
    nights = {n: grid[n].count("N") for n in names}
    offs = {n: grid[n].count("O") for n in names}
    tgt = off_target()
    # 근무 텀
    blocks = [b for n in names for b in _runs(grid[n])]
    long5 = sum(1 for b in blocks if b >= 5)
    iso1 = sum(1 for b in blocks if b == 1)
    good34 = sum(1 for b in blocks if b in (3, 4))
    # 소프트 위반
    eod = iso_wo = strans = 0
    for n in names:
        s = grid[n]
        for d in range(nd - 2):
            if s[d] == "E" and s[d + 1] == "O" and s[d + 2] == "D":
                eod += 1
        for d in range(1, nd - 1):
            if s[d - 1] == "O" and s[d] != "O" and s[d + 1] == "O":
                iso_wo += 1
        for d in range(nd - 1):
            if (s[d], s[d + 1]) in (("M", "D"), ("E", "M")):
                strans += 1
    # 주말 오프 공정성
    wke = {n: sum(1 for d in range(nd) if is_weekend(d) and grid[n][d] == "O") for n in names}
    # 팀 커버리지 미스
    teams: dict[int, list[str]] = {}
    for n in names:
        teams.setdefault(TEAM[n], []).append(n)
    miss = 0
    for d in range(nd):
        for sh in ("D", "E", "N"):
            for mem in teams.values():
                if not any(grid[m][d] == sh for m in mem):
                    miss += 1
    nl = list(nights.values())
    return {
        "hard": len(viol),
        "wanted_fulfilled": f"{wf}/{len(WANTED)}",
        "wanted_pct": round(wf / len(WANTED) * 100, 1),
        "night_spread": max(nl) - min(nl),
        "night_std": round(statistics.pstdev(nl), 2),
        "off_off_target": sum(1 for n in names if offs[n] != tgt),
        "blocks_len>=5": long5, "blocks_len==1": iso1, "blocks_3or4": good34,
        "EOD": eod, "isolated_work": iso_wo, "soft_transition": strans,
        "weekend_off_spread": max(wke.values()) - min(wke.values()),
        "team_cover_miss": miss,
    }


def similarity(grid: dict[str, list[str]]) -> float:
    real = real_grid()
    m = t = 0
    for n, seq in grid.items():
        for d, c in enumerate(seq):
            t += 1
            if real.get(n) and real[n][d] == c:
                m += 1
    return round(m / t * 100, 1) if t else 0.0


def print_round(k=5, tl=10, **cfg):
    grids, ostar, unique, status = generate_distinct(k=k, tl=tl, **cfg)
    print(f"\n=== 생성 결과: {len(grids)}개 (O*={ostar}, status={status}, 유일해={unique}) ===")
    real = real_grid()
    cols = ["hard", "wanted_pct", "night_spread", "night_std", "off_off_target",
            "blocks_len>=5", "blocks_len==1", "blocks_3or4", "EOD", "isolated_work",
            "soft_transition", "weekend_off_spread", "team_cover_miss"]
    header = ["metric"] + [f"gen{i+1}" for i in range(len(grids))] + ["REAL"]
    print("off_target(월오프):", off_target())
    rm = metrics(real)
    rows_out = []
    gms = [metrics(g) for g in grids]
    for c in cols:
        rows_out.append([c] + [str(gm[c]) for gm in gms] + [str(rm[c])])
    w = [max(len(str(x)) for x in col) for col in zip(header, *rows_out)]
    def line(vals): return " | ".join(str(v).rjust(w[i]) for i, v in enumerate(vals))
    print(line(header))
    print("-" * (sum(w) + 3 * len(w)))
    for r in rows_out:
        print(line(r))
    print("\n유사도(실제와 셀 일치율):", [similarity(g) for g in grids])
    return grids, unique


def definitive(tl=30):
    """결정적 모드: num_workers=1 + 고정 시드 → 재현 가능한 단일 근무표.
    (사전식 타이브레이커는 22×31 규모에서 계산적으로 풀리지 않아 불채택 — 보고서 §6.)
    두 번 풀어 완전히 동일한지(=어떤 실행이든 같은 결과) 확인한다."""
    def run():
        return solve(build_request(time_limit_seconds=tl, deterministic_tiebreak=False,
                                   num_workers=1, random_seed=0))
    r1 = run()
    r2 = run()
    g1, g2 = grid_of(r1), grid_of(r2)
    if not g1:
        print(f"해 없음 status={r1.status}"); return False, {}
    identical = g1 == g2
    print(f"결정적 2회 실행 동일?: {identical}  (status={r1.status}, obj={r1.objective_value})")
    m = metrics(g1); rm = metrics(real_grid())
    print("\n지표(결정적 단일해 vs 실제):")
    for c in ["hard", "wanted_pct", "night_spread", "night_std", "off_off_target",
              "blocks_len>=5", "blocks_3or4", "EOD", "isolated_work", "soft_transition",
              "weekend_off_spread", "team_cover_miss"]:
        print(f"  {c:22} 결정해={m[c]:>6}   실제={rm[c]}")
    print("유사도(실제와 일치율):", similarity(g1))
    return identical, g1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--tl", type=float, default=10)
    ap.add_argument("--mode", choices=["round", "converge"], default="round")
    a = ap.parse_args()
    if a.mode == "converge":
        definitive(tl=a.tl)
    else:
        print_round(k=a.k, tl=a.tl)
