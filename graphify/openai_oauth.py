"""OpenAI Codex (ChatGPT subscription) OAuth backend.

Authenticates with the ChatGPT subscription OAuth flow used by the Codex CLI,
then calls the Codex Responses endpoint
(``https://chatgpt.com/backend-api/codex/responses``). Subscription tokens are
NOT valid against api.openai.com — they only work on the Codex backend, which
speaks the Responses API (not chat/completions) and rejects token-limit fields.

Credentials live in graphify's own store (``~/.graphify/openai-oauth.json``),
fully isolated from other OAuth clients (pi, Codex CLI): refresh tokens are
single-use, so sharing a store across clients would let one client's refresh
invalidate the other's. Log in once with ``python -m graphify.openai_oauth``
(device-code flow, headless-friendly).

Only the standard library is used (urllib) so this backend adds no dependency.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
# Cloudflare rejects the default Python-urllib User-Agent with a 530.
USER_AGENT = "graphify (python; codex-oauth)"
# Refresh a little early so a long extraction never straddles the expiry.
_EXPIRY_SKEW_MS = 60_000
# Refresh tokens are single-use: parallel workers must not each spend one.
_REFRESH_LOCK = threading.Lock()


def _auth_path() -> Path:
    override = os.environ.get("GRAPHIFY_OPENAI_OAUTH_AUTH_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".graphify" / "openai-oauth.json"


def load_credentials() -> dict | None:
    """Return the stored credential dict (``access``, ``refresh``, ``expires``)."""
    try:
        data = json.loads(_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("access") and data.get("refresh"):
        return data
    return None


def credentials_available() -> bool:
    return load_credentials() is not None


def _write_credentials(cred: dict) -> None:
    """Persist tokens atomically. Refresh tokens are single-use: the refreshed
    pair must reach disk reliably, and a torn write must never leave a stale
    refresh token behind."""
    path = _auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create at 0600 so tokens are never on disk with loose permissions, even
    # briefly; pid-unique name so concurrent processes don't share a tmp file.
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cred, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _api_timeout() -> float:
    raw = os.environ.get("GRAPHIFY_API_TIMEOUT", "").strip()
    try:
        value = float(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return 600.0


def _max_retries() -> int:
    raw = os.environ.get("GRAPHIFY_MAX_RETRIES", "").strip()
    try:
        value = int(raw)
        if value >= 0:
            return value
    except ValueError:
        pass
    return 3


def _post_form(url: str, fields: dict[str, str], timeout: float) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload_out = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload_out = {}
        return exc.code, payload_out


def _token_response_to_cred(json_data: dict) -> dict:
    if not json_data.get("access_token") or not json_data.get("refresh_token"):
        # Never interpolate the payload: a partial response may carry a live
        # bearer token, and this message ends up in stderr/logs.
        raise ValueError(
            "OpenAI token response missing access_token/refresh_token "
            f"(got keys: {sorted(json_data)})"
        )
    return {
        "access": json_data["access_token"],
        "refresh": json_data["refresh_token"],
        "expires": int(time.time() * 1000) + int(json_data.get("expires_in", 3600)) * 1000,
    }


def refresh_credentials(cred: dict) -> dict:
    max_retries = _max_retries()
    for attempt in range(max_retries + 1):
        try:
            data = _post_form(
                TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": cred["refresh"],
                    "client_id": CLIENT_ID,
                },
                _api_timeout(),
            )
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401):
                # Cross-process race: another graphify process may have spent the
                # same single-use refresh token and already stored the rotated
                # pair. Adopt it instead of forcing a re-login.
                latest = load_credentials()
                if (
                    latest is not None
                    and latest.get("refresh") != cred.get("refresh")
                    and (latest.get("expires") or 0) - _EXPIRY_SKEW_MS > time.time() * 1000
                ):
                    return latest
            raise ValueError(
                f"OpenAI OAuth token refresh failed ({exc.code}). The refresh token may be "
                "revoked or already consumed. Re-login with: python -m graphify.openai_oauth"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Refresh runs before call_codex's retry loop, so give transient
            # network failures the same backoff here.
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise ValueError(f"OpenAI OAuth token refresh failed: {exc}") from exc
    new_cred = _token_response_to_cred(data)
    _write_credentials(new_cred)
    return new_cred


def get_access_token() -> str:
    """Return a valid access token, refreshing and persisting if near expiry."""
    cred = load_credentials()
    if cred is None:
        raise ValueError(
            "No OpenAI OAuth credentials found. Log in once with "
            "`python -m graphify.openai_oauth` (device-code flow)."
        )
    expires = cred.get("expires") or 0
    if expires - _EXPIRY_SKEW_MS <= time.time() * 1000:
        with _REFRESH_LOCK:
            # Another thread may have refreshed while we waited: re-read before
            # spending the (single-use) refresh token.
            latest = load_credentials()
            if latest is not None and (latest.get("expires") or 0) - _EXPIRY_SKEW_MS > time.time() * 1000:
                return latest["access"]
            cred = refresh_credentials(latest or cred)
    return cred["access"]


def account_id_from_token(access: str) -> str:
    try:
        payload_b64 = access.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        account_id = (payload.get(JWT_CLAIM_PATH) or {}).get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    except Exception:
        pass
    raise ValueError("Failed to extract chatgpt_account_id from OAuth access token")


def _codex_base_url() -> str:
    raw = os.environ.get("GRAPHIFY_CODEX_BASE_URL", "").strip() or DEFAULT_CODEX_BASE_URL
    normalized = raw.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def _reasoning_effort() -> str:
    return os.environ.get("GRAPHIFY_OPENAI_OAUTH_REASONING_EFFORT", "").strip() or "medium"


def _sse_events(resp):
    """Yield ``(event, data)`` pairs from an SSE byte stream."""
    event = ""
    data_lines: list[str] = []
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif not line:
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    yield event, json.loads(data)
                except ValueError:
                    yield event, {"_raw": data}
            event = ""
            data_lines = []
    if data_lines:
        data = "\n".join(data_lines)
        try:
            yield event, json.loads(data)
        except ValueError:
            yield event, {"_raw": data}


def call_codex(
    model: str,
    system: str,
    user_message: str,
    *,
    access_token: str | None = None,
) -> tuple[str, int, int, str]:
    """One-shot Codex Responses call. Returns ``(text, input_tokens, output_tokens, finish_reason)``.

    The subscription backend rejects ``max_tokens``/``max_output_tokens`` and
    ``metadata``; none are sent. Output length is governed by the model itself.
    """
    token = access_token or get_access_token()
    account_id = account_id_from_token(token)
    body = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": system or "You are a helpful assistant.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": user_message}]}
        ],
        "text": {"verbosity": "low"},
        "reasoning": {"effort": _reasoning_effort()},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "originator": "graphify",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
        "User-Agent": USER_AGENT,
    }
    url = _codex_base_url()
    timeout = _api_timeout()
    max_retries = _max_retries()
    payload = json.dumps(body).encode()

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _consume_sse(resp)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = exc.headers.get("retry-after") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
                except ValueError:
                    delay = min(2 ** attempt, 30)
                time.sleep(delay)
                continue
            if exc.code == 401:
                raise ValueError(
                    "Codex backend rejected the OAuth token (401). Re-login with "
                    "`python -m graphify.openai_oauth`."
                ) from exc
            raise ValueError(f"Codex backend HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            # Keep timeouts typed. Note: graphify's bisection classifier only
            # matches SDK-specific timeout types, so these do not trigger
            # chunk bisection (documented limitation).
            timed_out = isinstance(exc, TimeoutError) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
            )
            if timed_out:
                raise TimeoutError(f"Codex backend request timed out after {timeout}s") from exc
            raise ValueError(f"Codex backend request failed: {exc}") from exc


def _consume_sse(resp) -> tuple[str, int, int, str]:
    text_parts: list[str] = []
    item_texts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    finish_reason = "stop"
    completed = False
    for event, data in _sse_events(resp):
        if event == "response.output_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event == "response.output_item.done":
            item = data.get("item") or {}
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        item_texts.append(part["text"])
        elif event in ("response.completed", "response.incomplete"):
            completed = True
            response = data.get("response") or {}
            usage = response.get("usage") or {}
            input_tokens = usage.get("input_tokens") or 0
            output_tokens = usage.get("output_tokens") or 0
            if event == "response.incomplete" or response.get("status") == "incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason")
                finish_reason = "length" if reason == "max_output_tokens" else "stop"
        elif event == "response.failed":
            response = data.get("response") or {}
            error = response.get("error") or {}
            raise ValueError(f"Codex response failed: {error.get('message') or error or data}")
        elif event == "error":
            raise ValueError(f"Codex stream error: {data.get('message') or data}")
    text = "".join(text_parts) or "".join(item_texts)
    if not completed and not text:
        raise ValueError("Codex stream ended without a completed response or text")
    return text, input_tokens, output_tokens, finish_reason


def login_device_code() -> dict:
    """Headless device-code login. Prints the URL + code, polls, stores tokens."""
    timeout = _api_timeout()
    status, data = _post_json(DEVICE_USER_CODE_URL, {"client_id": CLIENT_ID}, timeout)
    if status != 200 or not data.get("device_auth_id") or not data.get("user_code"):
        raise ValueError(f"Device code request failed ({status}): {data}")
    interval = data.get("interval")
    interval = float(interval.strip() if isinstance(interval, str) else interval or 5)
    print(
        f"\nOpen {DEVICE_VERIFICATION_URI} in a browser and enter code: {data['user_code']}\n",
        file=sys.stderr,
        flush=True,
    )
    deadline = time.time() + DEVICE_CODE_TIMEOUT_SECONDS
    authorization_code = code_verifier = None
    while time.time() < deadline:
        time.sleep(max(interval, 1))
        status, token_data = _post_json(
            DEVICE_TOKEN_URL,
            {"device_auth_id": data["device_auth_id"], "user_code": data["user_code"]},
            timeout,
        )
        if status == 200 and token_data.get("authorization_code"):
            authorization_code = token_data["authorization_code"]
            code_verifier = token_data.get("code_verifier")
            break
        error = token_data.get("error")
        error_code = error.get("code") if isinstance(error, dict) else error
        if status in (403, 404) or error_code == "deviceauth_authorization_pending":
            continue
        if error_code == "slow_down":
            interval += 5
            continue
        raise ValueError(f"Device auth failed ({status}): {token_data}")
    if not authorization_code:
        raise ValueError("Device code login timed out")
    exchanged = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": authorization_code,
            "code_verifier": code_verifier or "",
            "redirect_uri": DEVICE_REDIRECT_URI,
        },
        timeout,
    )
    cred = _token_response_to_cred(exchanged)
    account_id_from_token(cred["access"])  # validate claim presence early
    _write_credentials(cred)
    print(f"[graphify] OpenAI OAuth credentials saved to {_auth_path()}", file=sys.stderr)
    return cred


if __name__ == "__main__":
    login_device_code()