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
> 그래서 SQLite 파일을 **GCS 버킷 볼륨(/data)** 에 두고 `--max-instances 1`(동시 인스턴스
> 1개, `--min-instances 0` = scale-to-zero로 비용 절감)로 고정한다. 한 병동 규모 MVP에는 충분하다. 사용자가 늘면(여러 병동·동시성)
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

## 7.6 데이터 백업 권한 `DUTY_BACKUP_OWNER_UID` (설정하지 않으면 기능 잠김)

`GET /api/admin/backup` 은 병동 **전체 데이터(실명·사번·피드백 원문 포함)** 를 ZIP으로
반출한다. 그래서 `role=="master"` 만으로는 열리지 않는다 — 마스터는 **병동마다** 생기므로
(병동 개설자 = 그 병동의 마스터) **누구나 새 병동을 열면 master가 된다**. 즉 role 조건은
실질 방어가 아니고, 실제로 막는 것은 아래 uid 목록 하나뿐이다.

### 왜 사번·이메일이 아니라 uid인가 (검수부 침투 재현 결과)

| 예전 방식(`DUTY_BACKUP_OWNER` = 사번/이메일) | 실제로 뚫린 방법 |
|---|---|
| 사번 문자열로 지정 | **선점**: 그 사번이 아직 아무 계정에도 없는 동안, 공격자가 새 병동을 열어 master가 된 뒤 그 사번을 자기 계정에 등록 → 전체 DB 반출 성공 |
| 이메일 문자열로 지정 | **유니코드 접힘**: 이메일은 소문자로 저장하는데 비교는 대문자로 해서, 점 없는 `ı`(U+0131)를 쓴 **다른 이메일**이 같은 대문자열로 접혀 통과 → 전체 DB 반출 성공 |

`users.id`(uid)는 가입할 때 서버가 부여하고 바뀌지 않으며 **사용자가 값을 고를 수 없다.**
그래서 위 두 우회가 성립하지 않는다. 문자열 판정은 하위 호환도 남기지 않았다 —
남기는 순간 그 경로가 그대로 취약점이 되기 때문이다. 예전 `DUTY_BACKUP_OWNER` 변수는
**설정해도 아무 효과가 없다**(남아 있으면 삭제할 것).

### 설정 순서 — **가입 먼저 → uid 확인 → 환경변수** (순서를 지킬 것)

1. **허가할 사람이 먼저 앱에 가입한다.** (첫 가입자가 병동을 개설하면 그 병동의 master)
2. 그 사람이 로그인 → **⚙️ 설정 → 🆔 내 계정 번호** 카드에 있는 **숫자**를 배포
   담당자에게 알려준다. (서버에 접속할 필요 없이 화면에서 확인된다)
3. 배포 담당자가 그 숫자를 `DUTY_BACKUP_OWNER_UID` 에 넣고 재배포한다.
4. 그 사람이 다시 로그인하면 설정 탭에 **💾 데이터 백업** 카드가 나타난다.

> 환경변수를 **먼저** 설정하는 순서는 쓰지 않는다. 예전 방식에서 선점 창이 열렸던 지점이고,
> uid는 가입 전에는 존재하지도 않는 값이라 애초에 미리 적을 수 없다.

| 조건 | 결과 |
|------|------|
| `DUTY_BACKUP_OWNER_UID` 미설정·빈 값 | **전원 403** — 기본 개방 없음 |
| 값에 정수가 아닌 토큰이 하나라도 섞임(오타 등) | **전원 403** — 설정 전체를 무효로 본다 |
| 목록에 있는 uid + `role=="master"` | 200 (ZIP) |
| 목록에 있으나 admin·staff | 403 |
| 목록에 없는 master(다른 병동 개설자 포함) | 403 |
| 토큰 없음·만료 | 401 |

```bash
# 값은 허가할 계정의 uid(숫자). 여러 명이면 콤마로 구분한다.
#   DUTY_BACKUP_OWNER_UID=<uid>
#   DUTY_BACKUP_OWNER_UID=<uid1>,<uid2>
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars DUTY_BACKUP_OWNER_UID=<uid>
```

