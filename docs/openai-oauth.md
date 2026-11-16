# openai-oauth backend — use your ChatGPT subscription

The `openai-oauth` backend runs graphify's LLM extraction on a **ChatGPT Plus/Pro
subscription** instead of pay-as-you-go API credit. It is the OpenAI analogue of
the `claude-cli` backend.

## How it works

ChatGPT subscription OAuth tokens are **not** API keys: they only authenticate
against the Codex backend (`https://chatgpt.com/backend-api/codex/responses`),
which speaks the Responses API over SSE — not the public `api.openai.com`
chat/completions API. This backend implements that protocol directly (standard
library only, no extra dependencies), following the same flow as the Codex CLI.

Supported paths: `graphify extract` / `update` (semantic extraction),
`--dedup-llm` tiebreaker, and `graphify label` (community naming).
The maintainer-only `graphify prs` triage is not supported.

## 1. Log in once (device-code flow, headless-friendly)

```sh
python -m graphify.openai_oauth
```

It prints a URL and a code. Open https://auth.openai.com/codex/device in any
browser (your laptop is fine — the server needs no browser), sign in to the
ChatGPT account that has the subscription, and enter the code. Tokens are
stored in `~/.graphify/openai-oauth.json` (mode 600) and refreshed automatically.

Credentials are **isolated from other OAuth clients** (pi, Codex CLI):
refresh tokens are single-use, so each client keeps its own login. If you
already use pi or Codex CLI on the same machine, just log in again here — the
sessions do not interfere.

## 2. Run graphify

```sh
graphify extract ./my-project --backend openai-oauth
# or make it the default for this shell:
export GRAPHIFY_BACKEND=openai-oauth
graphify update ./my-project
graphify label ./my-project --backend openai-oauth
```

The backend is never auto-detected; select it explicitly with `--backend` or
`GRAPHIFY_BACKEND`.

Default model: `gpt-6-luna`. Available models depend on your plan (Plus/Pro
and above); see OpenAI's Codex models page. Override with
`--model gpt-6.1-sol` or `GRAPHIFY_OPENAI_OAUTH_MODEL`.

## Concurrency

Subscription rate limits are stricter than API limits, so extraction and
labeling run **serially by default** (like `claude-cli`). To parallelize:

```sh
GRAPHIFY_OPENAI_OAUTH_PARALLEL=1 graphify extract ./my-project \
    --backend openai-oauth --max-concurrency 4
```

429/5xx responses are retried with backoff regardless.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GRAPHIFY_OPENAI_OAUTH_MODEL` | `gpt-6-luna` | Model override |
| `GRAPHIFY_OPENAI_OAUTH_AUTH_FILE` | `~/.graphify/openai-oauth.json` | Credential store path |
| `GRAPHIFY_OPENAI_OAUTH_REASONING_EFFORT` | `medium` | Reasoning effort (`low`/`medium`/`high`) |
| `GRAPHIFY_OPENAI_OAUTH_PARALLEL` | unset (serial) | `1` lifts the serial constraint |
| `GRAPHIFY_CODEX_BASE_URL` | `https://chatgpt.com/backend-api` | Codex endpoint override (proxies) |
| `GRAPHIFY_API_TIMEOUT` | `600` | Per-request timeout, seconds |
| `GRAPHIFY_MAX_RETRIES` | `3` | Retries on 429/5xx/network errors |

## Limitations

- **Text only** (v1): image files are downgraded to text-reference nodes, as
  with other non-vision backends.
- Output length is model-governed: the Codex backend rejects `max_tokens`
  fields, so `--token-budget` still controls *input* chunking but not output caps.
- Costs report as $0 (subscription-billed); usage token counts are still shown.
- Request timeouts surface as `TimeoutError` but do not trigger graphify's
  adaptive chunk-bisection (its classifier only recognizes SDK-specific timeout
  types). Lower `GRAPHIFY_API_TIMEOUT` if chunks routinely time out.
- If a refresh fails (revoked session, password change), re-run
  `python -m graphify.openai_oauth`.

## Applying this as a patch

This feature ships as a standalone patch against official graphify releases:

```sh
git apply 0001-feat-add-openai-oauth-backend.patch   # in a graphify checkout
# or: patch -p1 < 0001-feat-add-openai-oauth-backend.patch
```

After upgrading graphify, re-apply the patch; conflicts (if any) are confined
to the small hook sites in `graphify/llm.py` and `graphify/cli.py` — the bulk of
the feature lives in the new files `graphify/openai_oauth.py`,
`tests/test_openai_oauth.py`, and this document.