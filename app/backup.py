"""관리자용 데이터 백업 내려받기 (T3 — D-15 B안).

허가된 **단일 계정**만 병동 전체 데이터를 ZIP 한 개로 내려받는다.

권한 판정은 **`users.id`(uid)** 로만 한다. 사번·이메일 같은 "문자열"로 판정하면
두 가지 우회가 실제로 성립했다(검수부 침투 재현):

1. **선점** — 환경변수에 적힌 사번이 아직 어느 계정에도 묶이지 않았으면, 아무나
   새 병동을 열어 master가 된 뒤 그 사번을 자기 계정에 등록해 전체 DB를 반출할 수
   있다. 배포 문서가 안내하던 "환경변수 먼저" 순서에서 이 창이 기본으로 열렸다.
2. **유니코드 접힘** — 이메일은 소문자로 저장하는데 비교는 대문자로 해서, 점 없는
   ı(U+0131) 같은 글자를 쓴 **다른 이메일**이 같은 대문자열로 접혀 통과했다.

uid는 가입 시 DB가 부여하고 이후 바뀌지 않으며, 사용자가 자기 값을 고를 수 없다.
그래서 위 두 우회가 원천적으로 성립하지 않는다. **문자열 기반 판정은 남기지
않는다** — 하위 호환으로 한 줄만 남겨도 그 줄이 곧 취약점이다.

`role=="master"` 결합 조건은 유지하지만 **이것은 실질 방어가 아니다**: 마스터는
병동마다 생기고(병동 개설자 = 그 병동의 마스터), 누구나 새 병동을 열어 master가 될
수 있다. 실질 방어선은 uid allowlist 하나뿐이고, 환경변수가 없으면 **전원 거부**다.

일관성: 운영 DB는 WAL 모드라 파일 직접 복사(`shutil.copy`)는 -wal 미반영·손상
위험이 있다. `VACUUM INTO`(잠금·구버전으로 실패하면 `Connection.backup()`)로
**스냅샷 사본**을 만들고, CSV도 그 사본에서 읽어 duty.db와 시점이 어긋나지 않게 한다.
스냅샷은 내려주기 전에 `PRAGMA quick_check`로 검증한다 — 손상된 DB를 "성공"으로
내려주면 사고 당일에야 복구 불가를 알게 된다.

환경 변수: DUTY_BACKUP_OWNER_UID (users.id, 콤마 구분 정수 — 배포 환경에서만 설정).
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import time
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

# CSV에서 값을 가리는 **테이블.컬럼** 목록.
#
# 근거: 복구 정본은 `duty.db`이고 CSV는 사람이 눈으로 보는 사본일 뿐이다. 아래
# 값들은 CSV에 실려도 복구에 아무 도움이 되지 않는 반면(이득 0), 파일이 한 번
# 새면 그대로 침입 도구가 된다 — 검수부가 백업본의 초대 코드로 실제 가입에
# 성공했다. duty.db 안의 값은 그대로 두므로 복구 능력은 줄지 않는다.
#
# **새 테이블·컬럼을 만들 때 여기에 등록하지 않으면 그 값은 그대로 나간다.**
# 자격증명·비밀·1회용 코드 성격의 컬럼을 추가했다면 반드시 이 표에 넣을 것.
MASKED_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset({"pw_hash", "salt"}),  # 오프라인 비밀번호 대입 공격의 재료
    "ward_invites": frozenset({"code"}),      # 유효한 병동 가입 코드(그 자체로 통행증)
}
MASK_TEXT = "(생략)"

# 거부 응답에는 어떤 신원 정보도 담지 않는다(개인정보 — 교훈 L-1).
DENIED_MSG = "백업 내려받기 권한이 없습니다."
BUILD_FAIL_MSG = (
    "백업 파일을 만들지 못했습니다. 잠시 후 다시 시도하고, 계속 실패하면 "
    "운영 담당자에게 알려 주세요."
)

# 폴백 백업의 데드라인(초). CPython의 Connection.backup()은 SQLITE_BUSY에서
# **횟수 제한 없이** 0.25초씩 재시도하므로 그대로 두면 요청 스레드가 영구
# 점유되고, finally가 영원히 실행되지 않아 개인정보가 든 임시 파일이 남는다.
SNAPSHOT_TIMEOUT_SEC = 30
# 스냅샷 전용 연결의 잠금 대기(ms) — storage._conn()과 같은 값.
SNAPSHOT_BUSY_TIMEOUT_MS = 5000


class BackupError(RuntimeError):
    """백업본을 신뢰할 수 없어 내려주면 안 되는 상황(손상·시간초과 등)."""


# ---- 권한 ----

def _allowed_uids() -> set[int]:
    """DUTY_BACKUP_OWNER_UID 의 허가 uid 집합. 미설정·빈 값이면 빈 집합(= 전원 거부).

    값에 정수가 아닌 토큰이 하나라도 섞여 있으면 **설정 전체를 무효**로 보고 빈
    집합을 돌려준다. 오타 하나를 조용히 무시하고 남은 값으로 문을 열어주는 쪽이
    훨씬 위험하기 때문이다(fail-closed).
    """
    raw = os.environ.get("DUTY_BACKUP_OWNER_UID", "")
    tokens = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not tokens:
        return set()
    uids: set[int] = set()
    for tok in tokens:
        try:
            uid = int(tok)
        except ValueError:
            return set()  # 정수가 아닌 값이 섞였다 → 전원 거부
        if uid <= 0:
            return set()
        uids.add(uid)
    return uids


def is_backup_owner(user: UserInfo) -> bool:
    """허가 계정 여부 — uid allowlist ∧ role==master 동시 충족일 때만 True.

    role 조건은 보조일 뿐이다(누구나 새 병동을 열면 master가 된다). 실제로 막는
    것은 uid allowlist다.
    """
    allowed = _allowed_uids()
    if not allowed:
        return False  # 미설정·형식 오류 = 기본 개방 금지
    if user.role != "master":
        return False  # admin·staff 불허
    return bool(user.uid) and user.uid in allowed


def require_backup_owner(
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """허가 계정만 통과 — 무인증은 get_current_user가 먼저 401을 낸다.

    이쪽은 이력을 남기지 않는다. 화면이 "백업 카드를 그릴지" 판단하려고 모든
    로그인 사용자가 /backup/status 를 한 번씩 호출하므로, 여기서 거부를 기록하면
    정상 이용이 전부 '침입 시도'로 쌓여 로그가 무의미해진다.
    """
    if not is_backup_owner(user):
        raise HTTPException(403, DENIED_MSG)
    return user


def require_backup_owner_audited(
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """반출 경로 전용 — 거부되면 `status='denied'` 로 남긴다.

    남기는 것은 uid와 시각뿐이다(실명·사번·이메일 금지 — 교훈 L-1).
    """
    if not is_backup_owner(user):
        _insert_log(_actor(user), "", 0, "denied")
        raise HTTPException(403, DENIED_MSG)
    return user


# ---- 스냅샷 ----

def _backup_with_deadline(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """`Connection.backup()` 폴백 — 데드라인을 넘기면 예외로 빠져나온다.

    pages=1024로 잘라 실행해야 진행 콜백이 매 단계 호출된다(기본값 -1은 한 번에
    끝내므로 콜백이 한 번뿐이라 시간 검사를 걸 수 없다). 콜백에서 올린 예외는
    backup()이 그대로 전파하므로, 호출부의 finally가 반드시 실행된다.
    """
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SEC

    def _tick(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError(
                f"백업 스냅샷이 {SNAPSHOT_TIMEOUT_SEC}초 안에 끝나지 않았습니다."
            )

    src.backup(dest, pages=1024, progress=_tick)


def _verify_snapshot(path: str) -> None:
    """스냅샷 무결성 확인 — `quick_check`가 'ok'가 아니면 내려주지 않는다.

    폴백 경로(`Connection.backup()`)는 페이지를 그대로 복사하므로 원본이 손상돼
    있으면 손상까지 충실히 복제한다. 검사 없이 200으로 내려주면 "백업했다"는
    기록·화면 표시만 남고 정작 복구는 불가능해진다.
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not row or str(row[0]).lower() != "ok":
        raise BackupError(f"백업 스냅샷 무결성 검사 실패: {row[0] if row else '결과 없음'}")


