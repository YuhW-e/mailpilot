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

if [[ "$dry_run" == true ]]; then
    MAILPILOT_INGRESS_URL=${MAILPILOT_INGRESS_URL:-https://mailpilot-ingress-example.run.app}
    project_number=123456789012
else
    project_number=$(gcloud_project projects describe "$project" --format='value(projectNumber)')
fi
origin=${MAILPILOT_INGRESS_URL:?Set MAILPILOT_INGRESS_URL to the public Ingress origin}
origin=${origin%/}
if [[ ! "$origin" =~ ^https://[a-zA-Z0-9.-]+$ ]]; then
    printf 'Expected an HTTPS Ingress origin.\n' >&2
    exit 2
fi

topic="$prefix-gmail"
subscription="$prefix-gmail-push"
push_account=$(account pubsub)
agent="service-$project_number@gcp-sa-pubsub.iam.gserviceaccount.com"
endpoint="$origin/email/gmail"
run services enable pubsub.googleapis.com
run beta services identity create --service=pubsub.googleapis.com
ensure iam service-accounts describe "$push_account" ||
    run iam service-accounts create "$prefix-pubsub"
run iam service-accounts add-iam-policy-binding "$push_account" --condition=None \
    --member="serviceAccount:$agent" --role=roles/iam.serviceAccountOpenIdTokenCreator

ensure pubsub topics describe "$topic" || run pubsub topics create "$topic"
run pubsub topics add-iam-policy-binding "$topic" \
    --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher

mode=create
topic_arg=(--topic="$topic")
if ensure pubsub subscriptions describe "$subscription"; then
    mode=update
    topic_arg=()
    actual=$(gcloud_project pubsub subscriptions describe "$subscription" --format='value(topic)')
    if [[ "$actual" != "projects/$project/topics/$topic" ]]; then
        printf 'Existing Pub/Sub subscription points to a different topic.\n' >&2
        exit 1
    fi
fi
run pubsub subscriptions "$mode" "$subscription" "${topic_arg[@]}" \
    --push-endpoint="$endpoint" --push-auth-service-account="$push_account" \
    --push-auth-token-audience="$endpoint" --expiration-period=never
