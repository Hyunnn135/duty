"""로그인/인증 시스템 (JWT + SQLite + 3역할 RBAC + 병동 초대 코드).

- 역할: master(마스터) / admin(관리자·파트장) / staff(일반 부서원)
- 가입 규칙: **병동을 처음 개설한 사용자가 그 병동의 master**가 되고 초대 코드가
  자동 발급된다. 이후 그 병동 가입은 **초대 코드 필수**(코드가 병동을 결정) →
  코드 없이 임의 병동에 들어와 명단·근무표를 보는 것을 차단한다.
  master가 /api/auth/set-role 로 admin 승격/변경, /api/auth/invite 로 코드 확인·재발급.
- 권한 확인: 토큰은 신원(sub)+만료만 신뢰하고 **역할·병동은 매 요청 DB에서 조회**
  → 강등/승격이 토큰 만료를 기다리지 않고 즉시 반영된다.
- 비밀번호: hashlib.scrypt(표준 라이브러리) + 사용자별 salt.
- 토큰: JWT(HS256), 12시간 유효.

배포 시 Supabase Auth로 교체 가능하도록 이 모듈에 격리되어 있다(PLAN 7.5.2).
환경 변수: DUTY_DB(사용자 DB 경로, 기본 duty.db), DUTY_SECRET(JWT 서명 키).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

ROLES = ("master", "admin", "staff")
TOKEN_TTL_SECONDS = 12 * 3600
_ALGO = "HS256"


_EPHEMERAL_SECRET: str | None = None


def _secret() -> str:
    """JWT 서명 키. 프로덕션은 DUTY_SECRET(Secret Manager)를 반드시 설정한다.

    미설정 시 고정 상수 대신 **프로세스마다 무작위 키**를 생성해 쓴다(소스에 박힌
    상수 키로 인한 토큰 위조를 원천 차단). 단점: 재시작 시 기존 토큰 무효화(재로그인)
    → 로컬/개발용으로만 적합하므로 배포에서는 반드시 DUTY_SECRET을 준다.
    """
    s = os.environ.get("DUTY_SECRET")
    if s:
        return s
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        import warnings
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        warnings.warn(
            "DUTY_SECRET 미설정 — 임시 무작위 서명 키 사용(재시작 시 재로그인 필요). "
            "배포에서는 반드시 DUTY_SECRET을 설정하세요.",
            RuntimeWarning, stacklevel=2,
        )
    return _EPHEMERAL_SECRET


def _db_path() -> str:
    return os.environ.get("DUTY_DB", "duty.db")


# DDL은 DB 경로별 1회만 실행한다(매 요청 스키마 파싱/락 오버헤드 제거).
# IF NOT EXISTS라 동시 초기화 경합에도 안전하다.
_INITED_DBS: set[str] = set()


def _conn() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if path not in _INITED_DBS:
        try:
            _init_db(conn)
            _INITED_DBS.add(path)
        except Exception:
            conn.close()  # 초기화 실패 시 연결(WAL 핸들) 누수 방지
            raise
    return conn


def _add_user_column(conn: sqlite3.Connection, name: str, decl: str) -> None:
    """users에 컬럼을 1회 추가한다 — 동시 첫 요청의 `duplicate column name` 500을 삼킨다.

    여러 워커가 빈 DB에 **동시에 처음** 붙으면 각자 자기 _INITED_DBS로 마이그레이션을
    돌리는데, PRAGMA 확인과 ALTER 사이에서 경합해 한쪽이 이미 붙인 컬럼을 다른 쪽이
    또 붙이려다 OperationalError('duplicate column name')로 500을 낸다. 무손실·자가
    치유지만 배포 직후·DB 초기화 직후의 그 창을 없앤다(D-1). ADD COLUMN이 아닌 다른
    OperationalError는 그대로 전파한다.
    """
    if name in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
        return
    try:
        conn.execute(f"ALTER TABLE users ADD COLUMN {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _init_db(conn: sqlite3.Connection) -> None:
    """스키마 생성·마이그레이션 (DB 경로별 1회, _conn에서 호출)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empno TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            ward TEXT DEFAULT '',
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            backup_owner INTEGER NOT NULL DEFAULT 0
        )"""
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "empno" not in cols:
        # 구 스키마(email NOT NULL, empno 없음) → 사번 도입 스키마로 1회 재구축.
        # 기존 계정은 empno=NULL로 이전되며 이메일 로그인이 계속 동작한다.
        # 쓰기 락(BEGIN IMMEDIATE) 아래에서 다시 확인(이중 확인 잠금) — 두 워커가
        # 동시에 진입해도 한쪽만 재구축하고, 그 사이 등록된 empno가 empno 없는
        # INSERT-SELECT 복사에 지워지는 일이 없게 한다.
        conn.execute("BEGIN IMMEDIATE")
        try:
            cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if "empno" not in cols2:
                conn.execute(
                    """CREATE TABLE users_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empno TEXT UNIQUE,
                        email TEXT UNIQUE,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL,
                        ward TEXT DEFAULT '',
                        pw_hash TEXT NOT NULL,
                        salt TEXT NOT NULL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                conn.execute(
                    "INSERT INTO users_v2 (id, email, name, role, ward, pw_hash, "
                    "salt, created_at) SELECT id, email, name, role, ward, pw_hash, "
                    "salt, created_at FROM users"
                )
                conn.execute("DROP TABLE users")
                conn.execute("ALTER TABLE users_v2 RENAME TO users")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    # 백업 권한 플래그(D-19) — 레거시 DB에 1회 추가한다. 기동만으로 올라와야 하므로
    # 재구축이 아니라 ADD COLUMN으로 붙인다(기본값 0 = 아무도 권한이 없는 상태).
    # 값이 계정에 붙어 있으므로 "uid를 먼저 차지하면 권한이 따라온다"는 선점 경로가
    # 없어진다. 권한은 운영자만 아는 코드를 제출한 계정에만 켜진다(backup.claim).
    # ADD COLUMN은 idempotent하지 않아 동시 첫 요청에서 duplicate column 500을 냈다
    # (D-1) — _add_user_column이 그 경합을 삼킨다.
    _add_user_column(conn, "backup_owner",
                     "backup_owner INTEGER NOT NULL DEFAULT 0")
    # 권한 코드 실패 잠금 상태 — 계정 행에 붙인다(backup.py가 읽고 쓴다).
    # 프로세스 메모리에 두면 scale-to-zero로 인스턴스가 내려갈 때 잠금이 통째로
    # 풀리고, 출처 IP를 키에 넣으면 프록시 뒤(Railway)에서는 전원이 같은 IP로 보여
    # 아무나 5회 틀려 정상 운영자를 막을 수 있다. IP를 DB에 남기면 백업 ZIP으로
    # 실려 나가기까지 한다(개인정보 — 교훈 L-1). 그래서 **계정 축만, DB에** 남긴다.
    _add_user_column(conn, "claim_fails",
                     "claim_fails INTEGER NOT NULL DEFAULT 0")
    # 이 시각(UTC ISO)까지 잠금. 실패할 때마다 '지금 + 잠금시간'으로 갱신되므로
    # **마지막 실패 시각의 표지**로도 쓴다(전역 실패 총량의 창 판정 — backup.py).
    _add_user_column(conn, "claim_locked_until", "claim_locked_until TEXT")
    # 대문자 정규화 이전 커밋으로 저장됐을 수 있는 소문자 사번을 1회 정규화(방어적)
    # — 조회·중복검사·명단 매칭이 모두 대문자 기준이므로 저장 데이터도 맞춘다.
    try:
        conn.execute(
            "UPDATE users SET empno=UPPER(empno) "
            "WHERE empno IS NOT NULL AND empno <> UPPER(empno)")
        conn.execute(
            "UPDATE wanted_requests SET nurse_email=UPPER(nurse_email) "
            "WHERE nurse_email NOT LIKE '%@%' AND nurse_email <> UPPER(nurse_email)")
    except sqlite3.IntegrityError:
        pass  # 대소문자 변형 중복 계정(비정상 데이터)은 수동 해소 — 초기화는 계속
    except sqlite3.OperationalError:
        pass  # wanted 테이블 미생성(첫 실행)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ward_invites (
            ward TEXT PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()


