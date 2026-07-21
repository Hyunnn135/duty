# 🏥 간호사 근무표 자동 생성 (Nurse Duty Scheduler)

3교대(Day/Evening/Night) 간호사 근무표(듀티표)를 제약조건 최적화로 자동 생성하는 웹 애플리케이션입니다.
Google **OR-Tools CP-SAT** 솔버로 복잡한 근무 규칙을 만족하는 근무표를 계산합니다.

## 주요 기능

- **3교대 배정**: Day(D) / Evening(E) / Night(N) / Off(O)
- **핵심 규칙 자동 준수**
  - 간호사는 하루에 하나의 교대에만 배정
  - 교대별 하루 최소 필요 인원 보장
  - 나이트 근무 다음 날은 D/E 배정 금지 (나이트 후 휴식)
  - 연속 근무일 / 연속 나이트 상한
  - 간호사별 최소 오프 일수 보장
- **개인 희망 반영**
  - `prefer`: 되도록 반영 (소프트 제약)
  - `forbid`: 반드시 준수 (하드 제약, 예: 승인된 연차)
- **공정성**: 나이트·총 근무일 편차를 최소화해 고르게 분배

## 기술 스택

| 영역 | 사용 기술 |
|------|-----------|
| 최적화 엔진 | OR-Tools CP-SAT |
| 백엔드 API | FastAPI |
| 데이터 검증 | Pydantic v2 |
| 프론트엔드 | Vanilla HTML/CSS/JS (단일 페이지) |
| 테스트 | pytest |

## 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 개발 서버 실행
uvicorn app.main:app --reload

# 브라우저에서 http://127.0.0.1:8000 접속
```

## 프로젝트 구조

```
duty/
├── app/
│   ├── models.py        # 요청/응답 데이터 모델 (Pydantic)
│   ├── scheduler.py     # CP-SAT 근무표 생성 엔진 (핵심)
│   ├── main.py          # FastAPI 서버 & API 엔드포인트
│   └── static/
│       └── index.html   # 웹 UI
├── tests/
│   └── test_scheduler.py
├── requirements.txt
└── README.md
```

## API

### `POST /api/schedule`

근무표를 생성합니다.

**요청 예시**

```json
{
  "num_days": 14,
  "nurses": [
    {"id": "n0", "name": "김민지"},
    {"id": "n1", "name": "이서준"}
  ],
  "min_staff": {"D": 2, "E": 2, "N": 1},
  "max_consecutive_days": 5,
  "max_consecutive_nights": 3,
  "min_off_days": 4,
  "requests": [
    {"nurse_id": "n0", "day": 2, "shift": "O", "type": "prefer"}
  ]
}
```

**응답 예시**

```json
{
  "status": "OPTIMAL",
  "feasible": true,
  "num_days": 14,
  "schedules": [
    {"nurse_id": "n0", "name": "김민지", "shifts": ["D","E","O","..."], "counts": {"D":4,"E":3,"N":3,"O":4}}
  ],
  "unmet_preferences": 0,
  "message": "근무표 생성 완료 (최적해)"
}
```

## 테스트

```bash
python -m pytest tests/ -q
```

## 제약조건 상세

| 구분 | 규칙 |
|------|------|
| 하드 | 하루 1교대, 교대별 최소 인원, 나이트 후 D/E 금지, 연속근무/나이트 상한, 최소 오프, forbid 요청 |
| 소프트 | prefer 요청 반영, 나이트·근무일 공정 분배 |

소프트 제약은 목적 함수의 가중치(`weight_preference`, `weight_fairness`)로 조정할 수 있습니다.
