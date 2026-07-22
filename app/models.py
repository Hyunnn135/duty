"""근무표 생성 요청/응답에 사용되는 데이터 모델 정의.

Phase 1 확장:
  - 미드(M) 교대 추가 (D → M → E → N 순방향 랭크)
  - 비근무 코드(LeaveCode: 연차 HY·공가·병가·Edu·경조·PH·AH) — 사전 배정용
  - 간호사 속성: 팀(고정, A안)·팀 내 경력순위·나이트 가능·신규·프리셉터 연결
  - 월 컨텍스트: year/month → 일수·요일·주말 자동 산출, 공휴일 수동 입력
  - 전월 이월(carry_over): 월초 연속근무·연속나이트·경계 회전 계산용
  - 사전 배정(pre_assigned): 연차 등 확정 휴무를 하드 고정
  - 원티드 신청(wanted): D/E/N/OFF 기간 신청, 오프 최우선 가중치

기존(Phase 0) 요청 형식은 그대로 유효하다 — 새 필드는 모두 기본값을 가진다.
"""
from __future__ import annotations

import calendar
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class Shift(str, Enum):
    """근무 교대 종류. O는 오프(휴무)를 의미한다."""

    DAY = "D"
    MID = "M"
    EVENING = "E"
    NIGHT = "N"
    OFF = "O"


# 실제 근무(오프 제외)에 해당하는 교대 목록 (순방향 회전 순서: D → M → E → N)
WORK_SHIFTS: list[Shift] = [Shift.DAY, Shift.MID, Shift.EVENING, Shift.NIGHT]


class LeaveCode(str, Enum):
    """비근무 코드(사전 확정 휴무). 스케줄링에서는 오프처럼 취급하되 표시는 코드로."""

    HY = "HY"  # 연차
    GONGGA = "공가"
    BYEONGGA = "병가"
    EDU = "Edu"
    GYEONGJO = "경조"
    PH = "PH"
    AH = "AH"


class Nurse(BaseModel):
    """간호사 한 명. 팀은 고정 속성(A안), 명단 순서=경력·능력순."""

    id: str
    name: str
    team: int = Field(1, ge=1, description="소속 팀 (고정 속성)")
    seniority_rank: int | None = Field(
        None, ge=1, description="팀 내 경력·능력 순위 (1=최고참). 미드 배정 등에 사용"
    )
    night_eligible: bool = Field(
        True, description="나이트 가능 여부. 신규·임신(산후 1년 미만) 시 False"
    )
    is_new: bool = Field(False, description="신규 여부 (입사 N개월 미만)")
    preceptor_id: str | None = Field(
        None, description="(신규인 경우) 프리셉터 간호사 id — 같은 근무 배정 소프트 제약"
    )


class RequestType(str, Enum):
    """개인 희망의 종류."""

    PREFER = "prefer"  # 되도록 반영 (소프트 제약)
    FORBID = "forbid"  # 반드시 지킴, 예: 승인된 연차 (하드 제약)


class ShiftRequest(BaseModel):
    """특정 간호사가 특정 날짜에 특정 교대를 원하거나(prefer) 배정 금지(forbid)."""

    nurse_id: str
    day: int = Field(..., ge=0, description="0부터 시작하는 날짜 인덱스")
    shift: Shift
    type: RequestType = RequestType.PREFER


class WantedRequest(BaseModel):
    """원티드 신청: D/E/N/OFF를 하루 또는 연속 기간으로 신청 (소프트).

    오프 신청은 최우선 가중치(weight_wanted_off), D/E/N은 낮은 가중치로 반영된다.
    """

    nurse_id: str
    start_day: int = Field(..., ge=0, description="0부터 시작하는 시작일 인덱스")
    end_day: int | None = Field(None, ge=0, description="종료일 인덱스(포함). None=하루")
    shift: Shift = Shift.OFF
    reason: str = ""

    @model_validator(mode="after")
    def _range_ok(self) -> "WantedRequest":
        if self.end_day is not None and self.end_day < self.start_day:
            raise ValueError("end_day는 start_day 이상이어야 합니다.")
        return self

    def days(self) -> range:
        end = self.start_day if self.end_day is None else self.end_day
        return range(self.start_day, end + 1)


