"""솔버 검증 (A): 실제 근무표에 우리 규칙(T1~T13)을 대조해 위반을 집계한다.

목적: 우리가 PLAN.md에 세운 규칙이 실제 근무표와 맞는지 확인한다.
 - 위반 0건  → 규칙이 현실과 일치 (하드 제약 후보)
 - 위반 다수 → 규칙이 너무 엄격하거나 예외가 많음 (소프트/재검토 대상)

파트장(관리자) 행과 비근무 코드(HY/경조/OFF 등)는 적절히 제외/처리한다.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

WORK_LETTERS = {"D", "E", "N", "M"}
REST_CODES = {"OFF", "HY", "경조", "PH", "AH"}  # 휴무성 코드(오프+연차 등)
# 월별 1일의 요일 (Mon=0 .. Sun=6): 7월1일=수, 8월1일=토
FIRST_WD = {"2026-07": 2, "2026-08": 5}


def is_rest(cell: str) -> bool:
    return cell in REST_CODES


def mon_weeks(month: str, ndays: int) -> list[list[int]]:
    """달력주(월요일 시작)로 날짜를 묶는다(부분주 포함). 반환: 1-based 날짜 리스트들."""
    base = FIRST_WD.get(month, 0)
    weeks, cur = [], []
    for d in range(1, ndays + 1):
        if (base + (d - 1)) % 7 == 0 and cur:  # 월요일에 새 주 시작
            weeks.append(cur); cur = []
        cur.append(d)
    if cur:
        weeks.append(cur)
    return weeks


def eff_shift(cell: str) -> str | None:
    """그날의 '실제 근무 교대'. 근무가 아니면 None.
    복합근무 M/D→D, M/E→E 로 간주(원본 집계 관행 반영)."""
    if cell in WORK_LETTERS:
        return cell
    if cell in ("M/D", "M/E"):
        return cell.split("/")[1]
    return None  # OFF, HY, 경조, PH, AH, S, S1, '.'


def is_work(cell: str) -> bool:
    return eff_shift(cell) is not None


def is_off(cell: str) -> bool:
    """순수 오프. 연차(HY)·경조 등은 '휴무'지만 OFF와는 구분."""
    return cell == "OFF"


def analyze_month(label: str, roster: list[dict], ndays: int = 31) -> None:
    nurses = [n for n in roster if not n["manager"]]
    week_under2 = 0   # 달력주 오프+연차 < 2 (소프트 지표)
    week_total = 0
    week_ex: list[str] = []

    counts = Counter()          # 규칙별 위반 건수
    examples: dict[str, list] = {}
    def add(rule, who, detail):
        counts[rule] += 1
        examples.setdefault(rule, [])
        if len(examples[rule]) < 4:
            examples[rule].append(f"{who} {detail}")

    night_totals = []
    max_work_streaks = []
    max_night_streaks = []

    for n in nurses:
        name = n["name"]
        s = n["shifts"]
        # 실제 근무한 날만(전출 '.' 제외) 대상
        active = [i for i, c in enumerate(s) if c != "."]
        if not active:
            continue

        # 나이트 총량(공정성)
        night_totals.append((name, sum(1 for c in s if eff_shift(c) == "N")))

        # 연속 근무/나이트 streak
        wstreak = nstreak = mws = mns = 0
        for c in s:
            if is_work(c):
                wstreak += 1; mws = max(mws, wstreak)
            else:
                wstreak = 0
            if eff_shift(c) == "N":
                nstreak += 1; mns = max(mns, nstreak)
            else:
                nstreak = 0
        max_work_streaks.append((name, mws))
        max_night_streaks.append((name, mns))

        for d in range(ndays - 1):
            a, b = s[d], s[d + 1]
            if a == "." or b == ".":
                continue
            sa, sb = eff_shift(a), eff_shift(b)
            # T7: 나이트 다음날 D/E/M (나이트 후 휴식 위반)
            if sa == "N" and sb in {"D", "E", "M"}:
                add("T7 나이트후 D/E/M", name, f"{d+1}→{d+2}일 N→{sb}")
            # 역회전 E→D
            if sa == "E" and sb == "D":
                add("역회전 E→D", name, f"{d+1}→{d+2}일 E→D")

        for d in range(ndays - 2):
            trio = s[d], s[d + 1], s[d + 2]
            if "." in trio:
                continue
            s0, s1, s2 = (eff_shift(x) for x in trio)
            # T11 지양: N-OFF-D, E-OFF-D
            if s0 == "N" and trio[1] == "OFF" and s2 == "D":
                add("T11 NOD (N-OFF-D)", name, f"{d+1}~{d+3}일")
            if s0 == "E" and trio[1] == "OFF" and s2 == "D":
                add("T11 EOD (E-OFF-D)", name, f"{d+1}~{d+3}일")
            # T6 고립 단일근무: OFF-근무-OFF
            if trio[0] == "OFF" and is_work(trio[1]) and trio[2] == "OFF":
                add("T6 고립근무 (OFF-근무-OFF)", name, f"{d+2}일 {trio[1]} 고립")

        # T3(하드): 7일 슬라이딩 윈도우 근무 ≤6 (= 7일마다 최소 1 휴무)
        for start in range(ndays - 6):
            win = [c for c in s[start:start + 7] if c != "."]
            if len(win) == 7 and sum(1 for c in win if is_work(c)) > 6:
                add("T3 주 근무>6", name, f"{start+1}~{start+7}일")

        # T2(소프트): 달력주(월~일) 오프+연차 ≥2 선호 — 하드 아님(§4.4 참고)
        for days in mon_weeks(label, ndays):
            if len(days) != 7 or any(s[d - 1] == "." for d in days):
                continue
            week_total += 1
            if sum(1 for d in days if is_rest(s[d - 1])) < 2:
                week_under2 += 1
                if len(week_ex) < 4:
                    week_ex.append(f"{name} {days[0]}~{days[-1]}일")

    # ---- 출력 ----
    print(f"\n{'='*60}\n■ {label}  (간호사 {len(nurses)}명, 파트장 제외)\n{'='*60}")
    print("\n[규칙 위반 집계]")
    ordered = [
        "T7 나이트후 D/E/M", "역회전 E→D", "T11 NOD (N-OFF-D)",
        "T11 EOD (E-OFF-D)", "T6 고립근무 (OFF-근무-OFF)",
        "T3 주 근무>6",
    ]
    for r in ordered:
        c = counts.get(r, 0)
        mark = "✅" if c == 0 else "⚠️ "
        ex = ("  예: " + " / ".join(examples[r])) if c else ""
        print(f"  {mark} {r:28s}: {c}건{ex}")

    print("\n[연속 근무일 최대] (T1/연속근무 상한 관련)")
    ws = Counter(m for _, m in max_work_streaks)
    print("  분포:", dict(sorted(ws.items())),
          "| >5인 사람:", [f"{n}({m})" for n, m in max_work_streaks if m > 5] or "없음")

    print("\n[연속 나이트 최대] (T8 상한 관련)")
    ns = Counter(m for _, m in max_night_streaks)
    print("  분포:", dict(sorted(ns.items())),
          "| >3인 사람:", [f"{n}({m})" for n, m in max_night_streaks if m > 3] or "없음")

    print("\n[T2 주간 오프 — 소프트 지표] 달력주(월~일) 오프+연차 ≥2")
    ratio = (week_total - week_under2) / week_total * 100 if week_total else 0
    print(f"  완전주 {week_total}개 중 <2인 주 {week_under2}개 (충족률 {ratio:.0f}%)"
          f" → 하드 아님, 소프트 선호로 처리")
    if week_ex:
        print("  예:", " / ".join(week_ex))

    print("\n[나이트 배분 공정성]")
    vals = [v for _, v in night_totals]
    if vals:
        lo = min(night_totals, key=lambda x: x[1])
        hi = max(night_totals, key=lambda x: x[1])
        print(f"  1인 평균 {sum(vals)/len(vals):.1f}개, 최소 {lo[1]}({lo[0]}) ~ 최대 {hi[1]}({hi[0]})")


if __name__ == "__main__":
    doc = json.loads((Path(__file__).parent / "schedule.json").read_text(encoding="utf-8"))
    for label, m in doc["months"].items():
        analyze_month(label, m["roster"], m["days"])
    print("\n※ 해석: '✅ 0건'인 규칙 = 실제 표에서 항상 지켜짐 → 하드 제약 후보.")
    print("   '⚠️ 다수'인 규칙 = 예외가 존재 → 소프트로 두거나 규칙 재검토 필요.")