# 혼동 문자(0/O, 1/I/L) 제외한 8자리 초대 코드
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


def _ensure_invite(conn: sqlite3.Connection, ward: str) -> str:
    """병동 초대 코드를 반환(없으면 생성 — 기존 DB 마이그레이션 겸용)."""
    row = conn.execute("SELECT code FROM ward_invites WHERE ward=?", (ward,)).fetchone()
    if row is not None:
        return row["code"]
    # INSERT OR IGNORE: 동시 첫 조회 경합에서 진 쪽도 500 없이 기존 코드를 읽는다
    conn.execute(
        "INSERT OR IGNORE INTO ward_invites (ward, code) VALUES (?, ?)",
        (ward, _new_code()),
    )
    conn.commit()
    return conn.execute(
        "SELECT code FROM ward_invites WHERE ward=?", (ward,)
    ).fetchone()["code"]


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
    ).hex()


# ---- 스키마 ----

# 사번: 숫자/영문 3~20자 (병원 사번 체계가 달라도 수용)
EMPNO_PATTERN = r"^[A-Za-z0-9]{3,20}$"


class RegisterRequest(BaseModel):
    # 사번이 로그인 아이디. (구버전 호환: 사번 없이 이메일만으로도 가입은 허용하되,
    # 웹 화면은 사번을 필수로 받는다.)
    empno: str = Field("", description="사번 — 로그인 아이디 (숫자/영문 3~20자)")
    email: EmailStr | None = Field(None, description="이메일 (선택) — 알림 수신용")
    password: str = Field(..., min_length=8, description="8자 이상")
    name: str = Field(..., min_length=1)
    ward: str = Field("", description="새 병동 개설 시 병동명 (예: 61) — 개설자가 마스터가 됨")
    invite_code: str = Field("", description="기존 병동 가입용 초대 코드 (부서장에게 받음)")