class PreAssigned(BaseModel):
    """사전 배정(하드 고정): 승인된 연차·공가 등. 해당 날은 근무 배정 불가."""

    nurse_id: str
    day: int = Field(..., ge=0)
    code: LeaveCode


class ShiftStaff(BaseModel):
    """한 교대의 최소(하드)/적정(소프트 목표) 인원."""

    min: int = Field(0, ge=0)
    target: int | None = Field(None, ge=0, description="적정 인원(부족분 감점). None=미사용")


class DayStaffing(BaseModel):
    """하루의 교대별 인원 기준."""

    D: ShiftStaff = Field(default_factory=ShiftStaff)
    E: ShiftStaff = Field(default_factory=ShiftStaff)
    N: ShiftStaff = Field(default_factory=ShiftStaff)
    M: ShiftStaff = Field(default_factory=ShiftStaff)

    def of(self, shift: Shift) -> ShiftStaff:
        return {
            Shift.DAY: self.D,
            Shift.EVENING: self.E,
            Shift.NIGHT: self.N,
            Shift.MID: self.M,
        }[shift]


class MinStaff(BaseModel):
    """(단순 모드) 교대별 하루 최소 필요 인원. staffing 미지정 시 모든 날에 적용."""

    D: int = Field(1, ge=0)
    E: int = Field(1, ge=0)
    N: int = Field(1, ge=0)
    M: int = Field(0, ge=0)

    def get(self, shift: Shift) -> int:
        return {
            Shift.DAY: self.D,
            Shift.EVENING: self.E,
            Shift.NIGHT: self.N,
            Shift.MID: self.M,
        }[shift]


DAY_CATEGORIES = ("weekday", "weekend", "holiday")

# carry_over에 허용되는 값 (비근무 코드는 'O'로 넣는다)
_CARRY_OK = {s.value for s in Shift}


