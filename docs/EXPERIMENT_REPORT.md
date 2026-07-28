# 실데이터 원티드 기반 근무표 수렴 실험 보고서 (2026-08)

실제 8월 원티드(오프) 신청 데이터(`data/wanted_2026_08.json`, 22명·26건)를 반영해 근무표를
생성하고, 실제 8월 근무표와 정량 비교하며 알고리즘을 라운드별로 개선한 기록이다.
하네스: `scripts/experiment.py` (재현: `python -m scripts.experiment`).

> 전제(사용자 결정): ① 연차(HY)는 예외로 제외(입력·생성 모두 안 씀, 실제 HY는 비교 시 오프로
> 간주). ② 원티드 100% 반영이 최우선. 근무 텀 3–4일 선호. 그 외 간호사 교대근무 선호는 웹
> 리서치로 보강. ③ E4 충돌은 실제 결과를 정답으로 삼아 조정. ④ 결정적 타이브레이커 허용(단
> 인위 요소 명시). ⑤ 한 번에 진행 후 최종 보고.

---

## 1. 웹 리서치 — 간호사 교대근무 선호 (중점 반영 내용)

근거 조사에서 다음을 실제 알고리즘에 반영했다:

| 선호(근거) | 반영 방식 |
|---|---|
| **포워드 로테이션** D→E→N (시계방향이 생체리듬 적응에 유리) | 역회전(N→D/E, E→D) 이미 하드 금지 + 소프트 전이(M→D·E→M) 감점 → 실측 위반 0 유지 |
| **연속 나이트 ≤3, 나이트 후 ≥48h(오프 2) 휴식** | 하드 P5(≤3) + 소프트 C3(블록 직후 오프2) — 기존 반영 |
| **연속근무 짧게(피로↓)**, 사용자 선호 **3–4일 텀** | **신규**: 5일 블록(최대치) 소프트 감점 `weight_long_block` → 3–4일 텀 유도 |
| **주말·야간 등 비선호 근무의 공정 분배** | 나이트 균등(기존) + **신규**: 주말 오프 공정성 `weight_weekend_fair` |
| **자율성·투명성(원티드 반영이 만족·잔류의 핵심)** | **원티드 오프 최우선** 가중치 50→200으로 상향 → 100% 반영 보장 |

출처:
- [Exploring nurse perspectives on AI-based shift scheduling (BMC Nursing)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12406402/)
- [Integrating Nurse Preferences Into AI-Based Scheduling (JMIR)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12157959/)
- [Healthiest shift work schedule — forward rotation](https://www.xenia.team/articles/healthiest-shift-work-schedule)
- [NIOSH Work Organization Strategies for Nurses](https://www.cdc.gov/niosh/work-hour-training-for-nurses/longhours/mod5/06.html)
- [How to schedule night shift work to reduce risks (PMC)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7737811/)
- [Building a Better Nurse Schedule (NurseRegistry)](https://www.nurseregistry.com/blog/building-a-better-nurse-schedule/)

---

## 2. E4(팀 오프 겹침) — 실데이터 발견

원티드 데이터에서 **팀2의 한혜수·정서인이 8일·9일에 동시 오프 신청**(E4 충돌 2건).
실제 8월 근무표를 보면:

- 한혜수: 8일 OFF, 9일 OFF
- 정서인: 8일 **HY(연차)**, 9일 OFF

→ **실제 파트장은 E4를 절대 규칙으로 지키지 않았다.** 원티드를 우선해 둘 다 쉬게 하되, 한
명(정서인)은 연차로 커버했다. 즉 "팀 오프 겹침 절대 금지"는 실제로는 **원티드 우선 + 커버리지
확보(팀 최소 인원)** 라는 목적의 heuristic이며, 커버리지는 이미 `team_min_staff`로 보장된다.

**조치**: E4를 **하드 → 소프트**로 변경(`exclusive_team_wanted_off` 기본 False,
`weight_team_off_overlap` 소프트 감점). 하드가 필요하면 옵트인 가능. (2차 면담에서 파트장께
"겹침 절대 금지"의 정확한 취지 재확인 권장.)

---

## 3. 라운드별 진행 (생성물 5개 vs 실제)

지표는 22명×31일 그리드 기준. ↑좋음/↓나쁨은 실제 대비 방향.

### Round 0 — 기준(변경 전 알고리즘, E4 하드)
- 원티드 92.3%(E4 하드가 겹침 2건 차단) · 5일블록 28–37(실제 24) · 3–4일블록 57–70(실제 77)
- ✅ 이미 우수: 나이트 편차·EOD(0)·고립근무(0)·팀커버(0미스)
- **문제**: 원티드 미달, 텀이 실제보다 길다.

### Round 1 — E4 소프트화 + 5일블록 감점 + 주말공정성
- 원티드 96–100% · 5일블록 0–1 · 3–4일블록 98–109 · 주말오프 편차 개선
- 잔여: gen1이 원티드 96.2%(1건 양보) → 원티드 최우선 미흡.

### Round 2 — 원티드 가중치 50→200
- **5개 전부 원티드 100%** · 하드 0 · 오프=목표(10) · 5일블록≈0 · 3–4일블록 100–110
- EOD 0 / 고립 0 / 팀커버 0미스 — 모든 소프트 지표에서 실제(EOD 14·고립 4·팀커버 1미스)를
  **동등 이상**으로 능가. 단 아직 서로 다른 최적해가 다수(비수렴).

### Round 3 — 수렴(결정적 타이브레이커)
<!-- CONVERGENCE_SECTION -->
(작성 중)

---

## 4. 실제 대비 최종 평가 — 잘못된 점 / 더 나은 점
<!-- FINAL_EVAL -->
(작성 중)

## 5. 알고리즘 변경 요약 (커밋 반영)
<!-- CHANGES -->
(작성 중)

## 6. 인위적(결정적) 요소 상세
<!-- ARTIFICIAL -->
(작성 중)