class LoginRequest(BaseModel):
    login: str = Field("", description="사번 또는 이메일")
    email: str = Field("", description="(구버전 호환) 이메일")
    password: str

    def login_id(self) -> str:
        return (self.login or self.email).strip()


class TokenResponse(BaseModel):
    token: str
    role: str
    name: str
    ward: str
    empno: str = ""
    # uid는 더 이상 내려주지 않는다. 백업 권한이 환경변수 uid 목록에서 계정 플래그로
    # 옮겨가면서(D-19) 화면이 uid를 보여줄 이유가 사라졌다 — 쓰지 않는 내부 식별자를
    # 굳이 응답에 실어 두지 않는다.


class UserInfo(BaseModel):
    email: str
    name: str
    role: str
    ward: str
    empno: str = ""
    uid: int = 0  # users.id — 사번/이메일과 무관한 안정적 내부 식별자(서버 내부용)
    backup_owner: bool = False  # users.backup_owner — 백업 반출 권한(D-19)

    def key(self) -> str:
        """데이터 연결용 계정 식별자 — 사번이 있으면 사번, 없으면(구계정) 이메일."""
        return self.empno or self.email


class SetRoleRequest(BaseModel):
    login: str = Field("", description="대상 사번 또는 이메일")
    email: str = Field("", description="(구버전 호환) 이메일")
    role: str

    def target(self) -> str:
        return (self.login or self.email).strip()

    def validate_role(self) -> None:
        if self.role not in ROLES:
            raise HTTPException(422, f"역할은 {ROLES} 중 하나여야 합니다.")


class SetEmpnoRequest(BaseModel):
    empno: str = Field(..., pattern=EMPNO_PATTERN, description="내 계정에 등록할 사번")


class WardUser(BaseModel):
    email: str
    name: str
    role: str
    empno: str = ""


class InviteInfo(BaseModel):
    ward: str
    code: str


# ---- 토큰/의존성 ----