> uid는 사번과 달리 개인정보가 아니라 가입 순번이지만, 허가 대상이 누구인지 굳이 드러낼
> 이유가 없으므로 값은 배포 플랫폼의 환경변수에만 둔다. GitHub Actions CD를 쓰면 §7.5의
> `EXTRA_ENV` 변수에 `,DUTY_BACKUP_OWNER_UID=<uid>` 를 이어붙인다(맨 앞 콤마 주의).
> 허가 계정을 바꾸려면 이 환경변수만 고치면 되고 재배포 외 조치는 없다.

**백업 이력**: `backup_log` 테이블에 한 행씩 쌓인다(actor는 `uid:<번호>` 만 — 실명·사번은
남기지 않는다). `status` 값의 뜻은 다음과 같다.

| status | 언제 남는가 |
|---|---|
| `pending` | 내려받기 요청을 받아 파일을 만들기 **직전**. 아직 전달되지 않았다 |
| `ok` | 브라우저가 파일을 **끝까지 받은 뒤** 보내는 확정 신호(`POST /api/admin/backup/confirm`, 받은 바이트 수 대조)를 처리했을 때 |
| `fail` | 스냅샷 손상·시간초과 등으로 만들지 못했을 때(응답은 500) |
| `denied` | 허가되지 않은 계정이 반출을 시도했을 때(uid·시각만 기록 — 침입 시도 탐지용) |

`pending` 이 `ok` 로 바뀌지 않은 행은 **다운로드가 중간에 끊겼다는 뜻**이다. 이 경우
경고는 꺼지지 않는다(예전에는 전송 전에 `ok`를 남겨, 파일이 없는데도 30일간 경고가 꺼졌다).

`GET /api/admin/backup/status` 가 마지막 **성공(`ok`)** 시각과 경고 단계(KST 경과일 기준
`ok`<30일 / `warn` 30~44 / `critical` ≥45일·이력 0건·**경과일이 음수**(서버 시계 역행))를
돌려주고, 화면 팝업·배너가 이 값을 따른다.

### 복구 절차 (수작업 — 업로드 기능은 없음)

> **먼저 알아둘 것**: 복구하면 **백업 시각 이후에 입력된 데이터는 모두 사라진다**
> (그 뒤 만든 근무표·원티드 신청·가입 계정·피드백). 복구 전에 마스터에게 "언제 시점으로
> 되돌아가는지"를 알리고 동의를 받는다.

1. 마스터에게 받은 ZIP에서 **`duty.db`** 만 꺼낸다(`tables/*.csv`는 사람이 보는 사본이라
   복구에 쓰지 않는다. 비밀번호·초대 코드 자리는 `(생략)`으로 가려져 있어 복구에 쓸 수도 없다).
2. **교체하기 전에 백업본이 성한지 확인한다.** 깨진 파일로 교체하면 되돌릴 수 없다.
   ```bash
   sqlite3 duty.db "PRAGMA integrity_check;"   # 'ok' 가 아니면 중단하고 다른 백업본을 쓴다
   sqlite3 duty.db "SELECT COUNT(*) FROM users; SELECT COUNT(*) FROM rosters;"
   ```
3. 서비스를 잠시 멈추거나 접속을 막는다(쓰기 중 교체 금지).
4. 볼륨의 기존 파일을 **지우지 말고 옆으로 치운다** — `duty.db`, `duty.db-wal`, `duty.db-shm`
   **3개를 같이** `duty.db.bak` 등으로 옮긴다. `-wal`/`-shm`이 남으면 새 파일과 섞여 손상된다.
   (치워둔 3개가 복구 실패 시의 **원복 수단**이다. 복구가 확실히 성공할 때까지 지우지 않는다.)
5. 백업본 `duty.db`를 `DUTY_DB` 경로(Cloud Run: `/data/duty.db`, Railway: 볼륨 `/data/duty.db`)에 놓는다.
6. 서비스를 다시 띄우고 `/health` → 로그인 → 명단·근무표가 보이는지 확인한다.
7. **실패하면 원복**: 서비스를 다시 멈추고, 5에서 놓은 파일과 새로 생긴 `-wal`/`-shm`을 지운 뒤
   4에서 치워둔 3개를 원래 이름으로 되돌리고 서비스를 띄운다.
