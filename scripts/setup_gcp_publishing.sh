#!/usr/bin/env bash
#
# One-off GCP setup for publishing filament-api releases to Artifact Registry
# from GitHub Actions (.github/workflows/create-release.yml).
#
# Run once by someone with owner rights on the GCP project. Safe to re-run:
# every resource is created only if it does not already exist, and IAM
# bindings are additive.
#
# Creates:
#   - Python Artifact Registry repositories:
#       $INTERNAL_REPO  standard repo: dev builds + copies of stable releases
#       $REMOTE_REPO    remote repo mirroring (and caching) public PyPI
#       $VIRTUAL_REPO   virtual repo combining both, $INTERNAL_REPO first
#   - Workload Identity Federation pool + GitHub OIDC provider, restricted to
#     the default-branch copy of create-release.yml in $GITHUB_REPO
#   - Publisher service account with artifactregistry.writer on $INTERNAL_REPO
#
# Configuration (environment variables):
#   GCP_PROJECT_ID   (required) GCP project ID
#   GCP_REGION       (required) Artifact Registry location, e.g. europe-west2
#   INTERNAL_REPO    default: python-internal
#   REMOTE_REPO      default: pypi-remote
#   VIRTUAL_REPO     default: python
#   POOL             default: github
#   PROVIDER         default: filament-api-python
#   SA_NAME          default: filament-api-publisher
#   GITHUB_REPO      default: FilamentSoftware/filament-api-python
#   DEFAULT_BRANCH   default: develop (the branch workflow_run runs from)
#   GITHUB_REPO_ID   numeric repository ID; default: looked up with `gh`
#   GITHUB_OWNER_ID  numeric owner (organisation) ID; default: looked up with `gh`
#
# Options:
#   --set-github-vars  set the GitHub repository variables with `gh` instead
#                      of only printing the commands
#
# See docs/publishing.md for the full runbook.

set -euo pipefail

set_github_vars=false
for arg in "$@"; do
  case "$arg" in
    --set-github-vars) set_github_vars=true ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to the GCP project ID}"
: "${GCP_REGION:?Set GCP_REGION to the Artifact Registry location (e.g. europe-west2)}"
INTERNAL_REPO="${INTERNAL_REPO:-python-internal}"
REMOTE_REPO="${REMOTE_REPO:-pypi-remote}"
VIRTUAL_REPO="${VIRTUAL_REPO:-python}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-filament-api-python}"
SA_NAME="${SA_NAME:-filament-api-publisher}"
GITHUB_REPO="${GITHUB_REPO:-FilamentSoftware/filament-api-python}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-develop}"

# The WIF condition and binding match on the immutable numeric IDs, not the
# repository name, so a rename or transfer can't hand access to a new repo
# that takes over the old name.
if [[ -z "${GITHUB_REPO_ID:-}" || -z "${GITHUB_OWNER_ID:-}" ]]; then
  if ! command -v gh > /dev/null 2>&1; then
    echo "Set GITHUB_REPO_ID and GITHUB_OWNER_ID, or install the GitHub CLI (gh) to look them up" >&2
    exit 1
  fi
  read -r looked_up_repo_id looked_up_owner_id < <(
    gh api "repos/${GITHUB_REPO}" --jq '"\(.id) \(.owner.id)"'
  )
  GITHUB_REPO_ID="${GITHUB_REPO_ID:-$looked_up_repo_id}"
  GITHUB_OWNER_ID="${GITHUB_OWNER_ID:-$looked_up_owner_id}"
fi
if [[ ! "$GITHUB_REPO_ID" =~ ^[0-9]+$ || ! "$GITHUB_OWNER_ID" =~ ^[0-9]+$ ]]; then
  echo "GITHUB_REPO_ID and GITHUB_OWNER_ID must be numeric (got '$GITHUB_REPO_ID', '$GITHUB_OWNER_ID')" >&2
  exit 1
fi

if ! command -v gcloud > /dev/null 2>&1; then
  echo "gcloud is not installed or not on PATH" >&2
  exit 1
fi

gc() {
  gcloud --project="$GCP_PROJECT_ID" --quiet "$@"
}

log() {
  echo "==> $*"
}

project_number="$(gc projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)')"
sa_email="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
ar_service_agent="service-${project_number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
pool_resource="projects/${project_number}/locations/global/workloadIdentityPools/${POOL}"
provider_resource="${pool_resource}/providers/${PROVIDER}"

# 1. APIs
log "Enabling required APIs"
gc services enable \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com

# On a fresh project the Artifact Registry service agent may not exist until
# first use, and step 5 grants it access. Create it explicitly (idempotent).
log "Ensuring the Artifact Registry service agent exists"
gc beta services identity create --service=artifactregistry.googleapis.com > /dev/null

repo_exists() {
  gc artifacts repositories describe "$1" --location="$GCP_REGION" > /dev/null 2>&1
}

# 2. Standard repository
if repo_exists "$INTERNAL_REPO"; then
  log "Repository $INTERNAL_REPO already exists"
else
  log "Creating standard repository $INTERNAL_REPO"
  gc artifacts repositories create "$INTERNAL_REPO" \
    --repository-format=python \
    --location="$GCP_REGION" \
    --description="filament-api dev builds and copies of stable releases"
fi

# 3. PyPI remote repository
if repo_exists "$REMOTE_REPO"; then
  log "Repository $REMOTE_REPO already exists"
else
  log "Creating PyPI remote repository $REMOTE_REPO"
  gc artifacts repositories create "$REMOTE_REPO" \
    --repository-format=python \
    --location="$GCP_REGION" \
    --mode=remote-repository \
    --remote-python-repo=PYPI \
    --description="Cached mirror of public PyPI"
fi

