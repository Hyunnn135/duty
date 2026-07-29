#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 간호사 근무표 앱 — GCP 원타임 배포 셋업 (docs/DEPLOY.md §2~6 자동화)
#
# 사용법:
#   1) gcloud 설치 + 로그인:  gcloud auth login
#   2) 프로젝트 선택:         gcloud config set project <PROJECT_ID>
#   3) 실행:                  bash scripts/gcp_setup.sh
#
# 대부분 명령이 멱등(재실행 안전)하도록 작성했다. 이미 존재하면 건너뛴다.
# 끝에서 GitHub에 등록할 secrets/variables 값을 출력한다.
# ---------------------------------------------------------------------------
set -euo pipefail

# ---- 설정(필요 시 환경변수로 덮어쓰기) ----
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-northeast3}"          # 서울
SERVICE="${SERVICE:-duty}"
REPO="${REPO:-Hyunnn135/duty}"               # GitHub owner/repo
BUCKET="${BUCKET:-${PROJECT_ID}-duty-data}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "❌ PROJECT_ID를 찾을 수 없습니다. 'gcloud config set project <ID>' 후 다시 실행하세요." >&2
  exit 1
fi
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DEPLOYER="duty-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "▶ PROJECT=${PROJECT_ID}  REGION=${REGION}  SERVICE=${SERVICE}  REPO=${REPO}"
echo "▶ BUCKET=${BUCKET}"
echo

# 헬퍼: 존재 여부로 건너뛰는 실행
have() { eval "$1" >/dev/null 2>&1; }

# ---- §2 API 활성화 ----
echo "▶ [1/6] API 활성화..."
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com storage.googleapis.com \
  --project "$PROJECT_ID"

# ---- §3 GCS 버킷(SQLite 영속화) ----
echo "▶ [2/6] GCS 버킷..."
if have "gcloud storage buckets describe gs://${BUCKET}"; then
  echo "  이미 존재: gs://${BUCKET}"
else
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
fi

# ---- §4 JWT 서명키 시크릿 ----
echo "▶ [3/6] JWT 시크릿(duty-secret)..."
if have "gcloud secrets describe duty-secret --project $PROJECT_ID"; then
  echo "  이미 존재: duty-secret"
else
  python3 -c "import secrets;print(secrets.token_urlsafe(48))" \
    | gcloud secrets create duty-secret --project "$PROJECT_ID" --data-file=-
fi

# ---- §5 배포 서비스 계정 + 권한 ----
echo "▶ [4/6] 배포 서비스 계정(duty-deployer) + 권한..."
if have "gcloud iam service-accounts describe $DEPLOYER --project $PROJECT_ID"; then
  echo "  이미 존재: ${DEPLOYER}"
else
  gcloud iam service-accounts create duty-deployer \
    --project "$PROJECT_ID" --display-name="Duty CI deployer"
fi

for ROLE in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.admin roles/storage.admin \
            roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER}" --role="$ROLE" --condition=None >/dev/null
done

# 런타임 SA: 시크릿 접근 + 버킷 읽기/쓰기
gcloud secrets add-iam-policy-binding duty-secret --project "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUNTIME}" --role=roles/storage.objectAdmin >/dev/null

# ---- §6 Workload Identity Federation ----
echo "▶ [5/6] Workload Identity Federation(키리스 인증)..."
if ! have "gcloud iam workload-identity-pools describe github-pool --project $PROJECT_ID --location=global"; then
  gcloud iam workload-identity-pools create github-pool \
    --project "$PROJECT_ID" --location=global --display-name="GitHub pool"
fi
POOL_ID="$(gcloud iam workload-identity-pools describe github-pool \
  --project "$PROJECT_ID" --location=global --format='value(name)')"

if ! have "gcloud iam workload-identity-pools providers describe github-provider --project $PROJECT_ID --location=global --workload-identity-pool=github-pool"; then
  gcloud iam workload-identity-pools providers create-oidc github-provider \
    --project "$PROJECT_ID" --location=global --workload-identity-pool=github-pool \
    --display-name="GitHub provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='${REPO}'" \
    --issuer-uri="https://token.actions.githubusercontent.com"
fi

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" --project "$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${REPO}" >/dev/null

WIF_PROVIDER="$(gcloud iam workload-identity-pools providers describe github-provider \
  --project "$PROJECT_ID" --location=global --workload-identity-pool=github-pool \
  --format='value(name)')"

# ---- 출력 ----
echo
echo "▶ [6/6] 완료! 아래 값을 GitHub 저장소에 등록하세요."
echo "  (Settings → Secrets and variables → Actions)"
echo
echo "── Repository secrets ──────────────────────────────"
echo "GCP_WIF_PROVIDER = ${WIF_PROVIDER}"
echo "GCP_SA_EMAIL     = ${DEPLOYER}"
echo
echo "── Repository variables ────────────────────────────"
echo "GCP_PROJECT_ID = ${PROJECT_ID}"
echo "GCP_REGION     = ${REGION}"
echo "SERVICE_NAME   = ${SERVICE}"
echo "DATA_BUCKET    = ${BUCKET}"
echo "────────────────────────────────────────────────────"
echo
echo "그다음: PR을 main에 병합하면 자동 배포되거나,"
echo "        GitHub Actions → 'Deploy to Cloud Run' → Run workflow 로 수동 실행하세요."
