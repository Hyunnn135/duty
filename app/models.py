"""근무표 생성 요청/응답에 사용되는 데이터 모델 정의."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Shift(str, Enum):
    """근무 교대 종류. O는 오프(휴무)를 의미한다."""

    DAY = "D"
    EVENING = "E"
    NIGHT = "N"
    OFF = "O"


# 실제 근무(오프 제외)에 해당하는 교대 목록
WORK_SHIFTS: list[Shift] = [Shift.DAY, Shift.EVENING, Shift.NIGHT]


class Nurse(BaseModel):
    """간호사 한 명."""

    id: str
    name: str


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


class MinStaff(BaseModel):
    """교대별 하루 최소 필요 인원."""

    D: int = Field(1, ge=0)
    E: int = Field(1, ge=0)
    N: int = Field(1, ge=0)

    def get(self, shift: Shift) -> int:
        return {Shift.DAY: self.D, Shift.EVENING: self.E, Shift.NIGHT: self.N}[shift]


class ScheduleRequest(BaseModel):
    """근무표 생성 요청."""

    num_days: int = Field(..., ge=1, le=62, description="배정할 총 일수")
    nurses: list[Nurse] = Field(..., min_length=1)
    min_staff: MinStaff = Field(default_factory=MinStaff)

    max_consecutive_days: int = Field(5, ge=1, description="연속 근무 최대 일수")
    max_consecutive_nights: int = Field(3, ge=1, description="연속 나이트 최대 일수")
    min_off_days: int = Field(0, ge=0, description="기간 내 간호사별 최소 오프 일수")

    requests: list[ShiftRequest] = Field(default_factory=list)

    # 목적 함수 가중치 (클수록 우선순위 높음)
    weight_preference: int = Field(10, ge=0, description="개인 희망 미반영 페널티")
    weight_fairness: int = Field(3, ge=0, description="근무 배분 불공정 페널티")

    # 솔버 시간 제한(초)
    time_limit_seconds: float = Field(15.0, gt=0, le=120)

    @field_validator("nurses")
    @classmethod
    def _unique_ids(cls, v: list[Nurse]) -> list[Nurse]:
        ids = [n.id for n in v]
        if len(ids) != len(set(ids)):
            raise ValueError("간호사 id가 중복되었습니다.")
        return v


class NurseSchedule(BaseModel):
    """간호사 한 명의 기간 전체 근무 배정 결과."""

    nurse_id: str
    name: str
    shifts: list[Shift]  # 날짜별 교대 (길이 = num_days)
    counts: dict[str, int]  # 교대별 총 횟수 {"D":.., "E":.., "N":.., "O":..}


class ScheduleResponse(BaseModel):
    """근무표 생성 결과."""

    status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN"
    feasible: bool
    num_days: int
    schedules: list[NurseSchedule]
    unmet_preferences: int
    message: str = ""
