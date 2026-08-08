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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            ward TEXT DEFAULT '',
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ward_invites (
            ward TEXT PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
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
    code = _new_code()
    conn.execute(
        "INSERT INTO ward_invites (ward, code) VALUES (?, ?)", (ward, code)
    )
    conn.commit()
    return code


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
    ).hex()


# ---- 스키마 ----

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, description="8자 이상")
    name: str = Field(..., min_length=1)
    ward: str = Field("", description="새 병동 개설 시 병동명 (예: 61) — 개설자가 마스터가 됨")
    invite_code: str = Field("", description="기존 병동 가입용 초대 코드 (부서장에게 받음)")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    token: str
    role: str
    name: str
    ward: str


class UserInfo(BaseModel):
    email: str
    name: str
    role: str
    ward: str


class SetRoleRequest(BaseModel):
    email: EmailStr
    role: str

    def validate_role(self) -> None:
        if self.role not in ROLES:
            raise HTTPException(422, f"역할은 {ROLES} 중 하나여야 합니다.")


class WardUser(BaseModel):
    email: str
    name: str
    role: str


class InviteInfo(BaseModel):
    ward: str
    code: str


# ---- 토큰/의존성 ----

def _make_token(user: sqlite3.Row) -> str:
    payload = {
        "sub": user["email"],
        "name": user["name"],
        "role": user["role"],
        "ward": user["ward"],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


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
        u = conn.execute(
            "SELECT name, role, ward FROM users WHERE email=?", (sub,)
        ).fetchone()
    finally:
        conn.close()
    if u is None:
        raise HTTPException(401, "계정을 찾을 수 없습니다. 다시 로그인해 주세요.")
    return UserInfo(email=sub, name=u["name"], role=u["role"], ward=u["ward"])


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
        salt = secrets.token_bytes(16)
        try:
            conn.execute(
                "INSERT INTO users (email, name, role, ward, pw_hash, salt) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (body.email.lower(), body.name, role, ward,
                 _hash_pw(body.password, salt), salt.hex()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "이미 가입된 이메일입니다.")
        user = conn.execute(
            "SELECT * FROM users WHERE email=?", (body.email.lower(),)
        ).fetchone()
        return TokenResponse(token=_make_token(user), role=user["role"],
                             name=user["name"], ward=user["ward"])
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
    conn = _conn()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email=?", (body.email.lower(),)
        ).fetchone()
        if user is None:
            raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
        expect = _hash_pw(body.password, bytes.fromhex(user["salt"]))
        if not secrets.compare_digest(expect, user["pw_hash"]):
            raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
        return TokenResponse(token=_make_token(user), role=user["role"],
                             name=user["name"], ward=user["ward"])
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
            "SELECT email, name, role FROM users WHERE ward=? ORDER BY name",
            (user.ward,),
        ).fetchall()
        return [WardUser(email=r["email"], name=r["name"], role=r["role"]) for r in rows]
    finally:
        conn.close()


@router.post("/set-role", response_model=UserInfo)
def set_role(
    body: SetRoleRequest,
    _master: Annotated[UserInfo, Depends(require_roles("master"))],
) -> UserInfo:
    """역할 변경 — 마스터 전용 (예: 파트장을 admin으로 승격)."""
    body.validate_role()
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE users SET role=? WHERE email=?", (body.role, body.email.lower())
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "해당 이메일의 사용자가 없습니다.")
        u = conn.execute(
            "SELECT * FROM users WHERE email=?", (body.email.lower(),)
        ).fetchone()
        return UserInfo(email=u["email"], name=u["name"], role=u["role"], ward=u["ward"])
    finally:
        conn.close()
