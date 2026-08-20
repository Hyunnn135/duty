"""관리자용 데이터 백업 내려받기 (T3 — D-15 B안).

허가된 **단일 계정**만 병동 전체 데이터를 ZIP 한 개로 내려받는다.

권한 판정이 `role=="master"` 만으로는 안 되는 이유: 마스터는 **병동마다** 존재한다
(병동 개설자 = 그 병동의 마스터, auth.py 참조). 역할만 보면 타 병동 개설자가 전
병동 간호사 실명·사번이 든 DB를 반출할 수 있다. 그래서 환경변수 allowlist
(`DUTY_BACKUP_OWNER`) **와** role==master 를 **동시에** 요구하고, 환경변수가
미설정이면 기본 개방이 아니라 **전원 거부**한다.

일관성: 운영 DB는 WAL 모드라 파일 직접 복사(`shutil.copy`)는 -wal 미반영·손상
위험이 있다. `VACUUM INTO`(실패 시 `Connection.backup()`)로 **스냅샷 사본**을
만들고, CSV도 그 사본에서 읽어 duty.db와 시점이 어긋나지 않게 한다.

환경 변수: DUTY_BACKUP_OWNER (사번/이메일, 콤마 구분 — 값은 배포 환경에서만 설정).
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .auth import UserInfo, get_current_user
from .storage import KST, _conn, _db_path, _now

# 경고 단계 경계 (KST 기준 경과일) — 마스터 승인 규칙
WARN_DAYS = 30
CRITICAL_DAYS = 45

# CSV로 함께 내보내는 데이터 테이블(전량). backup_log는 백업 자체의 이력
# (병동 데이터가 아님)이라 CSV에서는 제외한다 — duty.db 사본에는 그대로 들어간다.
CSV_EXCLUDED_TABLES = ("backup_log",)

# 거부 응답에는 어떤 신원 정보도 담지 않는다(개인정보 — 교훈 L-1).
DENIED_MSG = "백업 내려받기 권한이 없습니다."


# ---- 권한 ----

def _allowed_ids() -> set[str]:
    """DUTY_BACKUP_OWNER 의 허가 식별자 집합. 미설정·공백이면 빈 집합(= 전원 거부).

    사번은 대문자로 정규화해 저장·조회하고(auth.py) 이메일은 대소문자를 구분하지
    않으므로, 양쪽 모두 대문자로 맞춰 비교한다.
    """
    raw = os.environ.get("DUTY_BACKUP_OWNER", "")
    return {tok.strip().upper() for tok in raw.split(",") if tok.strip()}


def is_backup_owner(user: UserInfo) -> bool:
    """허가 계정 여부 — allowlist ∧ role==master 동시 충족일 때만 True."""
    allowed = _allowed_ids()
    if not allowed:
        return False  # 미설정 = 기본 개방 금지
    if user.role != "master":
        return False  # admin·staff 불허
    mine = {v.upper() for v in (user.empno, user.email) if v}
    return bool(mine & allowed)


def require_backup_owner(
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """허가 계정만 통과 — 무인증은 get_current_user가 먼저 401을 낸다."""
    if not is_backup_owner(user):
        raise HTTPException(403, DENIED_MSG)
    return user


# ---- 스냅샷 ----

def _snapshot(dest_path: str) -> None:
    """운영 DB의 일관된 사본을 dest_path에 만든다 (파일 직접 복사 금지).

    VACUUM INTO는 대상 파일이 이미 있으면 실패하므로 dest_path는 비어 있어야 한다.
    구버전 SQLite 등으로 실패하면 Connection.backup()으로 폴백한다.
    """
    src = sqlite3.connect(_db_path())
    try:
        try:
            src.execute("VACUUM INTO ?", (dest_path,))
        except sqlite3.Error:
            if os.path.exists(dest_path):  # 실패로 남은 부분 파일 제거 후 폴백
                os.remove(dest_path)
            dest = sqlite3.connect(dest_path)
            try:
                src.backup(dest)
            finally:
                dest.close()
    finally:
        src.close()


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in CSV_EXCLUDED_TABLES]


def _table_csv(conn: sqlite3.Connection, table: str) -> bytes:
    """테이블 1개를 CSV(UTF-8 BOM)로 직렬화 — 엑셀에서 한글이 깨지지 않게."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    cur = conn.execute(f'SELECT * FROM "{table}"')
    w.writerow([d[0] for d in cur.description])
    for row in cur:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8-sig")


