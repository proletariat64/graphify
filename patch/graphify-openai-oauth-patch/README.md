# graphify openai-oauth patch

Run [graphify](https://github.com/Graphify-Labs/graphify) on your **ChatGPT Plus/Pro subscription** instead of pay-as-you-go API credit.

**Version 0.1.0** · self-use provider patch · zero new dependencies

## What it does

Adds an `openai-oauth` LLM backend — the OpenAI analogue of `claude-cli`. Subscription OAuth tokens authenticate against the Codex Responses backend (`chatgpt.com/backend-api/codex/responses`), not `api.openai.com`; this patch implements that protocol with the Python standard library only.

- Covers `extract` / `update`, `label`, and `--dedup-llm`
- Device-code login (headless-friendly); credentials stored separately from pi / Codex CLI
- Default model `gpt-6-luna`, reasoning effort `medium`
- Serial by default; opt-in parallelism
- Text-only (images become text-reference nodes)

## Install

Requires `git` on the target machine. Target is a graphify source checkout or its installed `site-packages` directory:

```sh
python3 -c "import graphify,os;print(os.path.dirname(os.path.dirname(graphify.__file__)))"
```

```sh
./install.sh <graphify-dir>
```

The installer skips if already applied, pre-checks, and falls back to `--3way` / `--reject` on conflict. Manual equivalent:

```sh
cd <graphify-dir>
git apply 0001-feat-add-openai-oauth-backend.patch
```

## Login (once)

```sh
python -m graphify.openai_oauth
```

Open https://auth.openai.com/codex/device in any browser, sign in to the subscribed ChatGPT account, enter the printed code. Tokens are saved to `~/.graphify/openai-oauth.json` (mode 600) and refreshed automatically.

## Usage

```sh
graphify extract ./my-project --backend openai-oauth
graphify label   ./my-project --backend openai-oauth

# or set the default:
export GRAPHIFY_BACKEND=openai-oauth
```

The backend is never auto-detected; always select it explicitly.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GRAPHIFY_OPENAI_OAUTH_MODEL` | `gpt-6-luna` | Model |
| `GRAPHIFY_OPENAI_OAUTH_REASONING_EFFORT` | `medium` | `low` / `medium` / `high` |
| `GRAPHIFY_OPENAI_OAUTH_PARALLEL` | unset (serial) | `1` enables parallelism (`--max-concurrency N`) |
| `GRAPHIFY_OPENAI_OAUTH_AUTH_FILE` | `~/.graphify/openai-oauth.json` | Credential store path |
| `GRAPHIFY_CODEX_BASE_URL` | `https://chatgpt.com/backend-api` | Endpoint override |
| `GRAPHIFY_API_TIMEOUT` | `600` | Request timeout (seconds) |
| `GRAPHIFY_MAX_RETRIES` | `3` | Retries on 429/5xx/network errors |

## Compatibility

Self-use patch; only these targets are tested:

| Target | Apply | Tests |
|---|---|---|
| Upstream `v8` HEAD @ `81b1664` (≡ release `v0.9.111`) | clean | 289/289 |
| Installed graphify `0.9.107` | offsets only | 45/45 |

After upgrading graphify, re-apply the patch. If it fails: `git apply --3way`, else `git apply --reject` and merge the `.rej` hunks by hand — conflicts are confined to small hooks in `graphify/llm.py` and `graphify/cli.py`; all other files are new and never conflict. Run the test set to confirm:

```sh
pytest tests/test_openai_oauth.py tests/test_llm_backends.py \
       tests/test_backend_env_isolation.py tests/test_claude_cli_backend.py
```

(Outside a repo checkout, copy the repo's `tests/conftest.py` next to the tests.)

## Limitations

- No vision input; no `graphify prs` triage
- Output length is model-governed (the Codex backend rejects token-limit fields)
- Timeouts do not trigger graphify's chunk bisection; lower `GRAPHIFY_API_TIMEOUT` if needed
- Costs report as $0 (subscription-billed); token counts are still shown

## Package contents

```
0001-feat-add-openai-oauth-backend.patch   # code + tests + docs
install.sh                                 # installer
README.md
```

Full backend documentation ships inside the patch as `docs/openai-oauth.md`.