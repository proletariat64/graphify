"""Tests for the openai-oauth (ChatGPT subscription / Codex) backend."""

import base64
import io
import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from graphify import llm, openai_oauth


def _make_jwt(account_id="acct-123"):
    payload = {openai_oauth.JWT_CLAIM_PATH: {"chatgpt_account_id": account_id}}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{b64}.signature"


def _cred(access=None, refresh="refresh-1", expires=None):
    return {
        "access": access or _make_jwt(),
        "refresh": refresh,
        "expires": expires if expires is not None else int(time.time() * 1000) + 3600_000,
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    for key in (
        "GRAPHIFY_OPENAI_OAUTH_AUTH_FILE",
        "GRAPHIFY_CODEX_BASE_URL",
        "GRAPHIFY_OPENAI_OAUTH_MODEL",
        "GRAPHIFY_OPENAI_OAUTH_REASONING_EFFORT",
        "GRAPHIFY_OPENAI_OAUTH_PARALLEL",
        "GRAPHIFY_API_TIMEOUT",
        "GRAPHIFY_MAX_RETRIES",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GRAPHIFY_OPENAI_OAUTH_AUTH_FILE", str(tmp_path / "oauth.json"))


def _store(cred):
    Path(openai_oauth._auth_path()).write_text(json.dumps(cred), encoding="utf-8")


def _sse_bytes(events):
    out = []
    for event, data in events:
        out.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")
    return "".join(out).encode()


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _completed_event(input_tokens=10, output_tokens=20, status="completed", reason=None):
    response = {"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}, "status": status}
    if reason:
        response["incomplete_details"] = {"reason": reason}
    return ("response.completed", {"response": response})


# ---------------------------------------------------------------- credentials

def test_account_id_from_jwt():
    assert openai_oauth.account_id_from_token(_make_jwt("acct-xyz")) == "acct-xyz"


def test_account_id_missing_claim_raises():
    b64 = base64.urlsafe_b64encode(json.dumps({"sub": "u"}).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="chatgpt_account_id"):
        openai_oauth.account_id_from_token(f"h.{b64}.s")


def test_account_id_garbage_token_raises():
    with pytest.raises(ValueError):
        openai_oauth.account_id_from_token("not-a-jwt")


def test_load_credentials_missing_file():
    assert openai_oauth.load_credentials() is None
    assert openai_oauth.credentials_available() is False


def test_load_credentials_roundtrip():
    cred = _cred()
    _store(cred)
    assert openai_oauth.load_credentials() == cred
    assert openai_oauth.credentials_available() is True


def test_load_credentials_rejects_incomplete():
    Path(openai_oauth._auth_path()).write_text('{"access": "a"}', encoding="utf-8")
    assert openai_oauth.load_credentials() is None


def test_write_credentials_atomic_and_private():
    openai_oauth._write_credentials(_cred())
    path = Path(openai_oauth._auth_path())
    assert json.loads(path.read_text())["refresh"] == "refresh-1"
    assert not list(path.parent.glob("*.tmp"))
    assert (path.stat().st_mode & 0o777) == 0o600


def test_get_access_token_no_credentials():
    with pytest.raises(ValueError, match="python -m graphify.openai_oauth"):
        openai_oauth.get_access_token()


def test_get_access_token_fresh_skips_refresh():
    _store(_cred())
    with patch.object(openai_oauth, "_post_form") as post:
        token = openai_oauth.get_access_token()
    post.assert_not_called()
    assert token == _make_jwt()


def test_get_access_token_expired_refreshes_and_persists():
    _store(_cred(expires=int(time.time() * 1000) - 1000))
    new_access = _make_jwt("acct-new")
    response = {"access_token": new_access, "refresh_token": "refresh-2", "expires_in": 3600}
    with patch.object(openai_oauth, "_post_form", return_value=response) as post:
        token = openai_oauth.get_access_token()
    assert token == new_access
    fields = post.call_args[0][1]
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == "refresh-1"
    assert fields["client_id"] == openai_oauth.CLIENT_ID
    stored = json.loads(Path(openai_oauth._auth_path()).read_text())
    assert stored["refresh"] == "refresh-2"
    assert stored["access"] == new_access


def test_refresh_failure_mentions_relogin():
    _store(_cred(expires=0))
    err = urllib.error.HTTPError(openai_oauth.TOKEN_URL, 400, "Bad Request", {}, None)
    with patch.object(openai_oauth, "_post_form", side_effect=err):
        with pytest.raises(ValueError, match="Re-login"):
            openai_oauth.get_access_token()


def test_refresh_400_adopts_other_process_rotation():
    _store(_cred(expires=int(time.time() * 1000) - 1000))
    winner_access = _make_jwt("acct-winner")

    def race_then_fail(url, fields, timeout):
        # Simulate the winning process storing its rotated pair first.
        _store(_cred(access=winner_access, refresh="refresh-winner"))
        raise urllib.error.HTTPError(openai_oauth.TOKEN_URL, 400, "Bad Request", {}, None)

    with patch.object(openai_oauth, "_post_form", side_effect=race_then_fail):
        assert openai_oauth.get_access_token() == winner_access


def test_token_error_never_leaks_token_value():
    with pytest.raises(ValueError) as ei:
        openai_oauth._token_response_to_cred({"access_token": "SECRET-BEARER"})
    assert "SECRET-BEARER" not in str(ei.value)
    assert "access_token" in str(ei.value)


def test_parallel_refresh_spends_token_once():
    import threading

    _store(_cred(expires=int(time.time() * 1000) - 1000))
    new_access = _make_jwt("acct-race")
    response = {"access_token": new_access, "refresh_token": "refresh-2", "expires_in": 3600}
    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append(openai_oauth.get_access_token())

    with patch.object(openai_oauth, "_post_form", return_value=response) as post:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert post.call_count == 1
    assert results == [new_access] * 4


# ------------------------------------------------------------------ url shape

def test_auth_requests_send_non_default_user_agent():
    # Cloudflare 530s the default Python-urllib User-Agent.
    reqs = []

    def fake(req, timeout=None):
        reqs.append(req)
        return _FakeResponse(b'{"ok": true}')

    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        openai_oauth._post_form("https://x", {"a": "b"}, 5)
        openai_oauth._post_json("https://x", {"a": "b"}, 5)
    for req in reqs:
        ua = req.get_header("User-agent")
        assert ua and "Python-urllib" not in ua


@pytest.mark.parametrize("raw,expected", [
    ("", "https://chatgpt.com/backend-api/codex/responses"),
    ("https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/codex/responses"),
    ("https://chatgpt.com/backend-api/", "https://chatgpt.com/backend-api/codex/responses"),
    ("https://proxy.example/codex", "https://proxy.example/codex/responses"),
    ("https://proxy.example/codex/responses", "https://proxy.example/codex/responses"),
])
def test_codex_base_url_normalization(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("GRAPHIFY_CODEX_BASE_URL", raw)
    assert openai_oauth._codex_base_url() == expected


# ------------------------------------------------------------------ call_codex

@pytest.fixture
def stored_creds():
    _store(_cred())


def _capture_request(sse_events):
    """Patch urlopen; return (fake opener, captured dict)."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(_sse_bytes(sse_events))

    return fake_urlopen, captured


def test_call_codex_request_shape(stored_creds):
    events = [
        ("response.output_text.delta", {"delta": '{"nodes": []}'}),
        _completed_event(),
    ]
    fake, captured = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        text, inp, out, finish = openai_oauth.call_codex("gpt-6.1-sol", "sys", "hello")
    assert (text, inp, out, finish) == ('{"nodes": []}', 10, 20, "stop")
    req = captured["req"]
    assert req.full_url == "https://chatgpt.com/backend-api/codex/responses"
    body = json.loads(req.data)
    assert body["model"] == "gpt-6.1-sol"
    assert body["store"] is False
    assert body["stream"] is True
    assert body["instructions"] == "sys"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    # The subscription backend rejects token-limit fields and metadata.
    for forbidden in ("max_tokens", "max_output_tokens", "max_completion_tokens", "metadata", "temperature"):
        assert forbidden not in body
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["authorization"] == f"Bearer {_make_jwt()}"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["openai-beta"] == "responses=experimental"
    assert headers["accept"] == "text/event-stream"
    assert headers["originator"]


def test_call_codex_incomplete_maps_to_length(stored_creds):
    events = [
        ("response.output_text.delta", {"delta": "partial"}),
        _completed_event(status="incomplete", reason="max_output_tokens"),
    ]
    fake, _ = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        _, _, _, finish = openai_oauth.call_codex("m", "s", "u")
    assert finish == "length"


def test_call_codex_incomplete_event_maps_to_length(stored_creds):
    # The real protocol sends truncation as a terminal response.incomplete event.
    events = [
        ("response.output_text.delta", {"delta": "partial"}),
        ("response.incomplete", {"response": {
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "incomplete_details": {"reason": "max_output_tokens"},
        }}),
    ]
    fake, _ = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        text, inp, out, finish = openai_oauth.call_codex("m", "s", "u")
    assert (text, inp, out, finish) == ("partial", 3, 4, "length")


def test_call_codex_item_done_fallback(stored_creds):
    events = [
        ("response.output_item.done", {"item": {"type": "message", "content": [
            {"type": "output_text", "text": "fallback text"}]}}),
        _completed_event(),
    ]
    fake, _ = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        text, _, _, _ = openai_oauth.call_codex("m", "s", "u")
    assert text == "fallback text"


def test_call_codex_failed_event_raises(stored_creds):
    events = [("response.failed", {"response": {"error": {"message": "boom"}}})]
    fake, _ = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        with pytest.raises(ValueError, match="boom"):
            openai_oauth.call_codex("m", "s", "u")


def test_call_codex_empty_stream_raises(stored_creds):
    fake, _ = _capture_request([])
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        with pytest.raises(ValueError, match="without a completed response"):
            openai_oauth.call_codex("m", "s", "u")


def test_call_codex_401_raises_relogin(stored_creds):
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(ValueError, match="401"):
            openai_oauth.call_codex("m", "s", "u")


def test_call_codex_429_retries_then_succeeds(monkeypatch, stored_creds):
    monkeypatch.setenv("GRAPHIFY_MAX_RETRIES", "2")
    events = [("response.output_text.delta", {"delta": "ok"}), _completed_event()]
    fake, _ = _capture_request(events)
    err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"retry-after": "0"}, io.BytesIO(b"{}"))
    retry_resp = _FakeResponse(_sse_bytes(events))
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=[err, retry_resp]) as popen, \
         patch.object(openai_oauth.time, "sleep") as sleep:
        text, _, _, _ = openai_oauth.call_codex("m", "s", "u")
    assert text == "ok"
    assert popen.call_count == 2
    sleep.assert_called_once_with(0.0)


def test_call_codex_400_no_retry(stored_creds):
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"detail":"nope"}'))
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=err) as popen:
        with pytest.raises(ValueError, match="400"):
            openai_oauth.call_codex("m", "s", "u")
    assert popen.call_count == 1


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    urllib.error.URLError(TimeoutError("timed out")),
])
def test_call_codex_timeout_stays_typed(monkeypatch, stored_creds, exc):
    # graphify's adaptive-bisection layer matches on exception type, so an
    # exhausted timeout must surface as TimeoutError, not a wrapped ValueError.
    monkeypatch.setenv("GRAPHIFY_MAX_RETRIES", "0")
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=exc):
        with pytest.raises(TimeoutError):
            openai_oauth.call_codex("m", "s", "u")


def test_call_codex_network_error_wraps(monkeypatch, stored_creds):
    monkeypatch.setenv("GRAPHIFY_MAX_RETRIES", "0")
    err = urllib.error.URLError(ConnectionRefusedError("refused"))
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(ValueError, match="request failed"):
            openai_oauth.call_codex("m", "s", "u")


def test_call_codex_incomplete_read_retries(monkeypatch, stored_creds):
    import http.client

    monkeypatch.setenv("GRAPHIFY_MAX_RETRIES", "1")
    events = [("response.output_text.delta", {"delta": "ok"}), _completed_event()]
    retry_resp = _FakeResponse(_sse_bytes(events))
    err = http.client.IncompleteRead(b"partial")
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=[err, retry_resp]) as popen, \
         patch.object(openai_oauth.time, "sleep"):
        text, _, _, _ = openai_oauth.call_codex("m", "s", "u")
    assert text == "ok"
    assert popen.call_count == 2


def test_refresh_network_error_retries(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_MAX_RETRIES", "1")
    _store(_cred(expires=int(time.time() * 1000) - 1000))
    new_access = _make_jwt("acct-net")
    response = {"access_token": new_access, "refresh_token": "refresh-2", "expires_in": 3600}
    err = urllib.error.URLError(ConnectionResetError("reset"))
    with patch.object(openai_oauth, "_post_form", side_effect=[err, response]) as post, \
         patch.object(openai_oauth.time, "sleep"):
        assert openai_oauth.get_access_token() == new_access
    assert post.call_count == 2


# ------------------------------------------------------------- llm integration

def test_backend_registered():
    cfg = llm.BACKENDS["openai-oauth"]
    assert cfg["pricing"] == {"input": 0.0, "output": 0.0}
    assert cfg["default_model"] == "gpt-6-luna"
    assert llm._backend_env_keys("openai-oauth") == []
    assert llm._backend_supports_vision("openai-oauth") is False


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OPENAI_OAUTH_MODEL", "gpt-6-luna")
    assert llm._default_model_for_backend("openai-oauth") == "gpt-6-luna"


def test_never_auto_detected(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OPENAI_OAUTH_MODEL", "gpt-6.1-sol")
    _store(_cred())
    assert llm.detect_backend() is None
    monkeypatch.setenv("GRAPHIFY_BACKEND", "openai-oauth")
    assert llm.detect_backend() == "openai-oauth"


def test_env_keys_message_mentions_login():
    assert "python -m graphify.openai_oauth" in llm._format_backend_env_keys("openai-oauth")


def test_extract_dispatch(tmp_path, stored_creds):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n")
    events = [
        ("response.output_text.delta", {"delta": '{"nodes": [{"id": "n1", "label": "foo", "type": "function"}], "edges": []}'}),
        _completed_event(),
    ]
    fake, captured = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        result = llm.extract_files_direct([f], backend="openai-oauth", root=tmp_path)
    assert result["nodes"][0]["label"] == "foo"
    assert result["input_tokens"] == 10
    assert result["model"] == "gpt-6-luna"
    body = json.loads(captured["req"].data)
    assert "extract" in body["instructions"].lower()


def test_extract_no_credentials_raises(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    with pytest.raises(ValueError, match="python -m graphify.openai_oauth"):
        llm.extract_files_direct([f], backend="openai-oauth", root=tmp_path)


def test_extract_image_files_become_text_notes(tmp_path, stored_creds):
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    events = [
        ("response.output_text.delta", {"delta": '{"nodes": [], "edges": []}'}),
        _completed_event(),
    ]
    fake, captured = _capture_request(events)
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        llm.extract_files_direct([img], backend="openai-oauth", root=tmp_path)
    body = json.loads(captured["req"].data)
    assert "=== IMAGES ===" in body["input"][0]["content"][0]["text"]
    assert "diagram.png" in body["input"][0]["content"][0]["text"]


def test_api_key_sentinel_tracks_credentials():
    assert llm._get_backend_api_key("openai-oauth") == ""
    _store(_cred())
    assert llm._get_backend_api_key("openai-oauth") != ""


def test_llm_text_dispatch(stored_creds):
    events = [("response.output_text.delta", {"delta": "same concept"}), _completed_event(5, 7)]
    fake, captured = _capture_request(events)
    usage = {}
    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake):
        text = llm._call_llm("are these the same?", backend="openai-oauth", usage_out=usage)
    assert text == "same concept"
    assert usage == {"input": 5, "output": 7}
    body = json.loads(captured["req"].data)
    assert body["input"][0]["content"][0]["text"] == "are these the same?"


def _run_parallel(tmp_path, monkeypatch, parallel_env):
    _store(_cred())
    files = []
    for i in range(3):
        f = tmp_path / f"f{i}.py"
        f.write_text(f"def fn{i}():\n    pass\n")
        files.append(f)
    if parallel_env:
        monkeypatch.setenv("GRAPHIFY_OPENAI_OAUTH_PARALLEL", "1")
    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        user_text = body["input"][0]["content"][0]["text"]
        # Per-file nodes so the response is not "hollow" (which would trigger
        # real retry sleeps in extract_corpus_parallel).
        names = [ln.split("def ")[1].split("(")[0] for ln in user_text.splitlines() if ln.startswith("def ")]
        nodes = [{"id": n, "label": n, "type": "function"} for n in names] or [{"id": "n", "label": "n", "type": "function"}]
        events = [("response.output_text.delta", {"delta": json.dumps({"nodes": nodes, "edges": []})}), _completed_event()]
        return _FakeResponse(_sse_bytes(events))

    seen_workers = []
    real_executor = llm.ThreadPoolExecutor

    def spy_executor(max_workers=None, **kwargs):
        seen_workers.append(max_workers)
        return real_executor(max_workers=max_workers, **kwargs)

    with patch.object(openai_oauth.urllib.request, "urlopen", side_effect=fake_urlopen), \
         patch.object(llm, "ThreadPoolExecutor", side_effect=spy_executor), \
         patch.object(llm.time, "sleep"):
        llm.extract_corpus_parallel(files, backend="openai-oauth", root=tmp_path,
                                    token_budget=None, chunk_size=1, max_concurrency=4)
    return seen_workers


def test_parallel_forced_serial_by_default(tmp_path, monkeypatch):
    # workers == 1 takes the no-thread-pool sequential path in extract_corpus_parallel.
    assert _run_parallel(tmp_path, monkeypatch, parallel_env=False) == []


def test_parallel_env_enables_concurrency(tmp_path, monkeypatch):
    assert _run_parallel(tmp_path, monkeypatch, parallel_env=True) == [3]