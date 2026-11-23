#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

dry_run=false
release_only=false
case "${1:-}" in
    --dry-run) dry_run=true ;;
    --release-only) release_only=true ;;
    '') ;;
    *) printf 'Usage: %s [--dry-run|--release-only]\n' "$0" >&2; exit 2 ;;
esac
configure_cloud

image_base="$region-docker.pkg.dev/$project/$repo/mailpilot"
if [[ "$dry_run" == true ]]; then
    MAILPILOT_IMAGE=${MAILPILOT_IMAGE:-"$image_base@sha256:0000000000000000000000000000000000000000000000000000000000000000"}
fi
image=${MAILPILOT_IMAGE:?Set MAILPILOT_IMAGE to the Artifact Registry digest}
if [[ "${image%@*}" != "$image_base" || ! "${image##*@}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf 'Expected an immutable digest in the MailPilot Artifact Registry repository.\n' >&2
    exit 2
fi

if [[ "$release_only" == true ]]; then
    run run jobs update "$prefix-digest" --region="$region" --image="$image"
    for role in worker ingress; do
        run run services update "$prefix-$role" --region="$region" --image="$image"
        run run services update-traffic "$prefix-$role" --region="$region" --to-latest
    done
    printf '%s\n' "$image"
    exit
fi

if [[ "$dry_run" == true ]]; then
    export MAILPILOT_PROJECT_ID="$project"
    bash ./bootstrap.sh --dry-run
    bash ./wif.sh --dry-run
    MAILPILOT_ALLOWED_USER_IDS=${MAILPILOT_ALLOWED_USER_IDS:-'[1001]'}
    MAILPILOT_ALLOWED_CHAT_IDS=${MAILPILOT_ALLOWED_CHAT_IDS:-'[1001]'}
    MAILPILOT_TELEGRAM_BOT_ID=${MAILPILOT_TELEGRAM_BOT_ID:-1002}
    MAILPILOT_OUTLOOK_TENANT_ID=${MAILPILOT_OUTLOOK_TENANT_ID:-00000000-0000-0000-0000-000000000000}
    MAILPILOT_OUTLOOK_CLIENT_ID=${MAILPILOT_OUTLOOK_CLIENT_ID:-00000000-0000-0000-0000-000000000000}
    MAILPILOT_MODEL_PROVIDER=${MAILPILOT_MODEL_PROVIDER:-openai}
    MAILPILOT_MODEL_NAME=${MAILPILOT_MODEL_NAME:-fixture}
    MAILPILOT_GMAIL_OAUTH_VERSION=${MAILPILOT_GMAIL_OAUTH_VERSION:-1}
    MAILPILOT_WEBHOOK_VERSION=${MAILPILOT_WEBHOOK_VERSION:-1}
    MAILPILOT_BOT_VERSION=${MAILPILOT_BOT_VERSION:-1}
    MAILPILOT_MODEL_VERSION=${MAILPILOT_MODEL_VERSION:-1}
fi

work_budget=${MAILPILOT_WORK_BUDGET_SECONDS:-240}
hard_timeout=${MAILPILOT_HARD_TIMEOUT_SECONDS:-600}
if [[ ! "$work_budget" =~ ^[1-9][0-9]{0,2}$ || ! "$hard_timeout" =~ ^[1-9][0-9]{0,2}$ ]] ||
    ((work_budget + 30 > hard_timeout || hard_timeout > 900)); then
    printf 'Require a positive work budget, 30s for handoff, and hard timeout up to 900s.\n' >&2
    exit 2
fi
version_ref() {
    if [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
        printf 'Secret version for %s must be numeric.\n' "$1" >&2
        return 2
    fi
    printf 'projects/%s/secrets/%s-%s/versions/%s' "$project" "$prefix" "$1" "$2"
}
bot_version=$(version_ref telegram-bot "${MAILPILOT_BOT_VERSION:?Set MAILPILOT_BOT_VERSION}")
model_version=$(version_ref model-key "${MAILPILOT_MODEL_VERSION:?Set MAILPILOT_MODEL_VERSION}")
webhook_version=$(version_ref telegram-webhook "${MAILPILOT_WEBHOOK_VERSION:?Set MAILPILOT_WEBHOOK_VERSION}")

users=${MAILPILOT_ALLOWED_USER_IDS:?Set MAILPILOT_ALLOWED_USER_IDS as a JSON array}
chats=${MAILPILOT_ALLOWED_CHAT_IDS:?Set MAILPILOT_ALLOWED_CHAT_IDS as a JSON array}
python3 - "$users" "$chats" <<'PY'
import json
import sys

try:
    users, chats = map(json.loads, sys.argv[1:])
    for ids in (users, chats):
        if (
            not isinstance(ids, list)
            or not all(type(i) is int and i > 0 for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError
    if not set(users).intersection(chats):
        raise ValueError
except ValueError:
    raise SystemExit("Allowlist format invalid or no Telegram private recipient is shared.")
PY

shared="^|^MAILPILOT_PROJECT_ID=$project|MAILPILOT_WORK_BUDGET_SECONDS=$work_budget"
shared+="|MAILPILOT_HARD_TIMEOUT_SECONDS=$hard_timeout|MAILPILOT_ALLOWED_USER_IDS=$users"
shared+="|MAILPILOT_ALLOWED_CHAT_IDS=$chats"
digest_env="$shared|MAILPILOT_TELEGRAM_BOT_TOKEN_VERSION=$bot_version"
digest_env+="|MAILPILOT_MODEL_API_KEY_VERSION=$model_version"
digest_env+="|MAILPILOT_MODEL_PROVIDER=${MAILPILOT_MODEL_PROVIDER:?Set MAILPILOT_MODEL_PROVIDER}"
digest_env+="|MAILPILOT_MODEL_NAME=${MAILPILOT_MODEL_NAME:?Set MAILPILOT_MODEL_NAME}"
case "${MAILPILOT_EMAIL_PROVIDER:-gmail}" in
    gmail)
        email_provider=gmail
        oauth_version=$(version_ref gmail-oauth "${MAILPILOT_GMAIL_OAUTH_VERSION:?Set MAILPILOT_GMAIL_OAUTH_VERSION}")
        digest_env+="|MAILPILOT_GMAIL_OAUTH_CLIENT_VERSION=$oauth_version"
        ;;
    outlook)
        email_provider=outlook
        digest_env+="|MAILPILOT_OUTLOOK_TENANT_ID=${MAILPILOT_OUTLOOK_TENANT_ID:?Set MAILPILOT_OUTLOOK_TENANT_ID}"
        digest_env+="|MAILPILOT_OUTLOOK_CLIENT_ID=${MAILPILOT_OUTLOOK_CLIENT_ID:?Set MAILPILOT_OUTLOOK_CLIENT_ID}"
        ;;
    *)
        printf 'MAILPILOT_EMAIL_PROVIDER must be gmail or outlook.\n' >&2
        exit 2
        ;;
esac
digest_env+="|MAILPILOT_EMAIL_PROVIDER=$email_provider"

run run jobs deploy "$prefix-digest" --region="$region" --image="$image" \
    --service-account="$(account job)" --command=mailpilot-job --tasks=1 --max-retries=0 \
    --task-timeout="${hard_timeout}s" --set-env-vars="$digest_env"
run run jobs add-iam-policy-binding "$prefix-digest" --region="$region" \
    --member="serviceAccount:$(account worker)" --role="projects/$project/roles/MailPilotJobLaunch"

deploy_worker() {
    run run deploy "$prefix-worker" --region="$region" --image="$image" \
        --service-account="$(account worker)" --command=mailpilot-worker --min=0 --min-instances=0 \
        --ingress=all --default-url --invoker-iam-check --no-allow-unauthenticated --timeout="${hard_timeout}s" --set-env-vars="$1"
}

if [[ "$dry_run" == true ]]; then
    deploy_worker "$digest_env"
    worker_url="https://mailpilot-worker-example.$region.run.app"
else
    if ! ensure run services describe "$prefix-worker" --region="$region"; then
        deploy_worker "$digest_env"
    fi
    worker_url=$(gcloud_project run services describe "$prefix-worker" --region="$region" \
        --format='value(status.url)')
    [[ "$worker_url" == https://* ]] || { printf 'No Worker service URL.\n' >&2; exit 1; }
fi

task_env="MAILPILOT_TASK_QUEUE_PATH=$queue_path|MAILPILOT_TASK_WORKER_URL=$worker_url"
task_env+="|MAILPILOT_TASK_INVOKER_EMAIL=$(account tasks)"
deploy_worker "$digest_env|$task_env|MAILPILOT_SCHEDULER_INVOKER_EMAIL=$(account scheduler)|MAILPILOT_DIGEST_JOB=$job_path"
for role in scheduler tasks; do
    run run services add-iam-policy-binding "$prefix-worker" --region="$region" \
        --member="serviceAccount:$(account "$role")" --role=roles/run.invoker
done

ingress_env="$shared|$task_env|MAILPILOT_TELEGRAM_WEBHOOK_SECRET_VERSION=$webhook_version"
ingress_env+="|MAILPILOT_EMAIL_PROVIDER=$email_provider"
ingress_env+="|MAILPILOT_TELEGRAM_BOT_ID=${MAILPILOT_TELEGRAM_BOT_ID:?Set MAILPILOT_TELEGRAM_BOT_ID}"
run run deploy "$prefix-ingress" --region="$region" --image="$image" \
    --service-account="$(account ingress)" --command=mailpilot-ingress \
    --min=0 --min-instances=0 --ingress=all --default-url --no-invoker-iam-check --set-env-vars="$ingress_env"

for service in worker ingress; do
    run run services update-traffic "$prefix-$service" --region="$region" --to-latest
    run run services add-iam-policy-binding "$prefix-$service" --region="$region" \
        --member="serviceAccount:$(account deploy)" --role=roles/run.developer
done
run run jobs add-iam-policy-binding "$prefix-digest" --region="$region" \
    --member="serviceAccount:$(account deploy)" --role=roles/run.developer

schedule() {
    local name="$prefix-$1" cron=$2 zone=$3 route=$4 mode=create
    if ensure scheduler jobs describe "$name" --location="$scheduler_region"; then mode=update; fi
    run scheduler jobs "$mode" http "$name" --location="$scheduler_region" --schedule="$cron" \
        --time-zone="$zone" --uri="$worker_url$route" --http-method=POST --attempt-deadline=60s \
        --oidc-service-account-email="$(account scheduler)" --oidc-token-audience="$worker_url" \
        --max-retry-attempts=5 --min-backoff=15s --max-backoff=300s --max-retry-duration=1800s
}
schedule morning '0 6 * * *' Europe/Zurich /internal/schedule/morning
schedule evening '0 21 * * *' Europe/Zurich /internal/schedule/evening
schedule recover '*/15 * * * *' Etc/UTC /internal/telegram/recover
