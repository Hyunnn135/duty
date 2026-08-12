"""61병동 실제 근무예정표(2026년 7·8월) 전사 원본 데이터 + 검산/내보내기.

사진(수기 근무표)을 셀 단위로 전사하고, 각 행의 우측 집계값(D/E/N/M/S/S1/HY/PH/AH/OFF)
으로 자동 검산한다. 검산을 통과한 데이터를 CSV/JSON으로 내보낸다.

근무 코드
  D/E/N/M : 데이(07-15)/이브닝(15-22)/나이트(22-07)/미드
  OFF     : 오프(휴무)
  S/S1    : 파트장 상근(관리자 근무)
  HY      : 연차(휴가)
  경조     : 경조사 휴가
  M/D,M/E : 초록 형광 표시 복합·수정 근무(집계 D/E/N/M엔 미포함, 근무시간에만 일부 반영)
  .       : 전출 등으로 해당 월 근무 없음(빈칸)

주의
  - 파트장(김은미) 행은 관리자 상근 행으로 원본 집계 자체가 31일과 맞지 않으며
    S/S1 표기가 근사치다. D/E/N 스케줄링 대상이 아니므로 검산에서 제외한다.
  - 김에스더/김민신은 7/13 71병동으로 부서이동(7월 일부만 근무, 8월 명단 없음).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

SUMMARY_COLS = ["D", "E", "N", "M", "S", "S1", "HY", "PH", "AH", "OFF"]
MANAGER = "김은미"  # 파트장, 검산 제외

# (사번, 이름, 팀, "31일 셀(공백구분)", (D,E,N,M,S,S1,HY,PH,AH,OFF), 근무시간)
JUL = [
("100360","김은미",1,"S S1 S S1 OFF S S S S S OFF OFF S S S S S1 S1 OFF S S S S S OFF OFF S S S S S",(0,0,0,0,21,1,0,0,0,6),176),
("100275","신유진",1,"D D OFF E E E E OFF D OFF N N N OFF OFF D E E E OFF N N N OFF OFF D D D OFF D D",(9,7,6,0,0,0,0,0,0,9),176),
("100265","이정욱",1,"OFF D D D OFF D D D N N OFF OFF E E E E HY OFF D E OFF D D N N N OFF OFF D E OFF",(10,6,5,0,0,0,1,0,0,9),176),
("100373","장예진",1,"E OFF N N N OFF OFF E E E E E OFF HY N N N OFF OFF D D D D OFF D D E E OFF OFF E",(6,9,6,0,0,0,1,0,0,9),176),
("100268","한지호",1,"HY E E HY OFF N N N OFF OFF HY D D D D OFF D D OFF OFF E E E E E OFF N N N OFF OFF",(6,7,6,0,0,0,3,0,0,9),176),
("100274","한지원",1,"N N OFF OFF D D D D OFF D D D HY OFF E E OFF N N N OFF OFF OFF D E E OFF E E N N",(8,6,7,0,0,0,1,0,0,9),176),
("100591","구민주",1,"E M M N N OFF OFF E HY D D OFF OFF N N N OFF OFF E E E E OFF N N OFF OFF M/D D D D",(5,6,7,2,0,0,1,0,0,9),168),
("100698","김에스더",1,"D D OFF E E E E OFF D OFF 경조 N . . . . . . . . . . . . . . . . . . .",(3,4,1,0,0,0,0,0,0,3),64),
("100704","강유빈",1,"OFF D D D OFF D D D N N OFF OFF E E E E OFF D D E OFF D D N N N OFF OFF D E OFF",(11,6,5,0,0,0,0,0,0,9),176),
("100264","장현진",2,"D E E E OFF D D D E OFF D D OFF OFF N N OFF OFF E E E E E OFF N N N OFF OFF N N",(6,9,7,0,0,0,0,0,0,9),176),
("100503","한혜수",2,"OFF HY HY D D N N OFF OFF D E OFF E N N OFF OFF D N N N OFF OFF D E E OFF E E E E",(5,8,7,0,0,0,2,0,0,9),176),
("100269","정서인",2,"N N OFF E E HY HY N N N OFF HY D D D OFF N N OFF OFF D D D OFF D D OFF D D OFF OFF",(10,2,7,0,0,0,3,0,0,9),176),
("100612","문수연",2,"E OFF D N N OFF E E E OFF E E OFF OFF OFF D D D D D OFF N N N OFF OFF E M/E M/E N N",(6,7,7,0,0,0,0,0,0,9),160),
("100602","유현석",2,"N N N OFF OFF E E OFF D E N N N OFF OFF M/E E E OFF D D E OFF D OFF OFF D D D D D",(9,6,6,0,0,0,0,0,0,9),168),
("100604","권희원",2,"D D OFF D E HY HY N N N OFF OFF D D E E E OFF OFF N N OFF E M/E OFF D D N N OFF OFF",(7,5,7,0,0,0,2,0,0,9),168),
("100589","유수미",2,"HY HY D M OFF N N OFF OFF E E E E E OFF D N N N OFF OFF M/D OFF E N N OFF OFF M/D M/D M/D",(2,6,7,1,0,0,2,0,0,9),144),
("100703","김민신",2,"OFF E E E OFF D D D E OFF D D . . . . . . . . . . . . . . . . . . .",(5,4,0,0,0,0,0,0,0,3),72),
("100266","안현영",3,"OFF D E E HY N N OFF OFF D HY OFF D D D D D OFF N N OFF OFF E E E N N OFF OFF E E",(7,7,6,0,0,0,2,0,0,9),176),
("100417","채수빈",3,"OFF OFF D D D D D OFF D E HY OFF N N N OFF OFF E E E E OFF D D D D OFF N N N OFF",(10,5,6,0,0,0,1,0,0,9),176),
("100378","이지은",3,"E OFF N N N OFF OFF D D N N N OFF OFF D E E OFF D D OFF E E E OFF E E E E E OFF",(5,11,6,0,0,0,0,0,0,9),176),
("100271","김유진",3,"D E OFF HY E E E E E OFF D D OFF E E N N N OFF OFF D D N N N OFF OFF OFF OFF D D",(7,8,6,0,0,0,1,0,0,9),176),
("100453","강고은",3,"OFF E E HY D E OFF N N OFF E E E E HY OFF D D D OFF N N N OFF OFF OFF D D D OFF N",(7,7,6,0,0,0,2,0,0,9),176),
("100603","성시은",3,"N N N OFF OFF M M OFF OFF M N N N OFF OFF OFF D E M/E M/E M/E OFF D D D E E OFF E E E",(4,6,6,3,0,0,0,0,0,9),152),
("100705","강소영",3,"OFF D E E OFF N N OFF OFF D D OFF D D D D OFF D N N HY OFF E E E N N OFF OFF E E",(8,7,6,0,0,0,1,0,0,9),176),
("100699","양연주",3,"D OFF D D D D D OFF D E OFF OFF N N N OFF OFF E E E OFF M/D M/D D D D OFF N N N OFF",(10,4,6,0,0,0,0,0,0,9),160),
]

AUG = [
("100360","김은미",1,"OFF OFF S S S S S S1 OFF S S S S S S OFF S1 OFF S S S S S1 OFF S S S S OFF OFF S",(0,0,0,0,20,1,0,0,0,9),168),
("100275","신유진",1,"OFF OFF D D D OFF D D OFF D D D D OFF E N N OFF OFF OFF E E E OFF OFF OFF D D N N N",(11,4,5,0,0,0,0,0,0,11),160),
("100265","이정욱",1,"N N N OFF OFF D E E OFF OFF E E N N OFF OFF OFF D D D D D OFF E E E E OFF OFF OFF E",(6,9,5,0,0,0,0,0,0,11),160),
("100373","장예진",1,"E E OFF N N N OFF OFF E E OFF OFF OFF D D D D OFF E E E OFF OFF N N N OFF OFF D D D",(7,7,6,0,0,0,0,0,0,11),160),
("100268","한지호",1,"D D E E E OFF N N N OFF OFF E E E OFF OFF OFF N N N OFF OFF OFF D D D OFF E E E OFF",(5,9,6,0,0,0,0,0,0,11),160),
("100274","한지원",1,"OFF OFF E E E E OFF D D N N N OFF OFF D E E E OFF OFF N N N OFF OFF OFF D D D HY OFF",(6,7,6,0,0,0,1,0,0,11),160),
("100604","권희원",1,"D D D OFF D D OFF E OFF HY OFF N N OFF OFF D D E E E OFF D D OFF OFF E N N N OFF OFF",(9,5,5,0,0,0,1,0,0,11),160),
("100591","구민주",1,"E OFF D D OFF OFF D OFF D D OFF E E OFF N N N OFF OFF OFF D D M D D OFF OFF HY HY N N",(9,3,5,1,0,0,2,0,0,11),160),
("100704","강유빈",1,"M M N N N OFF OFF E E E OFF OFF E E E E OFF D D D M OFF OFF OFF D D N N OFF OFF OFF",(5,7,5,3,0,0,0,0,0,11),160),
("100264","장현진",2,"OFF OFF D D D OFF OFF E E E E E OFF N N N OFF OFF OFF OFF E E N N N OFF OFF D D D D",(7,7,6,0,0,0,0,0,0,11),160),
("100503","한혜수",2,"E OFF OFF D N N N OFF OFF OFF D D D D OFF D E E E E OFF D D D OFF N N N OFF OFF OFF",(9,5,6,0,0,0,0,0,0,11),160),
("100269","정서인",2,"D D N N OFF OFF E HY OFF OFF N N N OFF OFF OFF D D D D D OFF OFF OFF D D D OFF E E E",(10,4,5,0,0,0,1,0,0,11),160),
("100612","문수연",2,"OFF OFF OFF HY OFF D D D D D OFF OFF E E E OFF N N N OFF OFF OFF E E E E E OFF N N N",(5,8,6,0,0,0,1,0,0,11),160),
("100602","유현석",2,"OFF E E E E E OFF N N N OFF OFF OFF D D E OFF OFF HY N N N OFF OFF OFF D E E OFF E E",(3,10,6,0,0,0,1,0,0,11),160),
("100589","유수미",2,"N N OFF OFF D E OFF D D D E OFF OFF N N N OFF OFF OFF OFF E E E E E OFF D D E OFF D",(7,8,5,0,0,0,0,0,0,11),160),
("100266","안현영",3,"N N OFF OFF E N N OFF OFF E E E OFF D D D D OFF OFF N N N OFF OFF OFF D D E HY OFF E",(6,6,7,0,0,0,1,0,0,11),160),
("100417","채수빈",3,"OFF D D HY OFF D E N N N OFF OFF E E OFF OFF E E E E OFF OFF D D D OFF N N N OFF OFF",(6,7,6,0,0,0,1,0,0,11),160),
("100378","이지은",3,"OFF OFF E E OFF OFF D D E OFF D D D N N OFF OFF D D D D D OFF N N N OFF OFF OFF E E",(10,5,5,0,0,0,0,0,0,11),160),
("100271","김유진",3,"D OFF N N N OFF OFF OFF D D D D OFF OFF D E OFF OFF OFF E E E E E OFF OFF E E E N N",(6,9,5,0,0,0,0,0,0,11),160),
("100453","강고은",3,"OFF OFF OFF D D E E E OFF OFF N N N OFF OFF M E N N N OFF OFF OFF D E E OFF D D D D",(7,6,6,1,0,0,0,0,0,11),160),
("100603","성시은",3,"E E OFF OFF OFF D D OFF N N N OFF OFF E E OFF D D D D D OFF N N N OFF OFF OFF D D HY",(9,4,6,0,0,0,1,0,0,11),160),
("100705","강소영",3,"N N OFF OFF E E E OFF E E E OFF D D M OFF N N N OFF OFF OFF D E E N N OFF OFF E OFF",(3,9,7,1,0,0,0,0,0,11),160),
("100699","양연주",3,"OFF E E E OFF N N N OFF OFF D D D OFF OFF OFF E E E OFF N N N OFF OFF E E E OFF D D",(5,9,6,0,0,0,0,0,0,11),160),
]


def validate(rows, ndays=31):
    """전사 셀을 집계값과 대조. 불일치 행 목록 반환(파트장 제외)."""
    fails = []
    for emp, name, team, cells, summ, hrs in rows:
        cl = cells.split()
        if len(cl) != ndays:
            fails.append((name, f"길이 {len(cl)}≠{ndays}"))
            continue
        if name == MANAGER:
            continue  # 관리자 상근 행은 원본 집계가 비정형이라 제외
        cnt = {k: 0 for k in SUMMARY_COLS}
        for c in cl:
            if c in cnt:
                cnt[c] += 1
        exp = dict(zip(SUMMARY_COLS, summ))
        mism = [f"{k}({cnt[k]}≠{exp[k]})" for k in SUMMARY_COLS if cnt[k] != exp[k]]
        if mism:
            fails.append((name, ",".join(mism)))
    return fails


def to_csv(rows, path, ndays=31):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["team", "empno", "name"] + [str(d) for d in range(1, ndays + 1)])
        for emp, name, team, cells, summ, hrs in rows:
            w.writerow([team, emp, name] + cells.split())


def to_json(path):
    def pack(rows):
        out = []
        for emp, name, team, cells, summ, hrs in rows:
            out.append({
                "empno": emp, "name": name, "team": team,
                "shifts": cells.split(),
                "summary": dict(zip(SUMMARY_COLS, summ)),
                "work_hours": hrs,
                "manager": name == MANAGER,
            })
        return out
    doc = {
        "ward": "61병동",
        "part_manager": "김은미",
        "shift_codes": {
            "D": "데이 07:00-15:00", "E": "이브닝 15:00-22:00",
            "N": "나이트 22:00-07:00", "M": "미드", "OFF": "오프",
            "S": "파트장 상근", "S1": "파트장 상근(변형)",
            "HY": "연차", "경조": "경조사 휴가",
            "M/D": "복합/수정 근무(형광)", "M/E": "복합/수정 근무(형광)",
            ".": "해당 월 근무 없음(전출 등)",
        },
        "teams": {"count": 3, "per_team": "약 7~8명",
                   "note": "굵은 가로선 기준 팀 블록. 7월 9/8/8, 8월 9/6/8"},
        "months": {
            "2026-07": {"first_weekday": "수", "days": 31, "roster": pack(JUL)},
            "2026-08": {"first_weekday": "토", "days": 31, "roster": pack(AUG)},
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    here = Path(__file__).parent
    ok = True
    for label, rows in [("2026-07", JUL), ("2026-08", AUG)]:
        fails = validate(rows)
        if fails:
            ok = False
            print(f"✗ {label} 검산 실패:")
            for n, m in fails:
                print(f"   - {n}: {m}")
        else:
            print(f"✅ {label} 전 행 집계 일치 ({len(rows)}명, 파트장 제외)")
    to_csv(JUL, here / "schedule_2026_07.csv")
    to_csv(AUG, here / "schedule_2026_08.csv")
    to_json(here / "schedule.json")
    print("→ schedule_2026_07.csv / schedule_2026_08.csv / schedule.json 생성 완료")
