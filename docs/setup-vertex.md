# Setting up vertex mode

One Google Cloud credential, federated from GitHub Actions, driving both models.
Nothing long-lived ends up in the repository.

Roughly fifteen minutes. The step people miss is [enabling Claude in Model
Garden](#3-enable-claude-in-model-garden) — skip it and every verification call
returns 404 while the Gemini half keeps working, which is a confusing way to
find out.

## Prerequisites

- A Google Cloud project with billing enabled
- `roles/owner` or equivalent on it, for the one-time setup
- `gcloud` authenticated: `gcloud auth login`
- Admin access to the GitHub repository, to add secrets

```bash
export PROJECT_ID="your-project-id"
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export GITHUB_OWNER="your-org"
export GITHUB_REPO="your-repo"
```

## 1. Enable the APIs

```bash
gcloud services enable aiplatform.googleapis.com iamcredentials.googleapis.com \
  --project="$PROJECT_ID"
```

## 2. Create the service account

`roles/aiplatform.user` is enough to call models. Do not grant more: this
identity is reachable from any workflow run in the repository.

```bash
gcloud iam service-accounts create quorum-review \
  --project="$PROJECT_ID" \
  --display-name="quorum-review PR reviewer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:quorum-review@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

## 3. Enable Claude in Model Garden

Claude on Vertex is a partner model and is off until someone accepts its terms
for the project. There is no `gcloud` command; it is a console action.

1. Open **Vertex AI → Model Garden** in the [Cloud Console](https://console.cloud.google.com/vertex-ai/model-garden).
2. Search for the Claude model you plan to use as the verifier.
3. Click **Enable** and accept the terms.

Note which regions the enablement covers. If it is region-scoped rather than
global, set `claude-vertex-region` on the action to match — see
[Troubleshooting](#troubleshooting).

While you are here, confirm your Gemini model ID. Availability varies by project
and release channel, and a stale ID is the most common first-run failure:

```bash
python -m src.review --list-models
```

## 4. Set up Workload Identity Federation

This is what replaces a stored service-account key. GitHub signs an OIDC token
describing the workflow run; Google Cloud verifies it and issues a short-lived
credential.

```bash
gcloud iam workload-identity-pools create "github" \
  --project="$PROJECT_ID" --location="global" \
  --display-name="GitHub Actions"

# The attribute condition is not optional. Without it, any GitHub repository
# anywhere could present a token to this provider.
gcloud iam workload-identity-pools providers create-oidc "quorum-review" \
  --project="$PROJECT_ID" --location="global" \
  --workload-identity-pool="github" \
  --display-name="quorum-review" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner == '${GITHUB_OWNER}'"
```

Now let exactly one repository impersonate the service account. The
`attribute.repository/...` suffix is the narrowing that matters — bind on the
pool alone and every repository in the org inherits the grant.

```bash
gcloud iam service-accounts add-iam-policy-binding \
  "quorum-review@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}"
```

## 5. Add the repository secrets

```bash
echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/quorum-review"
echo "quorum-review@${PROJECT_ID}.iam.gserviceaccount.com"
echo "$PROJECT_ID"
```

Add those three under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `WIF_PROVIDER` | the `projects/.../providers/quorum-review` string |
| `WIF_SERVICE_ACCOUNT` | the service account email |
| `GOOGLE_CLOUD_PROJECT` | your project ID |

None of these is a credential. They name an identity; the trust comes from the
attribute condition and the IAM binding above.

## 6. Add the workflow

Copy [`examples/review-vertex.yml`](../examples/review-vertex.yml) to
`.github/workflows/`. Two lines in it are load-bearing:

- `id-token: write` — without it the runner cannot mint an OIDC token and
  federation fails before any model is called.
- The `github.event.comment.user.type != 'Bot'` guard — without it, the summary
  comment this action posts re-triggers the workflow, which posts another
  comment. Do not remove it.

## 7. Verify locally first

Confirm both models answer on one credential before involving Actions. This
isolates a Google Cloud problem from a GitHub Actions problem, and the two fail
in very similar-looking ways.

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"

python - <<'PY'
import asyncio, os
from anthropic import AsyncAnthropicVertex
from google import genai

project = os.environ["GOOGLE_CLOUD_PROJECT"]

async def main():
    gemini = genai.Client(vertexai=True, project=project, location="global")
    reply = await gemini.aio.models.generate_content(
        model=os.getenv("PRIMARY_MODEL", "gemini-3.1-pro-preview"),
        contents="Reply with the single word: ok",
    )
    print("gemini:", (reply.text or "").strip())

    claude = AsyncAnthropicVertex(
        project_id=project, region=os.getenv("CLAUDE_VERTEX_REGION", "global")
    )
    message = await claude.messages.create(
        model=os.getenv("VERIFIER_MODEL", "claude-opus-5"),
        max_tokens=8000,   # thinking is on by default and shares this budget
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    print("claude:", "".join(b.text for b in message.content if b.type == "text").strip())

asyncio.run(main())
PY
```

Two `ok`s means one credential reached both models. That is the result this
whole repository exists to demonstrate.

## Troubleshooting

**`404` on the Claude call, Gemini fine.** Model Garden enablement is missing or
region-scoped. Re-check step 3, then try `claude-vertex-region: us-east5`.

**`403 Permission denied` on either.** The service account is missing
`roles/aiplatform.user`, or the workflow is federating as a different identity
than you think. IAM changes take up to five minutes to propagate.

**Federation fails before any model call.** Usually `id-token: write` missing
from `permissions:`, or `attribute.repository` in the IAM binding not matching
`owner/repo` exactly.

**`400 INVALID_ARGUMENT` on `rawPredict`, but the same code works locally.** A
[reported failure mode](https://github.com/anthropics/anthropic-sdk-go/issues/222)
specific to the Actions federated path. Pass the project ID explicitly rather
than relying on inference — the action does this via `google-cloud-project` —
and confirm the impersonated service account, not your user account, holds
`roles/aiplatform.user`.

**Findings appear but nothing is posted.** Check `pull-requests: write` in the
workflow's `permissions`, and note that pull requests from forks get a read-only
token — those are unsupported.

## Governance

Two things worth telling a platform team:

- **`vertexai.allowedModels`** — an organisation policy constraint that
  restricts which Vertex models any project may call. Because both models here
  are Vertex models, one policy governs both. That is not possible when half the
  pipeline runs on a vendor API key.
- **No key to rotate.** There is no service-account key file, so there is
  nothing to leak, rotate, or find in a git history later. Revoking access means
  removing one IAM binding.
