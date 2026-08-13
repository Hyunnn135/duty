# 간호사 근무표 자동 생성 — 프로덕션 이미지 (Cloud Run 대상)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DUTY_DB=/data/duty.db

WORKDIR /app

# 런타임 의존성만 설치 (테스트 도구 제외)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드
COPY app ./app

# 데이터 디렉터리(볼륨 미마운트 시에도 동작).
# Railway 등은 볼륨을 root 소유로 마운트해 비-root 프로세스가 /data에 쓸 수 없다
# (SQLite 생성 실패 → 가입/저장 500). 컨테이너 격리 하에 root로 실행해 호환성을 확보한다.
RUN mkdir -p /data

EXPOSE 8080

# Cloud Run/Railway가 주입하는 $PORT를 따른다. OR-Tools 임포트로 콜드스타트 ~8초.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
