"""이메일 알림 (Phase 5) — SMTP 기반, 표준 라이브러리만 사용.

설정은 환경 변수로만 주입하며(코드/저장소에 자격증명 없음), **미설정 시 조용히 무시**된다.
그래서 로컬 개발·테스트·SMTP 미구성 배포에서도 앱이 정상 동작한다.

환경 변수:
  SMTP_HOST      SMTP 서버 (예: smtp.gmail.com). 없으면 이메일 기능 비활성.
  SMTP_PORT      기본 587(STARTTLS). 465면 SSL 사용.
  SMTP_USER      로그인 사용자 (선택 — 없으면 인증 없이 전송 시도)
  SMTP_PASSWORD  로그인 비밀번호/앱 비밀번호 (Secret Manager 권장)
  SMTP_FROM      발신자 주소 (없으면 SMTP_USER 사용)
  SMTP_STARTTLS  "0"이면 STARTTLS 비활성 (기본 활성)
  NOTIFY_ON_PUBLISH  "1"이면 근무표 발행 시 병동 구성원에게 메일 (기본 꺼짐)

배포 시 SMTP 값은 docs/DEPLOY.md의 Secret Manager/환경 변수로 주입한다.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("duty.email")


def _port() -> int:
    """SMTP_PORT를 안전하게 파싱. 비정상 값이면 기본 587로 폴백(예외 전파 방지)."""
    raw = (os.environ.get("SMTP_PORT", "") or "").strip()
    try:
        return int(raw) if raw else 587
    except ValueError:
        log.warning("SMTP_PORT 값이 올바르지 않음(%r) — 587로 폴백", raw)
        return 587


def _cfg() -> dict:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    return {
        "host": host,
        "port": _port(),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": (os.environ.get("SMTP_FROM", "").strip() or user),
        "starttls": os.environ.get("SMTP_STARTTLS", "1") not in ("0", "false", "False"),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["host"] and c["from"])


def notify_on_publish() -> bool:
    return os.environ.get("NOTIFY_ON_PUBLISH", "0") in ("1", "true", "True")


def send_email(to: str | list[str], subject: str, body: str) -> bool:
    """이메일 전송. 미설정·실패 시 예외 없이 False 반환(요청 흐름을 막지 않는다)."""
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [r for r in recipients if r]
    if not recipients:
        return False
    c = _cfg()
    if not (c["host"] and c["from"]):
        log.info("SMTP 미설정 — 이메일 생략: %s", subject)
        return False
    msg = EmailMessage()
    msg["From"] = c["from"]
    # 단체 발송 시 수신자 주소가 서로 노출되지 않도록 To에는 발신자만 적고
    # 실제 수신자는 봉투(to_addrs)로만 전달한다(BCC 방식).
    msg["To"] = recipients[0] if len(recipients) == 1 else c["from"]
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=10, context=ctx) as s:
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=10) as s:
                if c["starttls"]:
                    s.starttls(context=ctx)
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg, to_addrs=recipients)
        log.info("이메일 전송: %s → %d명", subject, len(recipients))
        return True
    except Exception as e:  # 전송 실패가 요청을 깨지 않도록
        log.warning("이메일 전송 실패(%s): %s", subject, e)
        return False