def _snapshot(dest_path: str) -> None:
    """운영 DB의 일관된 사본을 dest_path에 만든다 (파일 직접 복사 금지).

    VACUUM INTO는 대상 파일이 이미 있으면 실패하므로 dest_path는 비어 있어야 한다.
    폴백은 **잠금·구버전(OperationalError)** 일 때만 허용한다. `sqlite3.Error`를
    통째로 삼키면 손상(DatabaseError: database disk image is malformed)까지 폴백으로
    흘러가 손상본을 그대로 복제하게 된다 — 손상은 그대로 전파시켜야 한다.
    """
    src = sqlite3.connect(_db_path())
    try:
        src.execute(f"PRAGMA busy_timeout={SNAPSHOT_BUSY_TIMEOUT_MS}")
        try:
            src.execute("VACUUM INTO ?", (dest_path,))
        except sqlite3.OperationalError:
            if os.path.exists(dest_path):  # 실패로 남은 부분 파일 제거 후 폴백
                os.remove(dest_path)
            dest = sqlite3.connect(dest_path)
            try:
                _backup_with_deadline(src, dest)
            finally:
                dest.close()
    finally:
        src.close()
    _verify_snapshot(dest_path)


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in CSV_EXCLUDED_TABLES]


def _write_table_csv(
    zf: zipfile.ZipFile, conn: sqlite3.Connection, table: str
) -> None:
    """테이블 1개를 CSV(UTF-8 BOM)로 ZIP에 **흘려 쓴다** — 엑셀 한글 깨짐 방지.

    전량을 StringIO에 str로 쌓고 다시 encode하면 DB 크기의 4~5배가 메모리에
    올라간다(실측: 214MB DB에서 피크 RSS 998MB). 512MiB 컨테이너에서는 DB가
    100MB만 돼도 OOM이고, OOM은 요청 하나가 아니라 프로세스 전체를 죽인다.
    행 단위로 인코딩해 넘기면 피크가 한 행 수준으로 내려간다.

    MASKED_COLUMNS에 등록된 컬럼은 값 대신 `(생략)`을 쓴다.
    """
    masked = MASKED_COLUMNS.get(table, frozenset())
    cur = conn.execute(f'SELECT * FROM "{table}"')
    cols = [d[0] for d in cur.description]
    hidden = [i for i, c in enumerate(cols) if c in masked]
    with zf.open(f"tables/{table}.csv", "w") as raw:
        out = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        try:
            w = csv.writer(out)
            w.writerow(cols)
            for row in cur:
                vals = ["" if v is None else v for v in row]
                for i in hidden:
                    vals[i] = MASK_TEXT
                w.writerow(vals)
            out.flush()
        finally:
            out.detach()  # TextIOWrapper가 ZIP 항목을 닫지 않게 한다


