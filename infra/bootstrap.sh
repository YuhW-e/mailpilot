#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

dry_run=false
case "${1:-}" in
    --dry-run) dry_run=true ;;
    '') ;;
    *) printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac
configure_cloud

run services enable run.googleapis.com cloudscheduler.googleapis.com cloudtasks.googleapis.com \
    firestore.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
    iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

email_provider=${MAILPILOT_EMAIL_PROVIDER:-gmail}
app_secrets=(telegram-bot model-key)
if [[ "$email_provider" == gmail ]]; then
    run services enable gmail.googleapis.com
    app_secrets+=(gmail-oauth)
fi

for role in ingress worker job scheduler tasks deploy; do
    ensure iam service-accounts describe "$(account "$role")" ||
        run iam service-accounts create "$prefix-$role"
done

if [[ "$dry_run" == true ]]; then
    project_number=123456789012
else
    project_number=$(gcloud_project projects describe "$project" --format='value(projectNumber)')
fi
tasks_agent="service-$project_number@gcp-sa-cloudtasks.iam.gserviceaccount.com"
scheduler_agent="service-$project_number@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
# Google provisions these identities and service-agent roles on API activation.
project_binding "serviceAccount:$tasks_agent" roles/cloudtasks.serviceAgent
project_binding "serviceAccount:$scheduler_agent" roles/cloudscheduler.serviceAgent
account_user "$(account tasks)" "$tasks_agent"

ensure artifacts repositories describe "$repo" --location="$region" ||
    run artifacts repositories create "$repo" --location="$region" --repository-format=docker
run artifacts repositories add-iam-policy-binding "$repo" --location="$region" \
    --member="serviceAccount:$(account deploy)" --role=roles/artifactregistry.writer

database_location=${MAILPILOT_FIRESTORE_LOCATION:-$region}
if ensure firestore databases describe --database='(default)'; then
    actual=$(gcloud_project firestore databases describe --database='(default)' \
        --format='value(locationId,type)')
    if [[ "$actual" != "$database_location"$'\t'FIRESTORE_NATIVE ]]; then
        printf 'Existing Firestore location or mode differs from the requested configuration.\n' >&2
        exit 1
    fi
else
    run firestore databases create --database='(default)' --location="$database_location" \
        --type=firestore-native
fi
if [[ "$dry_run" == true ]]; then
    python3 ./firestore.py "$project" --dry-run
else
    python3 ./firestore.py "$project"
fi

for role in ingress worker job; do
    run projects add-iam-policy-binding "$project" --member="serviceAccount:$(account "$role")" \
        --role=roles/datastore.user \
        --condition="expression=resource.name==\"projects/$project/databases/(default)\",title=MailPilotDefaultDatabase"
    account_user "$(account "$role")" "$(account deploy)"
done

queue_mode=create
if ensure tasks queues describe "$queue" --location="$region"; then queue_mode=update; fi
run tasks queues "$queue_mode" "$queue" --location="$region" --max-concurrent-dispatches=4 \
    --max-dispatches-per-second=2 --max-attempts=12 --min-backoff=5s --max-backoff=300s \
    --max-retry-duration=86400s
for role in ingress worker; do
    run tasks queues add-iam-policy-binding "$queue" --location="$region" \
        --member="serviceAccount:$(account "$role")" --role=roles/cloudtasks.enqueuer
    account_user "$(account tasks)" "$(account "$role")"
done

if [[ "${MAILPILOT_URGENT_ENABLED:-false}" == true ]]; then
    sync_queue="$prefix-sync"
    sync_mode=create
    if ensure tasks queues describe "$sync_queue" --location="$region"; then sync_mode=update; fi
    run tasks queues "$sync_mode" "$sync_queue" --location="$region" --max-concurrent-dispatches=4 \
        --max-dispatches-per-second=2 --max-attempts=12 --min-backoff=5s --max-backoff=300s \
        --max-retry-duration=86400s
    for role in ingress worker; do
        run tasks queues add-iam-policy-binding "$sync_queue" --location="$region" \
            --member="serviceAccount:$(account "$role")" --role=roles/cloudtasks.enqueuer
    done
    if [[ "$email_provider" == outlook ]]; then
        ensure secrets describe "$prefix-graph-client-state" ||
            run secrets create "$prefix-graph-client-state" --replication-policy=automatic
        run secrets add-iam-policy-binding "$prefix-graph-client-state" \
            --member="serviceAccount:$(account worker)" --role=roles/secretmanager.secretAccessor
    fi
fi

for secret in telegram-webhook "${app_secrets[@]}"; do
    ensure secrets describe "$prefix-$secret" ||
        run secrets create "$prefix-$secret" --replication-policy=automatic
done
run secrets add-iam-policy-binding "$prefix-telegram-webhook" \
    --member="serviceAccount:$(account ingress)" --role=roles/secretmanager.secretAccessor
for secret in "${app_secrets[@]}"; do
    for role in worker job; do
        run secrets add-iam-policy-binding "$prefix-$secret" \
            --member="serviceAccount:$(account "$role")" --role=roles/secretmanager.secretAccessor
    done
done

custom_role() {
    local name=$1 permissions=$2 mode=create
    if ensure iam roles describe "$name" --project="$project"; then mode=update; fi
    run iam roles "$mode" "$name" --title="$name" --permissions="$permissions" --stage=GA
}
custom_role MailPilotCredentialRefresh \
    secretmanager.versions.access,secretmanager.versions.add,secretmanager.versions.list
custom_role MailPilotJobLaunch run.jobs.run,run.jobs.runWithOverrides

# Account secrets are created by the authorization CLI. Add bindings only for
# the explicitly listed account secret IDs.
account_secrets=${MAILPILOT_ACCOUNT_SECRETS:-}
if [[ "$dry_run" == true && -z "$account_secrets" ]]; then
    account_secrets="mailpilot-$email_provider-example"
fi
if [[ -n "$account_secrets" ]]; then
    read -r -a selected_secrets <<< "$account_secrets"
    for secret in "${selected_secrets[@]}"; do
        for role in worker job; do
            run secrets add-iam-policy-binding "$secret" \
                --member="serviceAccount:$(account "$role")" \
                --role="projects/$project/roles/MailPilotCredentialRefresh"
        done
    done
fi
