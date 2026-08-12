# 배포 가이드 — CI/CD & Cloud Run

간호사 근무표 앱(FastAPI + OR-Tools)을 **GitHub Actions → Google Cloud Run**으로
자동 배포하는 설정이다. 비용은 소규모 사용 시 사실상 무료에 가깝다(PLAN 7.5.2).

- **CI** (`.github/workflows/ci.yml`): 모든 push·PR에서 테스트 실행.
- **CD** (`.github/workflows/deploy.yml`): `main` 병합(또는 수동 실행) 시 테스트 →
  컨테이너 빌드(Cloud Build) → Cloud Run 배포.

---

## 0. 개요 · 아키텍처

```
GitHub push(main) ──▶ GitHub Actions
                         │  (Workload Identity Federation, 키 없음)
                         ▼
                    Cloud Build (Dockerfile 빌드)
                         ▼
                    Cloud Run 서비스 (단일 인스턴스)
                         │  DUTY_DB=/data/duty.db
                         ▼
                    GCS 버킷 볼륨 (/data 마운트, SQLite 영속화)
                    Secret Manager: duty-secret (JWT 서명키)
```

> **DB 영속화 주의**: Cloud Run 컨테이너는 상태가 없어 재시작 시 로컬 파일이 사라진다.
> 그래서 SQLite 파일을 **GCS 버킷 볼륨(/data)** 에 두고 `--min/max-instances 1`(단일
> 인스턴스)로 고정한다. 한 병동 규모 MVP에는 충분하다. 사용자가 늘면(여러 병동·동시성)
> **Postgres(Supabase/Cloud SQL)로 이관**을 권장한다(§6).

---

## 1. 사전 준비

- GCP 프로젝트(결제 연결됨) — `PROJECT_ID`
- 로컬에 `gcloud` CLI 설치 & 로그인: `gcloud auth login && gcloud config set project PROJECT_ID`
- 리전 결정(예: `asia-northeast3` 서울)

아래 변수를 셸에 설정해두면 편하다:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export REGION=asia-northeast3
export SERVICE=duty
export BUCKET=${PROJECT_ID}-duty-data
export REPO_OWNER_SLASH_NAME=Hyunnn135/duty   # GitHub owner/repo
```

> **⚡ 빠른 길**: §2~6을 한 번에 실행하는 스크립트를 제공한다. `gcloud auth login` +
> `gcloud config set project <ID>` 후 아래를 실행하면 API 활성화·버킷·시크릿·서비스계정·
> WIF까지 만들고, **GitHub에 등록할 값들을 출력**한다. 그다음 §7만 하면 된다.
> ```bash
> bash scripts/gcp_setup.sh          # REGION/SERVICE 등은 환경변수로 덮어쓰기 가능
> ```
> 아래 §2~6은 이 스크립트가 자동으로 하는 일을 단계별로 설명한 것이다(수동 실행/이해용).

## 2. API 활성화

```bash
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com storage.googleapis.com
```

## 3. SQLite 영속화용 GCS 버킷

```bash
gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
```

## 4. JWT 서명키 시크릿

```bash
# 강력한 랜덤 키 생성 후 Secret Manager에 저장
python -c "import secrets;print(secrets.token_urlsafe(48))" | \
  gcloud secrets create duty-secret --data-file=-
# 키를 교체할 때: gcloud secrets versions add duty-secret --data-file=-
```

## 5. 배포용 서비스 계정 + 런타임 권한

```bash
# (a) CI가 사용할 배포 서비스 계정
gcloud iam service-accounts create duty-deployer \
  --display-name="Duty CI deployer"
export DEPLOYER=duty-deployer@${PROJECT_ID}.iam.gserviceaccount.com

for ROLE in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin \
  roles/storage.admin \
  roles/iam.serviceAccountUser ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER}" --role="$ROLE"
done

# (b) Cloud Run 런타임 서비스 계정(기본 compute SA)에
#     시크릿 접근 + 버킷 읽기/쓰기 권한 부여
export RUNTIME=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com
gcloud secrets add-iam-policy-binding duty-secret \
  --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:${RUNTIME}" --role=roles/storage.objectAdmin
```

## 6. Workload Identity Federation (키리스 인증)

GitHub Actions가 서비스 계정 키 파일 없이 GCP에 인증하도록 연동한다.

```bash
# 풀 생성
gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub pool"