8. 복구 직후에는 **"데이터를 한 번도 백업하지 않았습니다" 빨간 배너가 뜬다.** 백업본 안의
   이력은 `pending` 상태로만 남아 있어(그 백업이 만들어진 시점 확인용) 성공 이력이 없기
   때문이며, **정상이다.** 마스터에게 안내하고 **즉시 1회 백업을 받게 한다** — 그러면 사라진다.
9. 복구가 끝나면 임시로 꺼내둔 백업 파일 사본을 삭제한다(개인정보 잔존 금지).

> 백업은 **`VACUUM INTO`**(잠금·구버전 SQLite로 실패하면 `Connection.backup()`)로 뜬 일관된
> 스냅샷이라 WAL이 반영된 단일 파일이다. 운영 중에도 안전하게 받을 수 있고, 받는 동안 앱은
> 계속 동작한다. 만들어진 스냅샷은 내려주기 전에 `PRAGMA quick_check`로 검증하며, 통과하지
> 못하면 200이 아니라 **500 + `backup_log(status='fail')`** 이 된다(손상본을 "성공"으로
> 내려주지 않는다).

#### Railway에서 볼륨에 파일을 넣는 방법

Railway는 파일 관리자 화면이 없어서 **브라우저만으로는 볼륨의 파일을 바꿀 수 없다.**
아래 중 하나로 한다(배포 담당자 작업 — 마스터가 직접 할 일이 아니다).

```bash
npm i -g @railway/cli        # 1) CLI 설치
railway login                # 2) 브라우저가 열리며 로그인
railway link                 # 3) 대상 프로젝트·서비스 선택

# 4a) 대화형 파일 브라우저(업로드·내려받기·삭제)
railway volume browse /

# 4b) 비대화형으로 바로 올리기·내려받기
railway volume files list /data
railway volume files download /data/duty.db ./duty-before-restore.db   # 교체 전 원본 확보
railway volume files upload ./duty.db /data/duty.db
```

- CLI 버전에 따라 하위 명령 이름이 다를 수 있다. `railway volume --help` 로 확인한다.
- 대안: `scp ./duty.db <서비스도메인>@ssh.railway.com:/data/duty.db` (Railway SSH).
- **`-wal`·`-shm` 정리를 잊지 말 것** — 위 4단계대로 세 파일을 함께 치운 뒤 올린다.
- 서비스 재시작은 Railway 대시보드의 **Deployments → Restart** 로 한다.

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
   - 백업 권한(`DUTY_BACKUP_OWNER_UID`)은 **여기서 설정하지 않는다.** 가입 전에는 uid가
     존재하지 않기 때문이다 — 6번 이후 8번에서 설정한다(§7.6).
4. 서비스 우클릭(또는 Settings) → **Attach Volume** → mount path `/data`
   (SQLite 영속화 — 볼륨 없이 재시작하면 데이터가 사라진다!)
5. **Settings → Networking → Generate Domain** → 공개 URL 발급
6. `https://<도메인>/health` 가 `{"status":"ok"}` 면 성공. 첫 가입자가 병동 개설(마스터).
7. 백업을 맡을 사람이 **가입한 뒤** 로그인 → **⚙️ 설정 → 🆔 내 계정 번호** 의 숫자를 알려준다.
8. **Variables** 탭에 `DUTY_BACKUP_OWNER_UID` = 그 숫자를 추가한다(§7.6). 재배포가 끝나고
   다시 로그인하면 설정 탭에 **💾 데이터 백업** 카드가 나타난다.
   - 볼륨의 파일을 직접 바꿔야 할 때(복구)는 브라우저만으로는 안 되고 Railway CLI가
     필요하다 — §7.6의 "Railway에서 볼륨에 파일을 넣는 방법" 참고.

- GCP 워크플로(deploy.yml)는 `GCP_PROJECT_ID` 변수가 없으면 자동 스킵되므로 충돌 없음.
- 이후 GCP 승인이 나면: 본 문서 §1~8로 Cloud Run에 띄우고, Railway 볼륨의
  `/data/duty.db` 파일을 GCS 버킷으로 복사하면 데이터 이전 완료.