def _readme(taken_at_kst: datetime) -> bytes:
    return (
        "듀티원 데이터 백업\n"
        "==================\n\n"
        f"백업 시각: {taken_at_kst:%Y년 %m월 %d일 %H:%M} (한국 시간)\n\n"
        "이 파일에 들어 있는 것\n"
        "- duty.db      : 복구에 사용하는 정본 파일입니다. 열어보지 말고 그대로 보관하세요.\n"
        "- tables/*.csv : 사람이 확인·인쇄할 수 있게 표로 뽑은 사본입니다(엑셀로 열립니다).\n"
        "- README.txt   : 이 안내문입니다.\n\n"
        "복구가 필요할 때\n"
        "- 이 ZIP 파일을 그대로 두고 운영 담당자에게 복구를 요청하세요.\n"
        "  (duty.db 파일이 있어야 복구할 수 있습니다. 압축을 풀어 편집하지 마세요.)\n\n"
        "개인정보 주의\n"
        "- 간호사 실명·사번·근무 이력 등 개인정보가 들어 있습니다.\n"
        "- 메신저·메일·공용 폴더에 올리거나 다른 사람에게 전달하지 마세요.\n"
        "- 본인 기기의 안전한 위치에 보관하고, 필요 없어지면 파일을 삭제하세요.\n"
    ).encode("utf-8")


def _build_zip() -> bytes:
    """스냅샷 → ZIP(bytes). 임시 파일은 성공·실패 무관하게 삭제한다."""
    tmpdir = tempfile.mkdtemp(prefix="duty-backup-")
    try:
        snap = os.path.join(tmpdir, "duty.db")
        _snapshot(snap)
        taken_at = datetime.now(KST)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "duty.db")
            conn = sqlite3.connect(snap)  # CSV도 같은 스냅샷에서 — 시점 일치
            try:
                for table in _table_names(conn):
                    zf.writestr(f"tables/{table}.csv", _table_csv(conn, table))
            finally:
                conn.close()
            zf.writestr("README.txt", _readme(taken_at))
        return buf.getvalue()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # 개인정보 잔존 금지


# ---- 이력·상태 ----

def _log(user: UserInfo, byte_size: int, status: str = "ok") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (user.key(), user.ward, _now(), byte_size, status),
        )
        conn.commit()
    finally:
        conn.close()


def days_since_kst(created_at: str | None) -> int | None:
    """저장된 UTC ISO 시각 → **KST 달력 날짜** 기준 경과일 (교훈 L-4).

    사용자가 "며칠 지났다"고 인식하는 기준이 KST이므로 UTC로 계산하면 자정
    부근에서 하루가 어긋난다. 값이 없거나 깨졌으면 None.
    """
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(KST).date() - dt.astimezone(KST).date()).days


def level_for(days: int | None) -> str:
    """경고 단계 — 서버가 계산해 프런트에 내려준다(이력 0건이면 critical)."""
    if days is None or days >= CRITICAL_DAYS:
        return "critical"
    if days >= WARN_DAYS:
        return "warn"
    return "ok"


class BackupStatus(BaseModel):
    last_backup_at: str | None = None  # 저장 형식 그대로(UTC ISO)
    days_since: int | None = None      # KST 달력 기준 경과일
    level: str = "critical"            # ok | warn | critical


# ---- 라우터 ----

router = APIRouter(prefix="/api/admin", tags=["backup"])


@router.get("/backup")
def download_backup(
    user: Annotated[UserInfo, Depends(require_backup_owner)],
) -> Response:
    """전체 데이터 백업 ZIP 내려받기 (허가 계정 전용)."""
    _conn().close()  # 스냅샷 전에 스키마 생성 보장(첫 실행 시 테이블 누락 방지)
    data = _build_zip()
    _log(user, len(data))
    fname = f"duty_backup_{datetime.now(KST):%Y%m%d_%H%M}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/backup/status", response_model=BackupStatus)
def backup_status(
    _user: Annotated[UserInfo, Depends(require_backup_owner)],
) -> BackupStatus:
    """마지막 백업 시각과 경고 단계 (허가 계정 전용)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT created_at FROM backup_log WHERE status='ok' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    last = row["created_at"] if row else None
    days = days_since_kst(last)
    return BackupStatus(last_backup_at=last, days_since=days, level=level_for(days))