export POOL_ID=$(gcloud iam workload-identity-pools describe github-pool \
  --location=global --format='value(name)')

# 공급자 생성 (이 저장소에서 오는 토큰만 허용)
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --display-name="GitHub provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO_OWNER_SLASH_NAME}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# 배포 SA를 이 저장소가 임시토큰으로 위임할 수 있도록 바인딩
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${REPO_OWNER_SLASH_NAME}"

# GitHub 시크릿에 넣을 공급자 리소스명 출력
gcloud iam workload-identity-pools providers describe github-provider \
  --location=global --workload-identity-pool=github-pool \
  --format='value(name)'
```

## 7. GitHub 저장소에 변수·시크릿 등록

**Settings → Secrets and variables → Actions**

Repository **secrets**:
| 이름 | 값 |
|------|-----|
| `GCP_WIF_PROVIDER` | §6 마지막 명령이 출력한 공급자 리소스명 (`projects/…/providers/github-provider`) |
| `GCP_SA_EMAIL` | `duty-deployer@PROJECT_ID.iam.gserviceaccount.com` |

Repository **variables**:
| 이름 | 값(예) |
|------|--------|
| `GCP_PROJECT_ID` | `PROJECT_ID` |
| `GCP_REGION` | `asia-northeast3` |
| `SERVICE_NAME` | `duty` |
| `DATA_BUCKET` | `${PROJECT_ID}-duty-data` |

## 7.5 이메일 알림 (선택)

피드백→마스터, 원티드 승인/반려→신청자, (옵트인) 근무표 발행→병동 구성원 메일 알림.
SMTP 미설정 시 앱은 정상 동작하며 메일만 생략된다.

```bash
# 예: Gmail SMTP (2단계 인증 후 '앱 비밀번호' 발급 필요)
printf '앱비밀번호16자리' | gcloud secrets create smtp-password --data-file=-
gcloud secrets add-iam-policy-binding smtp-password \
  --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor
```

| 환경 변수 | 의미 |
|-----------|------|
| `SMTP_HOST`/`SMTP_PORT` | SMTP 서버·포트(587 STARTTLS, 465 SSL) |
| `SMTP_USER`/`SMTP_PASSWORD` | 로그인 계정·비밀번호(앱 비밀번호). PASSWORD는 Secret Manager |
| `SMTP_FROM` | 발신자 주소(미지정 시 SMTP_USER) |
| `NOTIFY_ON_PUBLISH` | `1`이면 발행 시 병동 구성원에게 메일(기본 꺼짐) |

> 이 값들을 지정하지 않으면 이메일 기능은 자동 비활성(수신함만 동작).

**GitHub Actions CD에 적용** (워크플로 수정 불필요): 배포 명령의 `--set-env-vars`·
`--set-secrets`에 아래 저장소 **변수**가 그대로 이어붙는다. **각 값은 앞에 콤마로 시작**해야
기존 값(`DUTY_DB`·`DUTY_SECRET`)과 병합된다. 비워두면 이메일 없이 배포된다.

| 변수 | 값(예, 맨 앞 콤마 주의) |
|------|------|
| `EXTRA_ENV` | `,SMTP_HOST=smtp.gmail.com,SMTP_PORT=587,SMTP_USER=you@gmail.com,SMTP_FROM=you@gmail.com,NOTIFY_ON_PUBLISH=1` |
| `EXTRA_SECRETS` | `,SMTP_PASSWORD=smtp-password:latest` |

## 8. 첫 배포

- `main`에 병합하면 자동 실행되거나,
- **Actions 탭 → Deploy to Cloud Run → Run workflow** 로 수동 실행.

로컬에서 먼저 확인하고 싶다면:

```bash
gcloud run deploy "$SERVICE" --source . --region "$REGION" \
  --allow-unauthenticated --execution-environment gen2 \
  --cpu 2 --memory 2Gi --concurrency 8 --timeout 300 --min-instances 0 --max-instances 1 \
  --set-env-vars DUTY_DB=/data/duty.db \
  --set-secrets DUTY_SECRET=duty-secret:latest \
  --add-volume name=data,type=cloud-storage,bucket="$BUCKET" \
  --add-volume-mount volume=data,mount-path=/data
