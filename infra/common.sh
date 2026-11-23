#!/usr/bin/env bash

configure_cloud() {
    if [[ "$dry_run" == true ]]; then
        MAILPILOT_PROJECT_ID=${MAILPILOT_PROJECT_ID:-demo-mailpilot}
    fi
    project=${MAILPILOT_PROJECT_ID:?Set MAILPILOT_PROJECT_ID}
    region=${MAILPILOT_REGION:-europe-west6}
    scheduler_region=${MAILPILOT_SCHEDULER_REGION:-europe-west1}
    repo=mailpilot
    queue=mailpilot-interaction
    prefix=mailpilot
    queue_path="projects/$project/locations/$region/queues/$queue"
    job_path="projects/$project/locations/$region/jobs/$prefix-digest"
}

gcloud_project() {
    gcloud --quiet --project="$project" "$@"
}

run() {
    if [[ "$dry_run" == true ]]; then
        printf '  gcloud --project=%q' "$project"
        printf ' %q' "$@"
        printf '\n'
    else
        gcloud_project "$@" >/dev/null
    fi
}

ensure() {
    if [[ "$dry_run" == true ]] || ! gcloud_project "$@" >/dev/null 2>&1; then
        return 1
    fi
}

account() {
    printf '%s-%s@%s.iam.gserviceaccount.com' "$prefix" "$1" "$project"
}

project_binding() {
    run projects add-iam-policy-binding "$project" --condition=None --member="$1" --role="$2"
}

account_user() {
    run iam service-accounts add-iam-policy-binding "$1" --condition=None \
        --member="serviceAccount:$2" --role=roles/iam.serviceAccountUser
}
