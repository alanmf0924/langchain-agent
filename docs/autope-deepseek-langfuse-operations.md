# AutoPE: DeepSeek V4 Pro And Langfuse Operations

This runbook contains variable names and verification steps only. Put real values in a
secret manager or protected GitHub environment, never in this repository.

## 1. Prepare The Repository

1. Create the first commit and push the repository to GitHub.
2. Enable Actions for the repository.
3. Create the protected GitHub Environment named `autope-candidate-evaluation`.
4. Add required reviewers to that Environment.
5. In that Environment, add the secret `AUTOPE_DEEPSEEK_API_KEY` with a DeepSeek API key.

The candidate workflow fixes the OpenAI-compatible endpoint to `https://api.deepseek.com`
and the model to `deepseek-v4-pro`. The endpoint is configuration, not a secret.

## 2. Local Candidate Generation

Install the optional candidate-generation dependency group, then export transient shell
variables in the current terminal:

```bash
cd /path/to/skin-assistant-langchain-backend
uv sync --group autope
cd promptfoo-evaluation
export AUTOPE_DSPY_MODEL='openai/deepseek-v4-pro'
export AUTOPE_DSPY_BASE_URL='https://api.deepseek.com'
export AUTOPE_DSPY_API_KEY='your-deepseek-api-key'
npm run autope -- dspy-generate --output autope/local/candidates.json
```

Review the resulting manifest. Move only an approved manifest into
`autope/candidates/review/` for source review. Do not commit files from `autope/local/`.

Before starting the workflow, an authorized evaluator must fill and approve
`promptfoo-evaluation/autope/dataset-splits.json`. Each of `training`, `validation`,
and `challenge` must be non-empty, mutually exclusive, and contain only known golden or
approved online case IDs. Set `approval.status` to `approved` and record the reviewer,
timestamp, and review ticket. The workflow rejects the placeholder file by design.

Before registering a release candidate, an authorized release owner must also approve
`promptfoo-evaluation/autope/release-policy.json`. It declares the canary traffic percentage,
observation window, rollback owner and runbook, and measurable rollback thresholds. This file
does not deploy traffic; it prevents a candidate report from being mistaken for a release plan.

Start the `AutoPE candidate evaluation` workflow manually with the reviewed manifest path,
candidate ID, and a cost ceiling. It creates the deterministic baseline in the same clean
runner before any model call. A passing report is `eligible_for_human_release_review`,
followed by a `pending_human_release_approval` registry artifact; neither publishes a Prompt.

## 3. Application And Evaluation Model Settings

For a deployed application, set these variables through its secret manager:

```text
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=<secret>
OPENAI_MODEL=deepseek-v4-pro
```

For optional EvalOps LLM generation or judging, set `EVAL_LLM_ENABLED=true` and either
`EVAL_OPENAI_API_KEY` or the application key. Keep `EVAL_OPENAI_MODEL=deepseek-v4-pro`
and `EVAL_OPENAI_BASE_URL=https://api.deepseek.com` explicit in production.

## 4. Langfuse Staging First

Install observability support in the deployment image with `uv sync --group observability`.
Inject the following variables in staging first:

```text
LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=<secret>
LANGFUSE_SECRET_KEY=<secret>
LANGFUSE_ENVIRONMENT=staging
```

After restart, run one synthetic request and confirm the root observation named
`skin-assistant.agent-run` contains only question hash and length plus structured result
summary. It must not contain the raw question, answer, customer identity, access token, or
chain-of-thought. A failed run is marked `ERROR` with `agent_run_failed` as its status message.

## 5. Governed Online Feedback Loop

The service writes summary-only `SPAN` observations. The review queue hashes any returned
input/output and never copies content into Git. Langfuse therefore cannot be used as a source
of user text. A human reviewer must use an approved customer-feedback, support, or quality
system to retrieve an authorized record, redact and rewrite a reusable question, verify its
business truth and expected evidence, then mark the case approved in `reviewed_cases.json`.

Only approved, de-identified cases may enter the train, validation, or challenge split. Model
judging and candidate prompts cannot override evidence, safety, stock, price, order, or
transaction controls.