# 4. Virtual repository (internal repo takes priority over the PyPI mirror)
if repo_exists "$VIRTUAL_REPO"; then
  log "Repository $VIRTUAL_REPO already exists"
else
  log "Creating virtual repository $VIRTUAL_REPO"
  policy_file="$(mktemp)"
  trap 'rm -f "$policy_file"' EXIT
  cat > "$policy_file" << EOF
[
  {
    "id": "${INTERNAL_REPO}",
    "repository": "projects/${GCP_PROJECT_ID}/locations/${GCP_REGION}/repositories/${INTERNAL_REPO}",
    "priority": 100
  },
  {
    "id": "${REMOTE_REPO}",
    "repository": "projects/${GCP_PROJECT_ID}/locations/${GCP_REGION}/repositories/${REMOTE_REPO}",
    "priority": 10
  }
]
EOF
  gc artifacts repositories create "$VIRTUAL_REPO" \
    --repository-format=python \
    --location="$GCP_REGION" \
    --mode=virtual-repository \
    --upstream-policy-file="$policy_file" \
    --description="filament-api internal index (internal releases + PyPI mirror)"
fi

# 5. The virtual repository reads its upstreams as the Artifact Registry service agent
for upstream in "$INTERNAL_REPO" "$REMOTE_REPO"; do
  log "Granting Artifact Registry service agent reader on $upstream"
  gc artifacts repositories add-iam-policy-binding "$upstream" \
    --location="$GCP_REGION" \
    --member="serviceAccount:${ar_service_agent}" \
    --role=roles/artifactregistry.reader > /dev/null
done

# 6. Workload Identity Federation pool and GitHub OIDC provider
if gc iam workload-identity-pools describe "$POOL" --location=global > /dev/null 2>&1; then
  log "Workload Identity pool $POOL already exists"
else
  log "Creating Workload Identity pool $POOL"
  gc iam workload-identity-pools create "$POOL" \
    --location=global \
    --display-name="GitHub Actions"
fi

# create-release.yml is triggered by workflow_run, which always runs the
# default branch's copy of the workflow. So workflow_ref is fixed for both dev
# and stable releases, and can be pinned exactly.
workflow_ref="${GITHUB_REPO}/.github/workflows/create-release.yml@refs/heads/${DEFAULT_BRANCH}"
attribute_condition="assertion.repository_id == '${GITHUB_REPO_ID}' && assertion.repository_owner_id == '${GITHUB_OWNER_ID}' && assertion.workflow_ref == '${workflow_ref}' && assertion.event_name == 'workflow_run'"
attribute_mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.workflow_ref=assertion.workflow_ref"

if gc iam workload-identity-pools providers describe "$PROVIDER" \
    --location=global --workload-identity-pool="$POOL" > /dev/null 2>&1; then
  log "Updating OIDC provider $PROVIDER"
  gc iam workload-identity-pools providers update-oidc "$PROVIDER" \
    --location=global \
    --workload-identity-pool="$POOL" \
    --attribute-mapping="$attribute_mapping" \
    --attribute-condition="$attribute_condition"
else
  log "Creating OIDC provider $PROVIDER"
  gc iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --location=global \
    --workload-identity-pool="$POOL" \
    --display-name="filament-api-python releases" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$attribute_mapping" \
    --attribute-condition="$attribute_condition"
fi

# 7. Publisher service account, writer on the internal repository only
if gc iam service-accounts describe "$sa_email" > /dev/null 2>&1; then
  log "Service account $sa_email already exists"
else
  log "Creating service account $sa_email"
  gc iam service-accounts create "$SA_NAME" \
    --display-name="filament-api release publisher"
fi

log "Granting $sa_email writer on $INTERNAL_REPO"
gc artifacts repositories add-iam-policy-binding "$INTERNAL_REPO" \
  --location="$GCP_REGION" \
  --member="serviceAccount:${sa_email}" \
  --role=roles/artifactregistry.writer > /dev/null

# 8. Allow the GitHub repository's tokens (as filtered by the provider's
#    attribute condition) to impersonate the publisher service account
log "Allowing $GITHUB_REPO (repository ID $GITHUB_REPO_ID) to impersonate $sa_email"
gc iam service-accounts add-iam-policy-binding "$sa_email" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${pool_resource}/attribute.repository_id/${GITHUB_REPO_ID}" > /dev/null

# 9. GitHub repository variables
declare -A github_vars=(
  [GCP_PROJECT_ID]="$GCP_PROJECT_ID"
  [GCP_REGION]="$GCP_REGION"
  [GCP_ARTIFACT_REPOSITORY]="$INTERNAL_REPO"
  [GCP_WIF_PROVIDER]="$provider_resource"
  [GCP_SERVICE_ACCOUNT]="$sa_email"
)
var_names=(GCP_PROJECT_ID GCP_REGION GCP_ARTIFACT_REPOSITORY GCP_WIF_PROVIDER GCP_SERVICE_ACCOUNT)

if [[ "$set_github_vars" == true ]]; then
  if ! command -v gh > /dev/null 2>&1; then
    echo "--set-github-vars needs the GitHub CLI (gh) on PATH" >&2
    exit 1
  fi
  for name in "${var_names[@]}"; do
    log "Setting GitHub variable $name"
    gh variable set "$name" --repo "$GITHUB_REPO" --body "${github_vars[$name]}"
  done
else
  echo
  echo "Set these GitHub repository variables (or re-run with --set-github-vars):"
  echo
  for name in "${var_names[@]}"; do
    printf "  gh variable set %s --repo %q --body %q\n" "$name" "$GITHUB_REPO" "${github_vars[$name]}"
  done
fi

echo
echo "Install index for consumers:"
echo "  https://${GCP_REGION}-python.pkg.dev/${GCP_PROJECT_ID}/${VIRTUAL_REPO}/simple/"
