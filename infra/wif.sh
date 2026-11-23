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
    MAILPILOT_GITHUB_REPOSITORY=${MAILPILOT_GITHUB_REPOSITORY:-example/mailpilot}
    MAILPILOT_GITHUB_REPOSITORY_ID=${MAILPILOT_GITHUB_REPOSITORY_ID:-123456789}
    MAILPILOT_GITHUB_OWNER_ID=${MAILPILOT_GITHUB_OWNER_ID:-987654321}
fi

github_repo=${MAILPILOT_GITHUB_REPOSITORY:?Set OWNER/REPO}
repo_id=${MAILPILOT_GITHUB_REPOSITORY_ID:?Set numeric GitHub repository ID}
owner_id=${MAILPILOT_GITHUB_OWNER_ID:?Set numeric GitHub owner ID}
if [[ ! "$github_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ||
      ! "$repo_id" =~ ^[0-9]+$ || ! "$owner_id" =~ ^[0-9]+$ ]]; then
    printf 'Invalid GitHub repository or numeric IDs.\n' >&2
    exit 2
fi
if [[ "$dry_run" == true ]]; then
    project_number=123456789012
else
    project_number=$(gcloud_project projects describe "$project" --format='value(projectNumber)')
fi

pool=mailpilot-github
provider=github
ensure iam workload-identity-pools describe "$pool" --location=global ||
    run iam workload-identity-pools create "$pool" --location=global --display-name=MailPilot
mode=create-oidc
if ensure iam workload-identity-pools providers describe "$provider" --location=global \
    --workload-identity-pool="$pool"; then
    mode=update-oidc
fi
condition="assertion.repository_id=='$repo_id' && assertion.repository_owner_id=='$owner_id'"
condition+=" && assertion.environment=='mailpilot-deploy' && assertion.ref=='refs/heads/main'"
condition+=" && assertion.workflow_ref=='$github_repo/.github/workflows/deploy.yml@refs/heads/main'"
run iam workload-identity-pools providers "$mode" "$provider" --location=global \
    --workload-identity-pool="$pool" --issuer-uri=https://token.actions.githubusercontent.com \
    --attribute-mapping='google.subject=assertion.sub,attribute.repository_id=assertion.repository_id' \
    --attribute-condition="$condition"

pool_path="projects/$project_number/locations/global/workloadIdentityPools/$pool"
run iam service-accounts add-iam-policy-binding "$(account deploy)" --condition=None \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/$pool_path/attribute.repository_id/$repo_id"
printf 'WIF provider: %s/providers/%s\nDeploy identity: %s\n' "$pool_path" "$provider" "$(account deploy)"