```

배포 후 출력된 URL의 `/health`가 `{"status":"ok"}`를 반환하면 성공. 첫 접속 사용자가
자동으로 **마스터**가 된다.

---

## 로컬에서 컨테이너 검증 (선택)

```bash
docker build -t duty:local .
docker run --rm -p 8080:8080 -e DUTY_SECRET=devsecret -e DUTY_DB=/data/duty.db duty:local
# 다른 터미널: curl localhost:8080/health
```

## 운영 메모

- **비용(중요)**: 기본값을 **`--min-instances 0`(scale-to-zero)** 로 둔다. 한 병동이 월 몇 번
  생성하는 저트래픽에서는 안 쓸 때 인스턴스가 0으로 내려가 **사실상 무료**(Cloud Run 무료 티어
  vCPU-s·GiB-s·요청 범위 내). GCS·시크릿·빌드도 무료 티어. 정확한 비용은 GCP 요금 계산기로 확인.
- **콜드스타트 트레이드오프**: scale-to-zero면 오래 안 쓰다 첫 요청 시 OR-Tools 임포트로 **~8~10초**
  대기가 생긴다. 월 단위로 쓰는 도구라 보통 감수 가능. **항상 즉시 응답이 필요하면**
  `--min-instances 1`로 바꾼다(단, 인스턴스 상시 과금 → 월 $30~45 수준). 항상 켤 땐 SQLite
  단일 인스턴스 일관성도 함께 확보된다(scale-to-zero도 `--max-instances 1`이라 동시 쓰기는 없음).
- **동시성·타임아웃**: 근무표 생성은 CPU를 많이 쓰므로 `--concurrency 8`, `--cpu 2`로 제한.
  운영 모드(정확 인원 패턴) 생성은 최대 120초 계산하므로 요청 타임아웃을 `--timeout 300`으로
  여유를 둔다(콜드스타트 ~8초 + 계산 120초 + 여유). 메모리는 OR-Tools 대규모 풀이를 위해 `2Gi`.

## 확장(Postgres 이관) 경로 — 사용자·병동이 늘면

SQLite+단일 인스턴스는 한 병동 MVP용이다. 여러 병동·다수 동시 사용자로 커지면:

1. Supabase(무료 티어) 또는 Cloud SQL(Postgres) 프로비저닝.
2. `app/auth.py`·`app/storage.py`의 `sqlite3` 접근을 Postgres 드라이버로 교체
   (스키마는 이미 `ward`로 테넌트 스코프되어 있어 RLS 적용이 쉽다 — PLAN 7.5.5).
3. `--min/max-instances` 제한을 풀고 자동 확장 활성화, GCS 볼륨 마운트 제거.
4. `DUTY_DB` 대신 `DATABASE_URL` 환경변수로 연결.

이 변경은 라우터·프론트엔드를 건드리지 않고 데이터 접근 계층만 교체하면 된다.

---

## 부록 A. Railway 배포 (GCP 본인확인 대기 등 대안 경로)

GCP 한국 계정 본인확인(신분증·카드 사진, 며칠 소요)이 어려울 때, **브라우저만으로**
Railway(railway.app)에 같은 Docker 이미지를 배포할 수 있다. 월 약 $5(Hobby, 사용량 포함).

1. https://railway.app → **Login with GitHub** → Hobby 플랜 결제 카드 등록(신분증 불필요)
2. **New Project → Deploy from GitHub repo** → `Hyunnn135/duty` 선택(권한 허용)
   → Dockerfile을 자동 감지해 빌드·배포한다 (main 브랜치 기준)
3. 서비스 클릭 → **Variables** 탭:
   - `DUTY_SECRET` = 아무도 모르는 긴 무작위 문장(JWT 서명키 — 절대 공유 금지)
   - (`DUTY_DB`는 Dockerfile 기본값 `/data/duty.db` 사용, `PORT`는 Railway가 자동 주입)
4. 서비스 우클릭(또는 Settings) → **Attach Volume** → mount path `/data`
   (SQLite 영속화 — 볼륨 없이 재시작하면 데이터가 사라진다!)
5. **Settings → Networking → Generate Domain** → 공개 URL 발급
6. `https://<도메인>/health` 가 `{"status":"ok"}` 면 성공. 첫 가입자가 병동 개설(마스터).

- GCP 워크플로(deploy.yml)는 `GCP_PROJECT_ID` 변수가 없으면 자동 스킵되므로 충돌 없음.
- 이후 GCP 승인이 나면: 본 문서 §1~8로 Cloud Run에 띄우고, Railway 볼륨의
  `/data/duty.db` 파일을 GCS 버킷으로 복사하면 데이터 이전 완료.
