"""서버 영속화 계층 (Phase 5) — SQLite 기반.

auth.py와 같은 DB 파일(DUTY_DB)을 공유하며, 병동(ward)을 테넌트 경계로 사용한다
(멀티테넌시, PLAN 7.5.5). 배포 시 Postgres+RLS로 이관하기 쉽도록 모든 쿼리를
ward로 스코프한다.

저장 대상:
  - roster          : 병동 간호사 명단(부서장 관리). 민감 속성은 부서원에게 비공개.
  - schedules       : 확정/발행된 근무표(월별 1개). 병동 구성원 모두 조회 가능.
  - wanted_requests : 부서원 원티드 신청 + 승인 상태.
  - request_windows : 원티드 신청 기간(부서장 통제) — 만료 시 경고.
  - feedback        : 마스터 수신함 (사용자 → 마스터 메시지).

환경 변수: DUTY_DB (auth.py와 동일 경로).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from . import email_notify
from .auth import UserInfo, get_current_user, require_roles


def _emails_by_role(roles: tuple[str, ...], ward: str | None = None) -> list[str]:
    """알림 수신자 이메일 목록 (users 테이블 조회). 실패 시 빈 목록."""
    placeholders = ",".join("?" for _ in roles)
    try:
        conn = _conn()
        try:
            if ward is None:
                rows = conn.execute(
                    f"SELECT email FROM users WHERE role IN ({placeholders})", tuple(roles)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT email FROM users WHERE ward=? AND role IN ({placeholders})",
                    (ward, *roles),
                ).fetchall()
            return [r["email"] for r in rows]
        finally:
            conn.close()
    except Exception:  # users 테이블 부재 등 — 알림은 부가기능이므로 조용히 무시
        return []

# 부서원에게 공개되지 않는 민감 명단 속성 (PLAN 7.5.10)
# account_email(계정 연결)은 타인의 것을 노출하지 않도록 부서원 조회에서 제거한다
# (본인 연결 정보는 /api/me/nurse 로 별도 제공).
SENSITIVE_NURSE_FIELDS = {
    "seniority_rank", "night_eligible", "is_trainee", "trainer",
    "trainer_id", "competency", "employment", "note", "memo", "account_email",
}


def _db_path() -> str:
    return os.environ.get("DUTY_DB", "duty.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rosters (
            ward TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT,
            updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ward TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT,
            updated_by TEXT,
            UNIQUE(ward, year, month)
        );
        CREATE TABLE IF NOT EXISTS wanted_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ward TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            nurse_email TEXT NOT NULL,
            nurse_name TEXT NOT NULL,
            start_day INTEGER NOT NULL,
            end_day INTEGER NOT NULL,
            shift TEXT NOT NULL,
            reason TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT,
            decided_by TEXT,
            decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS request_windows (
            ward TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            opens_at TEXT,
            closes_at TEXT,
            note TEXT DEFAULT '',
            PRIMARY KEY(ward, year, month)
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ward TEXT DEFAULT '',
            from_email TEXT NOT NULL,
            from_name TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT,
            read_at TEXT
        );
        """
    )
    return conn


# ============================ 스키마 ============================

class RosterPayload(BaseModel):
    nurses: list[dict[str, Any]] = Field(default_factory=list)


class RosterResponse(BaseModel):
    nurses: list[dict[str, Any]]
    updated_at: str | None = None
    editable: bool = False  # 현재 사용자가 편집 가능한지(관리자·마스터)


class MyNurse(BaseModel):
    linked: bool
    nurse: dict[str, Any] | None = None  # 연결된 명단 항목(본인 것이므로 전체 노출)


