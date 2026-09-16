#!/usr/bin/env bash
# Deploy del harvester a GCP.
#
# Uso:
#   ./deploy.sh              # build imagen + deploy job + crear/actualizar scheduler
#   ./deploy.sh --build      # solo build
#   ./deploy.sh --job        # solo job (asume imagen ya construida)
#   ./deploy.sh --scheduler  # solo scheduler
#
# Requiere gcloud autenticado contra proyecto ontimeai.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ontimeai}"
REGION="${REGION:-us-central1}"
JOB_NAME="ontimeai-harvester"
SCHEDULER_NAME="ontimeai-harvester-scheduler"
IMAGE="gcr.io/${PROJECT_ID}/ontimeai-harvester:latest"
SERVICE_ACCOUNT="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

do_build=false
do_job=false
do_scheduler=false

if [[ $# -eq 0 ]]; then
    do_build=true; do_job=true; do_scheduler=true
else
    for arg in "$@"; do
        case "$arg" in
            --build) do_build=true ;;
            --job) do_job=true ;;
            --scheduler) do_scheduler=true ;;
            *) echo "unknown flag: $arg" >&2; exit 2 ;;
        esac
    done
fi

if $do_build; then
    echo ">> Build harvester image"
    gcloud builds submit \
        --project="$PROJECT_ID" \
        --config=cloudbuild-harvester.yaml \
        .
fi

# El camino real de despliegue es .github/workflows/deploy.yml, que corre en
# cada push a main. Este script es para desplegar a mano, y por eso actualiza
# con `--update-env-vars` en vez de `--set-env-vars`: correrlo desactualizado no
# puede apagar una variable que alguien haya agregado despues. La lista de abajo
# igual tiene que espejar la del workflow.
if $do_job; then
    echo ">> Deploy Cloud Run Job: $JOB_NAME"
    if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
        gcloud run jobs update "$JOB_NAME" \
            --project="$PROJECT_ID" --region="$REGION" \
            --image="$IMAGE" \
            --service-account="$SERVICE_ACCOUNT" \
            --update-env-vars=GCS_BUCKET=ontimeai-prod-live-db,AIRPORT_CODE=KATL,LOG_LEVEL=INFO,FR24_MAX_PAGES=30,CAPTURE_FUTURE_LEGS=true,FUTURE_LEG_HORIZON_HOURS=6,LINEAGE_HYDRATION_BUDGET=70 \
            --memory=1Gi --cpu=1 --task-timeout=900s --max-retries=1
    else
        gcloud run jobs create "$JOB_NAME" \
            --project="$PROJECT_ID" --region="$REGION" \
            --image="$IMAGE" \
            --service-account="$SERVICE_ACCOUNT" \
            --set-env-vars=GCS_BUCKET=ontimeai-prod-live-db,AIRPORT_CODE=KATL,LOG_LEVEL=INFO,FR24_MAX_PAGES=30,CAPTURE_FUTURE_LEGS=true,FUTURE_LEG_HORIZON_HOURS=6,LINEAGE_HYDRATION_BUDGET=70 \
            --memory=1Gi --cpu=1 --task-timeout=900s --max-retries=1
    fi
fi

if $do_scheduler; then
    echo ">> Create/update Scheduler: $SCHEDULER_NAME (cron 5,20,35,50 * * * *)"
    JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
    if gcloud scheduler jobs describe "$SCHEDULER_NAME" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
        gcloud scheduler jobs update http "$SCHEDULER_NAME" \
            --project="$PROJECT_ID" --location="$REGION" \
            --schedule="5,20,35,50 * * * *" \
            --time-zone=Etc/UTC \
            --uri="$JOB_URI" \
            --http-method=POST \
            --oauth-service-account-email="$SERVICE_ACCOUNT"
    else
        gcloud scheduler jobs create http "$SCHEDULER_NAME" \
            --project="$PROJECT_ID" --location="$REGION" \
            --schedule="5,20,35,50 * * * *" \
            --time-zone=Etc/UTC \
            --uri="$JOB_URI" \
            --http-method=POST \
            --oauth-service-account-email="$SERVICE_ACCOUNT"
    fi
fi

echo
echo "DONE."
echo "  Test the job manually:"
echo "    gcloud run jobs execute $JOB_NAME --region=$REGION --project=$PROJECT_ID"
echo "  Tail logs:"
echo "    gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=$JOB_NAME' --project=$PROJECT_ID --limit 50"
