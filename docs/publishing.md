# Publishing releases

Maintainer runbook for how `filament-api` releases are published, and the
one-off cloud setup that publishing depends on. If you only want to install the
package, see [Installation](../README.md#installation) in the README.

## Release flow

| Release | Source branch | Tag | PEP 440 version | GitHub release | GCP Artifact Registry | PyPI |
| ------- | ------------- | --- | --------------- | -------------- | --------------------- | ---- |
| Dev | `develop` | `v0.8.1-dev.0.0.4` | `0.8.1.dev4` | pre-release | yes | no |
| Stable | `main` | `v0.8.0` | `0.8.0` | release | yes | yes |

1. A push to `develop` or `main` runs `increment-version-develop.yml` /
   `increment-version-main.yml`, which updates the changelog, tags the release
   and uploads a `version` artifact.
2. When that run succeeds, `.github/workflows/create-release.yml` runs:

   ```text
   resolve-version ─▶ build ─┬─▶ release        (GitHub release)
                             ├─▶ publish-gcp    (all releases)
                             └─▶ publish-pypi   (stable only)
   ```

   - `resolve-version` reads the tag and decides the channel once (`is_dev`,
     true when the tag contains `dev`).
   - `build` checks out the tag, sets the PEP 440 version and runs
     `poetry build`. The three downstream jobs all download the same `dist`
     artifact, so they ship identical files.
   - `publish-gcp` authenticates to Google Cloud with Workload Identity
     Federation (no stored key) and uploads `dist/*` to the internal
     repository with `twine` and `keyrings.google-artifactregistry-auth`.
   - `publish-pypi` runs only when `is_dev` is false **and** the triggering
     branch was `main`. It publishes with
     [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no API
     token) through `pypa/gh-action-pypi-publish`, which also uploads PEP 740
     attestations.

### Failures and re-runs

The three downstream jobs are independent. A GCP or PyPI outage does not stop
the GitHub release, and a failed PyPI publish does not depend on GCP (or the
other way round). The tag already exists either way, so the fix for a failed
publish is **Re-run failed jobs** on the `Create Release` run. Re-running a job
whose files were partly uploaded is safe. `publish-gcp` lists the files already
in the repository (`gcloud artifacts files list`) and uploads only the missing
ones. `publish-pypi` uses `skip-existing: true`.

`publish-gcp` fails with `Missing repository variables: ...` if any of the
GitHub variables below is unset. This is deliberate: a release that silently
skipped the internal index would be easy to miss.

### Why everything is pinned to `develop`

`create-release.yml` is triggered by `workflow_run`. GitHub always runs the
**default branch's** copy of a `workflow_run` workflow (`develop` here), with
`github.ref = refs/heads/develop`, even when the trigger was the `main`
increment run. The triggering branch is only available as
`github.event.workflow_run.head_branch`. Consequences:

- The OIDC token's `workflow_ref` is always
  `FilamentSoftware/filament-api-python/.github/workflows/create-release.yml@refs/heads/develop`,
  for dev and stable releases alike. The GCP provider's attribute condition
  pins exactly that value.
- The `pypi` environment's deployment-branch rule is checked against
  `github.ref`, so it must allow `develop`, **not** `main`. Allowing only
  `develop` still stops a workflow named `create-release.yml` on any other
  branch from using the environment.
- PyPI Trusted Publishing matches on the workflow filename and environment,
  not the ref. The publish step must stay in `create-release.yml` itself:
  Trusted Publishing does not support reusable workflows.

### Trusting the triggering run

`workflow_run` matches the triggering workflow by **name** only, and the
release workflow runs with this repository's permissions, OIDC included. A
fork pull request could add its own workflow named
`Increment Version (develop)` with a `pull_request` trigger, upload a crafted
`version` artifact, and so start a release run. That run would have a version
string the attacker controls (e.g. `refs/pull/<N>/head`, the PR's own code).
To prevent this, `resolve-version` only proceeds when:

- the triggering run was a `push` from this repository
  (`workflow_run.head_repository`), on `develop` or `main`;
- the version is exactly `vX.Y.Z` (stable) or `vX.Y.Z-dev.A.B.C` (dev), and its
  channel matches the branch: dev from `develop`, stable from `main`.

The `build` and `release` jobs then check out `refs/tags/<version>`, so a
version that isn't an existing tag in this repository fails the run.

## Pre-publish checklist

Do these before the publishing jobs first merge to `develop`:

- [ ] GCP setup done ([below](#gcp-artifact-registry)) and the five GitHub
      repository variables set.
- [ ] PyPI pending Trusted Publisher added ([below](#pypi)).
- [ ] GitHub `pypi` environment created with deployment branch `develop`
      ([below](#github)).
- [ ] The GitHub repository is made **public** before the first stable
      release, so the README links and project URLs shown on PyPI resolve.
- [ ] When the repository goes public, set **Settings → Actions → General →
      Approval for running fork pull request workflows from contributors** to
      *Require approval for all external contributors*.

## GCP Artifact Registry

Run the setup script once, as someone with owner rights on the GCP project
(it is idempotent, so re-running it is safe). It is a bash script: on Windows,
run it from [Cloud Shell](https://cloud.google.com/shell) (which already has
`gcloud` and your credentials), WSL or Git Bash.

```bash
GCP_PROJECT_ID=filament-artifacts GCP_REGION=europe-west2 bash scripts/setup_gcp_publishing.sh
# or, to also set the GitHub repository variables with the gh CLI:
GCP_PROJECT_ID=filament-artifacts GCP_REGION=europe-west2 bash scripts/setup_gcp_publishing.sh --set-github-vars
```

The script looks up the repository's numeric IDs with the `gh` CLI. Without
`gh`, pass them as `GITHUB_REPO_ID` and `GITHUB_OWNER_ID` (from
`https://api.github.com/repos/FilamentSoftware/filament-api-python`: `id` and
`owner.id`).

Resource names can be overridden with environment variables; see the header
of [`scripts/setup_gcp_publishing.sh`](../scripts/setup_gcp_publishing.sh).
With the defaults it creates:

| Resource | Default name | Purpose |
| -------- | ------------ | ------- |
| Standard Python repository | `python-internal` | Dev builds and copies of stable releases. CI uploads here. |
| Remote Python repository | `pypi-remote` | Cached mirror of public PyPI, for third-party dependencies. |
| Virtual Python repository | `python` | Combines the two, with `python-internal` at higher priority (100 vs 10). Consumers install from this only. |
| Workload Identity pool | `github` | Trusts GitHub Actions OIDC tokens. |
| OIDC provider | `filament-api-python` | Issuer `https://token.actions.githubusercontent.com`. Attribute condition: `repository_id` and `repository_owner_id` equal this repository's numeric IDs (immutable, so a rename or transfer can't pass access to a repo that reuses the name), `workflow_ref == .../create-release.yml@refs/heads/develop` and `event_name == workflow_run`. |
| Service account | `filament-api-publisher` | `roles/artifactregistry.writer` on `python-internal` only. The principal set for this repository's ID (`attribute.repository_id/<id>`) gets `roles/iam.workloadIdentityUser` on it. |

It also grants the Artifact Registry service agent
`roles/artifactregistry.reader` on both upstream repositories, which the
virtual repository needs to read them.

Because of the attribute condition, only the default-branch copy of
`create-release.yml`, triggered by `workflow_run`, can impersonate the
publisher service account. Other workflows, branches and events cannot.

### GitHub repository variables

The workflow reads its GCP configuration from repository **variables** (not
secrets: none of these values are sensitive). The script prints the
`gh variable set` commands, or runs them with `--set-github-vars`.

| Variable | Value |
| -------- | ------- |
| `GCP_PROJECT_ID` | `filament-artifacts` |
| `GCP_REGION` | `europe-west2` |
| `GCP_ARTIFACT_REPOSITORY` | `python-internal` |
| `GCP_WIF_PROVIDER` | `projects/<project-number>/locations/global/workloadIdentityPools/github/providers/filament-api-python` |
| `GCP_SERVICE_ACCOUNT` | `filament-api-publisher@filament-artifacts.iam.gserviceaccount.com` |

## PyPI

The `filament-api` project does not need to exist yet. On PyPI, go to
**Your account → Publishing → Add a new pending publisher** and enter:

| Field | Value |
| ----- | ----- |
| PyPI project name | `filament-api` |
| Owner | `FilamentSoftware` |
| Repository name | `filament-api-python` |
| Workflow name | `create-release.yml` |
| Environment name | `pypi` |

The first successful publish creates the project and turns the pending
publisher into a normal Trusted Publisher. Add the other maintainers as
project owners afterwards.

Publishing to PyPI makes the sdist and wheel (and so the package source)
publicly downloadable, and the attestations name this repository and workflow.
The repository is expected to be public by then (see the
[checklist](#pre-publish-checklist)), and the package is MIT licensed.

## GitHub

1. **Settings → Environments → New environment**, named `pypi`.
2. **Deployment branches and tags → Selected branches and tags**, add
   `develop` only (see [why](#why-everything-is-pinned-to-develop)).
3. Optional: add **Required reviewers** for a manual approval gate before each
   PyPI publish. It is not configured by default.
4. Set the five repository variables above (**Settings → Secrets and
   variables → Actions → Variables**), unless the script already did.

## Granting consumers access

Internal workloads and people install from the **virtual** repository
(`https://europe-west2-python.pkg.dev/filament-artifacts/python/simple/`) with
`keyrings.google-artifactregistry-auth`. Grant them reader access on that
repository:

```bash
gcloud artifacts repositories add-iam-policy-binding python \
  --project=filament-artifacts --location=europe-west2 \
  --member="serviceAccount:<workload-sa>@<project>.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

Use `user:` or `group:` members for people. Third parties use public PyPI for
stable releases; grant them the same role only if they need dev builds.

## First-run checklist

End-to-end behaviour can only be checked against the real setup:

- [ ] First dev release after merge: `publish-gcp` succeeds, `publish-pypi` is
      skipped, and `pip install --pre --index-url .../simple/ filament-api`
      installs the new `X.Y.Z.devN`.
- [ ] Re-run `publish-gcp` for that release: it succeeds, logging
      `Already in Artifact Registry, skipping: ...` for each file and
      `... is already fully uploaded`.
- [ ] First stable release: `publish-gcp` and `publish-pypi` both succeed, the
      PyPI page renders the README, and the version is installable from both
      PyPI and the internal index.