class SchedulePublish(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    data: dict[str, Any]


class ScheduleMeta(BaseModel):
    year: int
    month: int
    updated_at: str | None = None
    updated_by: str | None = None


class ScheduleResponse(BaseModel):
    year: int
    month: int
    data: dict[str, Any]
    updated_at: str | None = None
    updated_by: str | None = None


class WantedSubmit(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    start_day: int = Field(..., ge=1, le=31)
    end_day: int = Field(..., ge=1, le=31)
    shift: str = Field("O")
    reason: str = ""

    def clean_shift(self) -> str:
        return self.shift if self.shift in ("D", "E", "N", "O") else "O"


class WantedItem(BaseModel):
    id: int
    year: int
    month: int
    nurse_email: str
    nurse_name: str
    start_day: int
    end_day: int
    shift: str
    reason: str
    status: str
    created_at: str | None = None


class WantedDecision(BaseModel):
    status: str  # approved | rejected

    def check(self) -> None:
        if self.status not in ("approved", "rejected", "pending"):
            raise HTTPException(422, "status는 approved/rejected/pending 중 하나여야 합니다.")


class WindowPayload(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    opens_at: str | None = None   # ISO 날짜/시각
    closes_at: str | None = None
    note: str = ""


class WindowStatus(BaseModel):
    year: int
    month: int
    opens_at: str | None = None
    closes_at: str | None = None
    note: str = ""
    is_open: bool = True
    message: str = ""


class FeedbackSubmit(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class FeedbackItem(BaseModel):
    id: int
    ward: str
    from_email: str
    from_name: str
    message: str
    created_at: str | None = None
    read_at: str | None = None


EXPIRED_MSG = "신청 기한이 만료되었습니다. 추가 수정 필요 시 부서장에게 직접 문의하세요."


# ============================ 라우터 ============================

router = APIRouter(prefix="/api", tags=["storage"])


def _public_nurse(n: dict[str, Any]) -> dict[str, Any]:
    """부서원용: 민감 속성 제거."""
    return {k: v for k, v in n.items() if k not in SENSITIVE_NURSE_FIELDS}


# ---- 명단(roster) ----

@router.put("/roster", response_model=RosterResponse)
def save_roster(
    body: RosterPayload,
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> RosterResponse:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO rosters (ward, data, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(ward) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (user.ward, json.dumps(body.nurses, ensure_ascii=False), _now(), user.email),
        )
        conn.commit()
        return RosterResponse(nurses=body.nurses, updated_at=_now(), editable=True)
    finally:
        conn.close()


@router.get("/me/nurse", response_model=MyNurse)
def my_nurse(user: Annotated[UserInfo, Depends(get_current_user)]) -> MyNurse:
    """현재 로그인 사용자와 연결된 명단 간호사를 반환 (account_email 매칭)."""
    conn = _conn()
    try:
        row = conn.execute("SELECT data FROM rosters WHERE ward=?", (user.ward,)).fetchone()
        if not row:
            return MyNurse(linked=False)
        for n in json.loads(row["data"]):
            if str(n.get("account_email", "")).strip().lower() == user.email.lower():
                return MyNurse(linked=True, nurse=n)
        return MyNurse(linked=False)
    finally:
        conn.close()


@router.get("/roster", response_model=RosterResponse)
def get_roster(user: Annotated[UserInfo, Depends(get_current_user)]) -> RosterResponse:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM rosters WHERE ward=?", (user.ward,)).fetchone()
        nurses = json.loads(row["data"]) if row else []
        editable = user.role in ("admin", "master")
        if not editable:
            nurses = [_public_nurse(n) for n in nurses]
        return RosterResponse(
            nurses=nurses, updated_at=row["updated_at"] if row else None, editable=editable
        )
    finally:
        conn.close()


# ---- 근무표(schedules) ----

@router.post("/schedule/publish", response_model=ScheduleResponse)
def publish_schedule(
    body: SchedulePublish,
    background: BackgroundTasks,
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> ScheduleResponse:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO schedules (ward, year, month, data, updated_at, updated_by) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(ward, year, month) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (user.ward, body.year, body.month, json.dumps(body.data, ensure_ascii=False),
             _now(), user.email),
        )
        conn.commit()
    finally:
        conn.close()
    # 발행 알림(옵트인): 병동 구성원에게 이메일 (NOTIFY_ON_PUBLISH=1 + SMTP 설정 시)
    if email_notify.notify_on_publish():
        emails = _emails_by_role(("staff", "admin", "master"), ward=user.ward)
        if emails:
            subject = f"[듀티원] {body.year}년 {body.month}월 근무표가 발행되었습니다"
            text = (f"{user.ward or ''}병동 {body.year}년 {body.month}월 근무표가 발행되었습니다.\n"
                    "듀티원에 로그인해 확인하세요.")
            background.add_task(email_notify.send_email, emails, subject, text)
    return ScheduleResponse(year=body.year, month=body.month, data=body.data,
                            updated_at=_now(), updated_by=user.name)


@router.get("/schedules", response_model=list[ScheduleMeta])
def list_schedules(user: Annotated[UserInfo, Depends(get_current_user)]) -> list[ScheduleMeta]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT year, month, updated_at, updated_by FROM schedules WHERE ward=? "
            "ORDER BY year DESC, month DESC", (user.ward,)
        ).fetchall()
        return [ScheduleMeta(year=r["year"], month=r["month"],
                             updated_at=r["updated_at"], updated_by=r["updated_by"]) for r in rows]
    finally:
        conn.close()


@router.get("/schedule/{year}/{month}", response_model=ScheduleResponse)
def get_schedule(
    year: int, month: int,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> ScheduleResponse:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM schedules WHERE ward=? AND year=? AND month=?",
            (user.ward, year, month)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "해당 월의 발행된 근무표가 없습니다.")
        return ScheduleResponse(year=year, month=month, data=json.loads(row["data"]),
                                updated_at=row["updated_at"], updated_by=row["updated_by"])
    finally:
        conn.close()


# ---- 원티드 신청(wanted) ----

def _window_open(conn: sqlite3.Connection, ward: str, year: int, month: int) -> tuple[bool, str]:
    row = conn.execute(
        "SELECT * FROM request_windows WHERE ward=? AND year=? AND month=?",
        (ward, year, month)
    ).fetchone()
    if row is None:
        return True, ""  # 윈도우 미설정 = 항상 열림
    now = _now()
    if row["opens_at"] and now < row["opens_at"]:
        return False, "아직 신청 기간이 시작되지 않았습니다."
    if row["closes_at"] and now > row["closes_at"]:
        return False, EXPIRED_MSG
    return True, ""


@router.post("/wanted", response_model=WantedItem)
def submit_wanted(
    body: WantedSubmit,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> WantedItem:
    if body.end_day < body.start_day:
        raise HTTPException(422, "종료일은 시작일 이상이어야 합니다.")
    if body.end_day - body.start_day + 1 > 3:
        raise HTTPException(422, "연속 오프 신청은 최대 3일까지 가능합니다 (대원칙 E4).")
    conn = _conn()
    try:
        is_open, msg = _window_open(conn, user.ward, body.year, body.month)
        if not is_open:
            raise HTTPException(403, msg)
        cur = conn.execute(
            "INSERT INTO wanted_requests (ward, year, month, nurse_email, nurse_name, "
            "start_day, end_day, shift, reason, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
            (user.ward, body.year, body.month, user.email, user.name,
             body.start_day, body.end_day, body.clean_shift(), body.reason, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM wanted_requests WHERE id=?", (cur.lastrowid,)).fetchone()
        return _wanted_item(row)
    finally:
        conn.close()


def _wanted_item(r: sqlite3.Row) -> WantedItem:
    return WantedItem(
        id=r["id"], year=r["year"], month=r["month"], nurse_email=r["nurse_email"],
        nurse_name=r["nurse_name"], start_day=r["start_day"], end_day=r["end_day"],
        shift=r["shift"], reason=r["reason"], status=r["status"], created_at=r["created_at"],
    )


@router.get("/wanted/mine", response_model=list[WantedItem])
def my_wanted(
    year: int, month: int,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> list[WantedItem]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM wanted_requests WHERE ward=? AND year=? AND month=? AND nurse_email=? "
            "ORDER BY start_day", (user.ward, year, month, user.email)
        ).fetchall()
        return [_wanted_item(r) for r in rows]
    finally:
        conn.close()


@router.get("/wanted", response_model=list[WantedItem])
def list_wanted(
    year: int, month: int,
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> list[WantedItem]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM wanted_requests WHERE ward=? AND year=? AND month=? "
            "ORDER BY nurse_name, start_day", (user.ward, year, month)
        ).fetchall()
        return [_wanted_item(r) for r in rows]
    finally:
        conn.close()


@router.post("/wanted/{req_id}/decision", response_model=WantedItem)
def decide_wanted(
    req_id: int, body: WantedDecision,
    background: BackgroundTasks,
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> WantedItem:
    body.check()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM wanted_requests WHERE id=? AND ward=?", (req_id, user.ward)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "신청을 찾을 수 없습니다.")
        conn.execute(
            "UPDATE wanted_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
            (body.status, user.email, _now(), req_id),
        )
        conn.commit()
        item = _wanted_item(conn.execute(
            "SELECT * FROM wanted_requests WHERE id=?", (req_id,)).fetchone())
    finally:
        conn.close()
    # 신청자에게 승인/반려 결과 이메일 (미설정 시 무시)
    if body.status in ("approved", "rejected") and row["nurse_email"]:
        ko = "승인" if body.status == "approved" else "반려"
        rng = f"{item.start_day}일" + (f"~{item.end_day}일" if item.end_day != item.start_day else "")
        subject = f"[듀티원] 원티드 신청이 {ko}되었습니다 ({item.year}년 {item.month}월)"
        text = (f"{item.nurse_name} 님의 {item.year}년 {item.month}월 원티드 신청"
                f"({rng}, {item.shift})이 {ko}되었습니다.")
        background.add_task(email_notify.send_email, row["nurse_email"], subject, text)
    return item


@router.delete("/wanted/{req_id}")
def delete_wanted(
    req_id: int,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> dict[str, bool]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM wanted_requests WHERE id=? AND ward=?", (req_id, user.ward)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "신청을 찾을 수 없습니다.")
        # 본인 신청이거나 관리자·마스터만 삭제 가능
        if row["nurse_email"] != user.email and user.role not in ("admin", "master"):
            raise HTTPException(403, "본인 신청만 취소할 수 있습니다.")
        # 신청 기간이 닫혔으면 부서원은 취소 불가(부서장 문의)
        if user.role == "staff":
            is_open, msg = _window_open(conn, user.ward, row["year"], row["month"])
            if not is_open:
                raise HTTPException(403, msg)
        conn.execute("DELETE FROM wanted_requests WHERE id=?", (req_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---- 신청 기간(request window) ----

@router.put("/request-window", response_model=WindowStatus)
def set_window(
    body: WindowPayload,
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> WindowStatus:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO request_windows (ward, year, month, opens_at, closes_at, note) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(ward, year, month) DO UPDATE SET opens_at=excluded.opens_at, "
            "closes_at=excluded.closes_at, note=excluded.note",
            (user.ward, body.year, body.month, body.opens_at, body.closes_at, body.note),
        )
        conn.commit()
        is_open, msg = _window_open(conn, user.ward, body.year, body.month)
        return WindowStatus(year=body.year, month=body.month, opens_at=body.opens_at,
                            closes_at=body.closes_at, note=body.note, is_open=is_open, message=msg)
    finally:
        conn.close()


@router.get("/request-window/{year}/{month}", response_model=WindowStatus)
def get_window(
    year: int, month: int,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> WindowStatus:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM request_windows WHERE ward=? AND year=? AND month=?",
            (user.ward, year, month)
        ).fetchone()
        is_open, msg = _window_open(conn, user.ward, year, month)
        return WindowStatus(
            year=year, month=month,
            opens_at=row["opens_at"] if row else None,
            closes_at=row["closes_at"] if row else None,
            note=row["note"] if row else "",
            is_open=is_open, message=msg,
        )
    finally:
        conn.close()


# ---- 피드백(마스터 수신함) ----

@router.post("/feedback", response_model=FeedbackItem)
def submit_feedback(
    body: FeedbackSubmit,
    background: BackgroundTasks,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> FeedbackItem:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            (user.ward, user.email, user.name, body.message, _now()),
        )
        conn.commit()
        r = conn.execute("SELECT * FROM feedback WHERE id=?", (cur.lastrowid,)).fetchone()
        item = _feedback_item(r)
    finally:
        conn.close()
    # 마스터에게 이메일 알림 (미설정 시 자동 무시)
    masters = _emails_by_role(("master",))
    if masters:
        subject = f"[듀티원] 새 피드백 · {user.name}"
        text = (
            f"{user.name} ({user.email})"
            f"{' · ' + user.ward + '병동' if user.ward else ''} 님이 메시지를 남겼습니다.\n\n"
            f"{body.message}\n\n"
            "— 듀티원 수신함에서 확인하세요."
        )
        background.add_task(email_notify.send_email, masters, subject, text)
    return item


def _feedback_item(r: sqlite3.Row) -> FeedbackItem:
    return FeedbackItem(
        id=r["id"], ward=r["ward"], from_email=r["from_email"], from_name=r["from_name"],
        message=r["message"], created_at=r["created_at"], read_at=r["read_at"],
    )


@router.get("/feedback", response_model=list[FeedbackItem])
def list_feedback(
    _master: Annotated[UserInfo, Depends(require_roles("master"))],
) -> list[FeedbackItem]:
    """마스터 수신함 — 모든 병동의 피드백을 최신순으로."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()
        return [_feedback_item(r) for r in rows]
    finally:
        conn.close()


@router.post("/feedback/{fb_id}/read", response_model=FeedbackItem)
def mark_feedback_read(
    fb_id: int,
    _master: Annotated[UserInfo, Depends(require_roles("master"))],
) -> FeedbackItem:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM feedback WHERE id=?", (fb_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "피드백을 찾을 수 없습니다.")
        conn.execute("UPDATE feedback SET read_at=? WHERE id=?", (_now(), fb_id))
        conn.commit()
        return _feedback_item(conn.execute("SELECT * FROM feedback WHERE id=?", (fb_id,)).fetchone())
    finally:
        conn.close()
