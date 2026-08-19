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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ward_invites (
                ward TEXT PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.commit()
        _INITED_DBS.add(path)
    return conn


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


class UserInfo(BaseModel):
    email: str
    name: str
    role: str
    ward: str
    empno: str = ""

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


def _user_info(u: sqlite3.Row) -> UserInfo:
    return UserInfo(email=u["email"] or "", name=u["name"], role=u["role"],
                    ward=u["ward"], empno=u["empno"] or "")


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
        if user.empno:
            raise HTTPException(409, "이미 사번이 등록된 계정입니다.")
        taken = conn.execute("SELECT 1 FROM users WHERE empno=?", (empno,)).fetchone()
        if taken:
            raise HTTPException(409, "이미 가입된 사번입니다. 본인 사번이 맞는지 확인하세요.")
        conn.execute("UPDATE users SET empno=? WHERE email=? AND ward=?",
                     (empno, user.email, user.ward))
        try:
            conn.execute(
                "UPDATE wanted_requests SET nurse_email=? WHERE nurse_email=? AND ward=?",
                (empno, user.email, user.ward))
        except sqlite3.OperationalError:
            pass  # wanted 테이블이 아직 없으면(첫 사용) 이관할 것도 없다
        # 명단의 계정 연결(account_email)도 새 식별자로 이관 — 파트장이 다시 저장하지
        # 않아도 승인 신청 매칭·내 근무 연결이 끊기지 않게 한다.
        try:
            import json as _json
            row = conn.execute(
                "SELECT data FROM rosters WHERE ward=?", (user.ward,)).fetchone()
            if row:
                nurses = _json.loads(row["data"])
                changed = False
                for n in nurses:
                    if str(n.get("account_email", "")).strip().lower() == user.email.lower():
                        n["account_email"] = empno
                        changed = True
                if changed:
                    conn.execute("UPDATE rosters SET data=? WHERE ward=?",
                                 (_json.dumps(nurses, ensure_ascii=False), user.ward))
        except (sqlite3.OperationalError, ValueError):
            pass  # 명단 미저장/파싱 불가 — 이관할 것 없음
        conn.commit()
        u = conn.execute("SELECT * FROM users WHERE empno=?", (empno,)).fetchone()
        return _user_info(u)
    finally:
        conn.close()