def _readme(taken_at_kst: datetime) -> bytes:
    return (
        "듀티원 데이터 백업\n"
        "==================\n\n"
        f"백업 시각: {taken_at_kst:%Y년 %m월 %d일 %H:%M} (한국 시간)\n\n"
        "이 파일에 들어 있는 것\n"
        "- duty.db      : 복구에 사용하는 정본 파일입니다. 열어보지 말고 그대로 보관하세요.\n"
        "- tables/*.csv : 사람이 확인할 수 있게 표로 뽑은 사본입니다(엑셀로 열립니다).\n"
        "- README.txt   : 이 안내문입니다.\n\n"
        "CSV에 담기는 내용(전부)\n"
        "- users        : 가입 계정 목록 — 이름·사번·이메일·역할·병동\n"
        "- rosters      : 간호사 명단(실명·팀·경력순 등)\n"
        "- schedules    : 만들어 둔 근무표와 발행 이력\n"
        "- wanted_requests : 부서원이 낸 원티드 신청과 승인 여부\n"
        "- request_windows : 원티드 신청 기간 설정\n"
        "- feedback     : 수신함에 들어온 **피드백 원문**(쓴 사람이 누구인지 포함)\n"
        "- ward_invites : 병동별 초대 코드 표\n\n"
        "CSV를 볼 때 알아둘 것\n"
        "- 비밀번호(pw_hash·salt)와 초대 코드(code) 자리에는 값 대신 (생략)이 적혀 있습니다.\n"
        "  새어 나갔을 때 그대로 침입에 쓰이는 값이라 일부러 가린 것이며, 복구에는\n"
        "  쓰이지 않습니다(복구는 duty.db로 합니다).\n"
        "- 빈칸은 '값이 없음'과 '빈 글자'를 구분하지 않습니다. 둘 다 빈칸으로 보입니다.\n"
        "- rosters·schedules의 data 열은 프로그램이 쓰는 형식(JSON)이 한 칸에 통째로\n"
        "  들어 있어 사람이 읽기 어렵습니다. 눈으로 확인하실 때는 앱 화면을 쓰세요.\n\n"
        "복구가 필요할 때\n"
        "- 이 ZIP 파일을 그대로 두고 운영 담당자에게 복구를 요청하세요.\n"
        "  (duty.db 파일이 있어야 복구할 수 있습니다. 압축을 풀어 편집하지 마세요.)\n\n"
        "개인정보 주의\n"
        "- 간호사 실명·사번·근무 이력, 그리고 피드백에 쓴 글 원문까지 들어 있습니다.\n"
        "- 메신저·메일·공용 폴더에 올리거나 다른 사람에게 전달하지 마세요.\n"
        "- OneDrive·iCloud 같은 자동 동기화가 켜진 폴더(바탕화면·문서·다운로드)에\n"
        "  두지 마세요. 그대로 인터넷에 올라갑니다.\n"
        "- 잃어버렸다면 즉시 앱에서 초대 코드를 재발급하세요.\n"
        "- 본인 기기의 안전한 위치에 보관하고, 복구에 쓰고 나면 파일을 삭제하세요.\n"
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
                    _write_table_csv(zf, conn, table)
            finally:
                conn.close()
            zf.writestr("README.txt", _readme(taken_at))
        return buf.getvalue()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # 개인정보 잔존 금지


# ---- 이력·상태 ----
#
# 이력은 **2단계**로 남긴다.
#   1) 요청을 받으면 스냅샷을 뜨기 **전에** status='pending' 행을 넣는다.
#   2) 브라우저가 파일을 끝까지 받은 뒤 /backup/confirm 을 호출하면 'ok'로 바꾼다.
#
# 왜 이렇게 하나:
#  - (a) 예전처럼 응답을 보내기 전에 'ok'를 남기면, 다운로드가 끊기거나 사용자가
#    저장을 취소해도 성공으로 기록돼 경고가 30일간 꺼진다. 파일은 없는데 시스템만
#    있다고 믿는 상태가 가장 위험하다. 브라우저는 **응답 본문을 전부 받은 뒤에만**
#    confirm을 호출하므로(중간에 끊기면 blob 읽기가 실패한다), 'ok'는 실제 전달의
#    증거가 된다. 크기까지 대조해 부분 전달을 걸러낸다.
#  - (b) pending 행을 스냅샷 **전에** 넣는 덕분에 그 행이 백업본 안에도 들어간다.
#    복구본에서 "언제 뜬 백업인지"를 알 수 있다(예전에는 backup_log가 항상 비어
#    있었다). 다만 복구본의 행은 'pending'이라 경고 단계는 여전히 critical이다 —
#    복구 직후 곧바로 1회 백업하도록 DEPLOY 문서에 명시했다.
#  - 실패(스냅샷 손상·시간초과)는 'fail'로 남겨 사후 추적이 가능하게 한다.


def _actor(user: UserInfo) -> str:
    """이력에 남기는 행위자 표기 — uid만. 실명·사번·이메일은 남기지 않는다."""
    return f"uid:{user.uid}"


def _insert_log(actor: str, ward: str, byte_size: int, status: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (actor, ward, _now(), byte_size, status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _update_log(entry_id: int, *, byte_size: int | None = None,
                status: str | None = None) -> None:
    sets, args = [], []
    if byte_size is not None:
        sets.append("byte_size=?")
        args.append(byte_size)
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if not sets:
        return
    args.append(entry_id)
    conn = _conn()
    try:
        conn.execute(f"UPDATE backup_log SET {', '.join(sets)} WHERE id=?", args)
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
    """경고 단계 — 서버가 계산해 프런트에 내려준다(이력 0건이면 critical).

    음수는 마지막 백업 시각이 미래라는 뜻이다(서버 시계 역행·수동 조정·조작된 행).
    "0일 전이니 안전"으로 읽으면 경고가 꺼지므로 신뢰할 수 없는 값으로 보고
    critical 취급한다.
    """
    if days is None or days < 0 or days >= CRITICAL_DAYS:
        return "critical"
    if days >= WARN_DAYS:
        return "warn"
    return "ok"


class BackupStatus(BaseModel):
    last_backup_at: str | None = None  # 저장 형식 그대로(UTC ISO)
    days_since: int | None = None      # KST 달력 기준 경과일
    level: str = "critical"            # ok | warn | critical


class BackupConfirm(BaseModel):
    id: int          # 내려받기 응답의 X-Backup-Id
    bytes: int = 0   # 브라우저가 실제로 받은 바이트 수


def _current_status() -> BackupStatus:
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


# ---- 라우터 ----

router = APIRouter(prefix="/api/admin", tags=["backup"])

# 전체 DB 사본·경고 상태가 브라우저·중간 캐시에 남지 않게 한다.
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/backup")
def download_backup(
    user: Annotated[UserInfo, Depends(require_backup_owner_audited)],
) -> Response:
    """전체 데이터 백업 ZIP 내려받기 (허가 계정 전용).

    여기서는 'ok'를 남기지 않는다 — 아직 파일이 전달되지 않았다. pending 행의
    번호를 `X-Backup-Id` 헤더로 알려주고, 받는 쪽이 /backup/confirm 으로 확정한다.
    """
    _conn().close()  # 스냅샷 전에 스키마 생성 보장(첫 실행 시 테이블 누락 방지)
    entry_id = _insert_log(_actor(user), user.ward, 0, "pending")
    try:
        data = _build_zip()
    except Exception:
        _update_log(entry_id, status="fail")
        raise HTTPException(500, BUILD_FAIL_MSG)
    _update_log(entry_id, byte_size=len(data))
    fname = f"duty_backup_{datetime.now(KST):%Y%m%d_%H%M}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Backup-Id": str(entry_id),
            **NO_STORE,
        },
    )


@router.post("/backup/confirm", response_model=BackupStatus)
def confirm_backup(
    body: BackupConfirm,
    user: Annotated[UserInfo, Depends(require_backup_owner_audited)],
    response: Response,
) -> BackupStatus:
    """내려받기 완료 확정 — 이 호출이 있어야 성공 이력('ok')이 된다.

    본인이 만든 pending 행이어야 하고, 받은 바이트 수가 서버가 만든 크기와
    정확히 같아야 한다(부분 전달 차단).
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT actor, byte_size, status FROM backup_log WHERE id=?",
            (body.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["actor"] != _actor(user):
        raise HTTPException(404, "확정할 백업 기록을 찾을 수 없습니다.")
    if row["status"] == "ok":
        response.headers.update(NO_STORE)
        return _current_status()  # 재호출은 조용히 통과(멱등)
    if row["status"] != "pending":
        raise HTTPException(409, "이미 실패로 처리된 백업입니다.")
    if row["byte_size"] <= 0 or body.bytes != row["byte_size"]:
        raise HTTPException(400, "받은 파일 크기가 서버 기록과 다릅니다.")
    _update_log(body.id, status="ok")
    response.headers.update(NO_STORE)
    return _current_status()


@router.get("/backup/status", response_model=BackupStatus)
def backup_status(
    _user: Annotated[UserInfo, Depends(require_backup_owner)],
    response: Response,
) -> BackupStatus:
    """마지막 백업 시각과 경고 단계 (허가 계정 전용)."""
    response.headers.update(NO_STORE)
    return _current_status()
