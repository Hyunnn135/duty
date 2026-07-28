"""로그인/인증 시스템 (JWT + SQLite + 3역할 RBAC).

- 역할: master(마스터) / admin(관리자·파트장) / staff(일반 부서원)
- 규칙: **최초 가입자는 자동으로 master**, 이후 가입자는 staff.
  master가 /api/auth/set-role 로 admin 승격/변경.
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


def _secret() -> str:
    s = os.environ.get("DUTY_SECRET")
    if not s:
        s = "dev-secret-change-me"  # 배포 시 반드시 환경 변수로 교체
    return s


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
    return conn


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
    ).hex()


# ---- 스키마 ----

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, description="8자 이상")
    name: str = Field(..., min_length=1)
    ward: str = Field("", description="소속 병동 (예: 61)")


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
    return UserInfo(
        email=payload["sub"], name=payload.get("name", ""),
        role=payload.get("role", "staff"), ward=payload.get("ward", ""),
    )


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
    conn = _conn()
    try:
        first = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
        role = "master" if first else "staff"
        salt = secrets.token_bytes(16)
        try:
            conn.execute(
                "INSERT INTO users (email, name, role, ward, pw_hash, salt) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (body.email.lower(), body.name, role, body.ward,
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