def _make_token(user: sqlite3.Row) -> str:
    # sub는 내부 id — 사번/이메일이 나중에 바뀌어도 토큰이 계속 유효하다.
    payload = {
        "sub": f"id:{user['id']}",
        "name": user["name"],
        "role": user["role"],
        "ward": user["ward"],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def _row_get(row: sqlite3.Row, key: str, default=None):
    """Row에서 컬럼을 안전하게 꺼낸다 — 마이그레이션 전 스키마에서도 죽지 않게."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _user_info(u: sqlite3.Row) -> UserInfo:
    return UserInfo(email=u["email"] or "", name=u["name"], role=u["role"],
                    ward=u["ward"], empno=u["empno"] or "", uid=u["id"],
                    backup_owner=bool(_row_get(u, "backup_owner", 0)))


# ---- 권한 코드 제출: 잠금·전역상한·grant/revoke를 한 트랜잭션으로 직렬화 ----
#
# 시각은 전부 `_utc_now_iso()`가 만든 같은 형식(UTC ISO, 고정 오프셋)이라 문자열
# 비교가 곧 시각 비교다. 파싱 없이 SQL에서 바로 창을 자를 수 있다.
#
# **왜 한 함수·한 트랜잭션인가(①결함1):** 예전에는 잠금 검사(_claim_guard)·코드
# 대조·실패 기록(record_claim_fail: read-modify-write)이 서로 다른 연결/트랜잭션으로
# 흩어져 있었다. 그래서 동시 요청 여러 개가 서로의 실패가 기록되기 **전에** 가드를
# 통과했다(검사-후-행동 경합). 실측: 50 동시 요청 중 다수가 상한 5를 넘겨 통과했고,
# 정답을 버스트에 섞으면 전역 상한 30도 한 버스트로 뚫렸다. 아래는 `BEGIN IMMEDIATE`로
# **쓰기 락을 먼저** 잡아 claim/revoke를 순차화하고, 락을 잡은 뒤 카운터를 **재조회**해
# 잠금을 다시 판정(fail-closed)한 다음에만 코드를 대조한다. 실패 누적은 한 문장
# `claim_fails+1`(원자적 증가)로, 성공 시 grant/revoke까지 같은 트랜잭션 안에서 끝낸다.
# SQLite에서 BEGIN IMMEDIATE는 여러 워커/스레드의 쓰기를 직렬화하고, busy_timeout(_conn의
# 5000ms)이 락 경합 시 즉시 에러 대신 대기하게 한다.

# run_claim_transaction 결과 코드
CLAIM_LOCKED = "locked"          # 이 계정이 잠김(연속 실패 상한)
CLAIM_LOCKED_GLOBAL = "locked_global"  # 전역 실패 총량 상한
CLAIM_BAD = "bad_code"           # 코드 불일치(실패 +1 기록됨)
CLAIM_OK = "ok"                  # 코드 일치 → grant/revoke 수행됨


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_claim_transaction(
    uid: int,
    *,
    action: str,
    max_fails: int,
    global_max_fails: int,
    lock_sec: int,
    code_ok,
) -> tuple[str, object]:
    """권한 코드 제출을 BEGIN IMMEDIATE 한 트랜잭션으로 직렬화한다(claim·revoke 공용).

    순서: (1) 쓰기 락 획득 → (2) 이 계정·전역의 실패 카운터를 **재조회**해 잠금
    재판정(fail-closed) → (3) 통과 시에만 `code_ok()`로 코드 대조 → (4) 틀리면
    `claim_fails+1`(원자적, 창이 지났으면 1로 리셋)·잠금 창 갱신, 맞으면 카운터를
    지우고 action('grant'|'revoke')을 **같은 트랜잭션에서** 수행. 전역 상한도 이
    트랜잭션 안에서 판정하므로 동시 버스트가 lost update로 상한을 넘지 못한다.

    code_ok(): 코드 일치 여부(hmac.compare_digest). 락 획득·잠금 통과 뒤에만 부른다.
    반환: (결과코드, data)
      - 결과코드: CLAIM_LOCKED | CLAIM_LOCKED_GLOBAL | CLAIM_BAD | CLAIM_OK
      - data(CLAIM_OK일 때만 의미 있음):
          action='grant'  → 'granted'(새로 켜짐) | 'already'(이미 켜져 있었음) |
                             'missing'(계정 없음)
          action='revoke' → 실제로 꺼진 [(uid, 병동), ...] 목록
        그 외 결과코드에서는 None.
    """
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 쓰기 락을 먼저 잡아 동시 claim/revoke를 순차화
        now = _utc_now_iso()
        # 락을 잡은 뒤 카운터를 다시 읽어 잠금을 재판정한다(fail-closed).
        row = conn.execute(
            "SELECT claim_fails, claim_locked_until FROM users WHERE id=?",
            (uid,)).fetchone()
        fails = 0
        if row is not None:
            until = _row_get(row, "claim_locked_until")
            if until and until > now:  # 창이 살아 있을 때만 실패가 유효(지났으면 0)
                fails = int(_row_get(row, "claim_fails", 0) or 0)
        if fails >= max_fails:
            conn.rollback()
            return CLAIM_LOCKED, None
        grow = conn.execute(
            "SELECT COALESCE(SUM(claim_fails), 0) AS n FROM users "
            "WHERE claim_locked_until IS NOT NULL AND claim_locked_until > ?",
            (now,)).fetchone()
        if int(grow["n"] if grow else 0) >= global_max_fails:
            conn.rollback()
            return CLAIM_LOCKED_GLOBAL, None
        if not code_ok():
            until2 = (datetime.now(timezone.utc)
                      + timedelta(seconds=lock_sec)).isoformat()
            # 원자적 증가 — 창이 지났으면 1로 리셋(claim_fail_state의 창 판정과 동일).
            conn.execute(
                "UPDATE users SET "
                "claim_fails = CASE WHEN claim_locked_until IS NOT NULL "
                "  AND claim_locked_until > ? THEN claim_fails + 1 ELSE 1 END, "
                "claim_locked_until = ? WHERE id=?",
                (now, until2, uid))
            conn.commit()
            return CLAIM_BAD, None
        # 코드 일치 — 실패 누적을 지우고 성공 동작을 같은 트랜잭션에서 수행한다.
        conn.execute(
            "UPDATE users SET claim_fails=0, claim_locked_until=NULL WHERE id=?",
            (uid,))
        if action == "revoke":
            rows = conn.execute(
                "SELECT id, ward FROM users WHERE backup_owner=1 ORDER BY id"
            ).fetchall()
            conn.execute("UPDATE users SET backup_owner=0 WHERE backup_owner=1")
            conn.commit()
            return CLAIM_OK, [(int(r["id"]), r["ward"] or "") for r in rows]
        # action == "grant" — 이미 켜져 있었는지 확인해 감사 중복 적재를 막는다(FIX-5).
        me = conn.execute(
            "SELECT backup_owner FROM users WHERE id=?", (uid,)).fetchone()
        if me is None:
            conn.rollback()
            return CLAIM_OK, "missing"
        already = bool(_row_get(me, "backup_owner", 0))
        if not already:
            conn.execute("UPDATE users SET backup_owner=1 WHERE id=?", (uid,))
        conn.commit()
        return CLAIM_OK, ("already" if already else "granted")
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _find_by_login(conn: sqlite3.Connection, login_id: str) -> sqlite3.Row | None:
    """사번 우선, 다음 이메일로 계정 조회. 사번은 대문자 정규화(저장과 동일 규칙)."""
    u = conn.execute(
        "SELECT * FROM users WHERE empno=?", (login_id.upper(),)).fetchone()
    if u is None and "@" in login_id:
        u = conn.execute(
            "SELECT * FROM users WHERE email=?", (login_id.lower(),)).fetchone()
    return u


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> UserInfo:
    """Bearer 토큰에서 사용자 정보를 꺼낸다. 없거나 무효면 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "로그인이 만료되었습니다. 다시 로그인해 주세요.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")
    # 역할·병동은 토큰이 아닌 DB 기준 — 강등/승격이 즉시 반영된다(토큰은 신원+만료만).
    conn = _conn()
    try:
        if sub.startswith("id:"):
            u = conn.execute(
                "SELECT * FROM users WHERE id=?", (sub.removeprefix("id:"),)
            ).fetchone()
        else:
            # 구버전 토큰(sub=email) 호환 — TTL(12시간)이 지나면 자연 소멸
            u = conn.execute("SELECT * FROM users WHERE email=?", (sub,)).fetchone()
    finally:
        conn.close()
    if u is None:
        raise HTTPException(401, "계정을 찾을 수 없습니다. 다시 로그인해 주세요.")
    return _user_info(u)