class ScheduleRequest(BaseModel):
    """근무표 생성 요청.

    날짜 지정은 둘 중 하나:
      - num_days 직접 지정 (요일 정보 없음 → 모든 날을 weekday로 취급)
      - year+month 지정 → 일수·요일·주말 자동 산출 (num_days 생략 가능)
    """

    year: int | None = Field(None, ge=2000, le=2100)
    month: int | None = Field(None, ge=1, le=12)
    num_days: int | None = Field(None, ge=1, le=62, description="배정할 총 일수")

    nurses: list[Nurse] = Field(..., min_length=1)

    # 인원 기준: staffing(요일 구분) 우선, 없으면 min_staff(모든 날 동일)
    min_staff: MinStaff = Field(default_factory=MinStaff)
    staffing: dict[str, DayStaffing] | None = Field(
        None, description='요일별 기준 {"weekday":…, "weekend":…, "holiday":…}'
    )
    holidays: list[int] = Field(
        default_factory=list, description="공휴일 (1부터 시작하는 날짜 번호)"
    )

    max_consecutive_days: int = Field(5, ge=1, description="연속 근무 최대 일수")
    max_consecutive_nights: int = Field(3, ge=1, description="연속 나이트 최대 일수")
    min_off_days: int = Field(0, ge=0, description="기간 내 간호사별 최소 오프 일수")

    requests: list[ShiftRequest] = Field(default_factory=list)
    wanted: list[WantedRequest] = Field(default_factory=list)
    pre_assigned: list[PreAssigned] = Field(default_factory=list)
    carry_over: dict[str, list[str]] = Field(
        default_factory=dict,
        description="간호사 id → 전월 말일 근무 나열(오래된 날 → 마지막 날). 예: ['E','N','N']",
    )

    # 목적 함수 가중치 (클수록 우선순위 높음)
    weight_preference: int = Field(10, ge=0, description="개인 희망 미반영 페널티")
    weight_fairness: int = Field(3, ge=0, description="근무 배분 불공정 페널티")
    weight_wanted_off: int = Field(50, ge=0, description="원티드 오프 미반영 페널티(최우선)")
    weight_wanted_work: int = Field(5, ge=0, description="원티드 D/E/N 미반영 페널티(낮음)")

    time_limit_seconds: float = Field(15.0, gt=0, le=120)

    # ---- 검증 ----
    @field_validator("nurses")
    @classmethod
    def _unique_ids(cls, v: list[Nurse]) -> list[Nurse]:
        ids = [n.id for n in v]
        if len(ids) != len(set(ids)):
            raise ValueError("간호사 id가 중복되었습니다.")
        return v

    @field_validator("staffing")
    @classmethod
    def _staffing_keys(cls, v):
        if v is not None:
            bad = set(v) - set(DAY_CATEGORIES)
            if bad:
                raise ValueError(f"staffing 키는 {DAY_CATEGORIES} 만 가능합니다: {bad}")
        return v

    @field_validator("carry_over")
    @classmethod
    def _carry_values(cls, v: dict[str, list[str]]):
        for nid, seq in v.items():
            bad = [s for s in seq if s not in _CARRY_OK]
            if bad:
                raise ValueError(f"carry_over[{nid}]에 알 수 없는 근무 {bad} (허용: {sorted(_CARRY_OK)})")
        return v

    @model_validator(mode="after")
    def _resolve_days(self) -> "ScheduleRequest":
        if self.num_days is None:
            if self.year is None or self.month is None:
                raise ValueError("num_days 또는 (year, month) 중 하나는 지정해야 합니다.")
            self.num_days = calendar.monthrange(self.year, self.month)[1]
        nd = self.num_days
        ids = {n.id for n in self.nurses}
        for p in self.pre_assigned:
            if p.day >= nd:
                raise ValueError(f"pre_assigned day {p.day} 가 기간({nd}일)을 벗어납니다.")
        for w in self.wanted:
            if w.start_day >= nd or (w.end_day is not None and w.end_day >= nd):
                raise ValueError("wanted 기간이 배정 기간을 벗어납니다.")
        for nid in self.carry_over:
            if nid not in ids:
                raise ValueError(f"carry_over의 간호사 id '{nid}' 가 명단에 없습니다.")
        return self

    # ---- 달력 도우미 ----
    def first_weekday(self) -> int | None:
        """1일의 요일 (0=월 … 6=일). year/month 미지정 시 None."""
        if self.year is None or self.month is None:
            return None
        return date(self.year, self.month, 1).weekday()

    def day_category(self, d: int) -> str:
        """d(0-based)일의 분류: holiday > weekend > weekday."""
        if (d + 1) in self.holidays:
            return "holiday"
        fw = self.first_weekday()
        if fw is not None and (fw + d) % 7 >= 5:
            return "weekend"
        return "weekday"

    def staffing_for(self, d: int) -> DayStaffing | None:
        """d일에 적용할 요일별 기준. staffing 미지정 시 None(min_staff 사용)."""
        if not self.staffing:
            return None
        cat = self.day_category(d)
        return self.staffing.get(cat) or self.staffing.get("weekday")


class NurseSchedule(BaseModel):
    """간호사 한 명의 기간 전체 근무 배정 결과."""

    nurse_id: str
    name: str
    shifts: list[Shift]  # 날짜별 교대 (길이 = num_days)
    labels: list[str]  # 표시용 라벨 (기본은 교대 값, 사전 배정은 'HY' 등 코드)
    counts: dict[str, int]  # 라벨별 총 횟수 {"D":…, "E":…, "N":…, "M":…, "O":…, "HY":…}


class ScheduleResponse(BaseModel):
    """근무표 생성 결과."""

    status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN"
    feasible: bool
    num_days: int
    schedules: list[NurseSchedule]
    unmet_preferences: int
    unmet_wanted_off: int = 0
    message: str = ""