def require_roles(*roles: str):
    """특정 역할만 허용하는 의존성 팩토리."""

    def dep(user: Annotated[UserInfo, Depends(get_current_user)]) -> UserInfo:
        if user.role not in roles:
            raise HTTPException(403, "권한이 없습니다.")
        return user

    return dep


# ---- 라우터 ----

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest) -> TokenResponse:
    """가입: 초대 코드가 있으면 해당 병동 staff, 없으면 '빈 병동 개설'만 허용(개설자=master).

    BEGIN IMMEDIATE로 확인·삽입을 직렬화해 동시 가입 시 마스터가 둘이 되는 경합을 막는다.
    """
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        code = body.invite_code.strip().upper()
        if code:
            # 초대 코드가 병동을 결정한다(임의 병동 지정 차단)
            row = conn.execute(
                "SELECT ward FROM ward_invites WHERE code=?", (code,)
            ).fetchone()
            if row is None:
                raise HTTPException(403, "초대 코드가 올바르지 않습니다. 부서장에게 확인하세요.")
            ward, role = row["ward"], "staff"
        else:
            ward = body.ward.strip()
            if not ward:
                raise HTTPException(
                    422, "병동명을 입력하거나 초대 코드를 입력하세요.")
            existing = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE ward=?", (ward,)
            ).fetchone()["c"]
            if existing > 0:
                raise HTTPException(
                    403, "이미 개설된 병동입니다. 부서장에게 초대 코드를 받아 가입하세요.")
            role = "master"  # 병동 개설자 = 그 병동의 마스터
            conn.execute(
                "INSERT OR REPLACE INTO ward_invites (ward, code) VALUES (?, ?)",
                (ward, _new_code()),
            )
        import re as _re
        # 사번은 대문자로 정규화해 저장 — "abc1"/"ABC1"이 서로 다른 계정이 되거나
        # 로그인 시 대소문자 오타로 실패하는 일을 막는다(숫자 사번은 영향 없음).
        empno = body.empno.strip().upper()
        email = body.email.lower() if body.email else None
        if empno and not _re.fullmatch(EMPNO_PATTERN, empno):
            raise HTTPException(422, "사번은 숫자/영문 3~20자로 입력하세요.")
        if not empno and not email:
            raise HTTPException(422, "사번(또는 이메일)을 입력하세요.")
        salt = secrets.token_bytes(16)
        try:
            cur = conn.execute(
                "INSERT INTO users (empno, email, name, role, ward, pw_hash, salt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (empno or None, email, body.name, role, ward,
                 _hash_pw(body.password, salt), salt.hex()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 어느 쪽이 겹쳤는지 구분해 안내 (사번 중복 확인 요구사항)
            if empno and conn.execute(
                    "SELECT 1 FROM users WHERE empno=?", (empno,)).fetchone():
                raise HTTPException(409, "이미 가입된 사번입니다. 본인 사번이 맞는지 확인하거나 로그인하세요.")
            raise HTTPException(409, "이미 가입된 이메일입니다.")
        user = conn.execute(
            "SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return TokenResponse(token=_make_token(user), role=user["role"],
                             name=user["name"], ward=user["ward"],
                             empno=user["empno"] or "")
    except HTTPException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@router.get("/invite", response_model=InviteInfo)
def get_invite(
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> InviteInfo:
    """내 병동의 초대 코드 조회 (관리자·마스터). 없으면 생성(기존 DB 마이그레이션)."""
    conn = _conn()
    try:
        return InviteInfo(ward=user.ward, code=_ensure_invite(conn, user.ward))
    finally:
        conn.close()


@router.post("/invite/rotate", response_model=InviteInfo)
def rotate_invite(
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> InviteInfo:
    """초대 코드 재발급 — 기존 코드는 즉시 무효 (유출 시 사용)."""
    conn = _conn()
    try:
        code = _new_code()
        conn.execute(
            "INSERT OR REPLACE INTO ward_invites (ward, code, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (user.ward, code),
        )
        conn.commit()
        return InviteInfo(ward=user.ward, code=code)
    finally:
        conn.close()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    login_id = body.login_id()
    if not login_id:
        raise HTTPException(422, "사번(또는 이메일)을 입력하세요.")
    conn = _conn()
    try:
        user = _find_by_login(conn, login_id)
        if user is None:
            raise HTTPException(401, "사번(또는 이메일)이나 비밀번호가 올바르지 않습니다.")
        expect = _hash_pw(body.password, bytes.fromhex(user["salt"]))
        if not secrets.compare_digest(expect, user["pw_hash"]):
            raise HTTPException(401, "사번(또는 이메일)이나 비밀번호가 올바르지 않습니다.")
        return TokenResponse(token=_make_token(user), role=user["role"],
                             name=user["name"], ward=user["ward"],
                             empno=user["empno"] or "")
    finally:
        conn.close()


@router.get("/me", response_model=UserInfo)
def me(user: Annotated[UserInfo, Depends(get_current_user)]) -> UserInfo:
    return user


@router.get("/ward-users", response_model=list[WardUser])
def ward_users(
    user: Annotated[UserInfo, Depends(require_roles("admin", "master"))],
) -> list[WardUser]:
    """같은 병동의 가입 사용자 목록 — 명단↔계정 연결 드롭다운용 (관리자·마스터 전용)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT empno, email, name, role FROM users WHERE ward=? ORDER BY name",
            (user.ward,),
        ).fetchall()
        return [WardUser(email=r["email"] or "", name=r["name"], role=r["role"],
                         empno=r["empno"] or "") for r in rows]
    finally:
        conn.close()


@router.post("/set-role", response_model=UserInfo)
def set_role(
    body: SetRoleRequest,
    _master: Annotated[UserInfo, Depends(require_roles("master"))],
) -> UserInfo:
    """역할 변경 — 마스터 전용 (예: 파트장을 admin으로 승격).

    같은 병동 사용자만 대상 — 병동 경계(테넌트) 밖의 계정은 존재 여부조차
    구분하지 않고 404로 처리한다(타 병동 권한 상승 차단).
    """
    body.validate_role()
    target = body.target()
    if not target:
        raise HTTPException(422, "대상 사번(또는 이메일)을 입력하세요.")
    conn = _conn()
    try:
        u = _find_by_login(conn, target)
        if u is None or u["ward"] != _master.ward:
            raise HTTPException(404, "해당 사번(이메일)의 사용자가 없습니다.")
        conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, u["id"]))
        conn.commit()
        u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        return _user_info(u)
    finally:
        conn.close()


@router.post("/set-empno", response_model=UserInfo)
def set_empno(
    body: SetEmpnoRequest,
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """사번 없는 구계정에 사번을 1회 등록 (본인만).

    등록 후에는 사번이 계정 식별자가 되므로, 이메일로 저장돼 있던 원티드 신청도
    사번 기준으로 함께 이관한다(명단 계정 연결은 부서장이 드롭다운에서 다시 선택).
    """
    empno = body.empno.strip().upper()
    conn = _conn()
    try:
        # 확인·갱신을 쓰기 락으로 직렬화 — 동시 등록 경쟁이 500(UNIQUE 위반)이나
        # 같은 계정의 이중 등록(먼저 이관된 신청 고아화)으로 새지 않게 한다.
        conn.execute("BEGIN IMMEDIATE")
        me = conn.execute("SELECT * FROM users WHERE id=?", (user.uid,)).fetchone()
        if me is None:
            raise HTTPException(401, "계정을 찾을 수 없습니다. 다시 로그인해 주세요.")
        if me["empno"]:
            raise HTTPException(409, "이미 사번이 등록된 계정입니다.")
        taken = conn.execute("SELECT 1 FROM users WHERE empno=?", (empno,)).fetchone()
        if taken:
            raise HTTPException(409, "이미 가입된 사번입니다. 본인 사번이 맞는지 확인하세요.")
        conn.execute("UPDATE users SET empno=? WHERE id=?", (empno, me["id"]))
        old_email = me["email"] or ""
        if old_email:
            try:
                conn.execute(
                    "UPDATE wanted_requests SET nurse_email=? WHERE nurse_email=? AND ward=?",
                    (empno, old_email, user.ward))
            except sqlite3.OperationalError:
                pass  # wanted 테이블이 아직 없으면(첫 사용) 이관할 것도 없다
            # 명단의 계정 연결(account_email)도 새 식별자로 이관 — 파트장이 다시
            # 저장하지 않아도 승인 신청 매칭·내 근무 연결이 끊기지 않게 한다.
            try:
                import json as _json
                row = conn.execute(
                    "SELECT data FROM rosters WHERE ward=?", (user.ward,)).fetchone()
                if row:
                    nurses = _json.loads(row["data"])
                    changed = False
                    for n in nurses:
                        if str(n.get("account_email", "")).strip().lower() == old_email.lower():
                            n["account_email"] = empno
                            changed = True
                    if changed:
                        conn.execute("UPDATE rosters SET data=? WHERE ward=?",
                                     (_json.dumps(nurses, ensure_ascii=False), user.ward))
            except (sqlite3.OperationalError, ValueError, TypeError, AttributeError):
                pass  # 명단 미저장/구조 손상 — 이관 생략(사번 등록 자체는 계속)
        conn.commit()
        u = conn.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
        return _user_info(u)
    except HTTPException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise HTTPException(409, "이미 가입된 사번입니다. 본인 사번이 맞는지 확인하세요.")
    finally:
        conn.close()
