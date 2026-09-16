from __future__ import annotations

from omm import auth, config
from omm import cli
from typer.testing import CliRunner


class _Backend:
    priority = 1

    def __init__(self):
        self.values = {}

    def get_password(self, service, provider):
        return self.values.get((service, provider))

    def set_password(self, service, provider, token):
        self.values[(service, provider)] = token

    def delete_password(self, service, provider):
        self.values.pop((service, provider), None)


class _UnavailableBackend(_Backend):
    def set_password(self, service, provider, token):
        raise RuntimeError("secret service locked")


class _UndeletableBackend(_Backend):
    def delete_password(self, service, provider):
        raise RuntimeError("store locked")


def test_environment_token_has_priority_over_secure_store(monkeypatch):
    backend = _Backend()
    backend.set_password(auth.SERVICE, "huggingface", "stored-token")
    monkeypatch.setattr(auth, "_native_backend", lambda: backend)
    monkeypatch.setenv("HF_TOKEN", "environment-token")

    assert auth.token_for("huggingface") == "environment-token"
    assert auth.token_source("huggingface") == "environment"


def test_missing_native_store_never_writes_plaintext(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(auth, "_native_backend", lambda: None)
    secret = "hf_secret_that_must_not_reach_files"

    result = auth.store_token("huggingface", secret)

    assert result["stored"] is False
    assert auth.token_source("huggingface") == "session"
    assert not config.CONFIG_PATH.exists()
    for path in config.OMM_HOME.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="ignore")


def test_huggingface_header_never_crosses_to_redirect_host(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "top-secret")
    assert auth.headers_for_url("https://huggingface.co/org/repo") == {
        "Authorization": "Bearer top-secret"
    }
    assert auth.headers_for_url("https://cdn.example/model.gguf") == {}


def test_unavailable_native_backend_falls_back_to_process_only(monkeypatch):
    monkeypatch.setattr(auth, "_native_backend", lambda: _UnavailableBackend())
    result = auth.store_token("lmstudio", "session-secret")
    assert result == {"provider": "lmstudio", "stored": False, "source": "session"}
    assert auth.token_for("lmstudio") == "session-secret"


def test_logout_does_not_claim_success_when_secret_remains(monkeypatch):
    backend = _UndeletableBackend()
    backend.values[(auth.SERVICE, "huggingface")] = "still-stored"
    monkeypatch.setattr(auth, "_native_backend", lambda: backend)
    try:
        auth.logout("huggingface")
    except auth.CredentialError as error:
        assert "did not delete" in str(error)
        assert "still-stored" not in str(error)
    else:
        raise AssertionError("logout falsely reported success")


def test_status_never_contains_token(monkeypatch):
    monkeypatch.setenv("LM_API_TOKEN", "lm-secret")
    rendered = repr(auth.status("lmstudio"))
    assert "lm-secret" not in rendered
    assert "environment" in rendered


def test_cli_login_reads_stdin_and_writes_only_native_store(isolated_omm_home, monkeypatch):
    backend = _Backend()
    monkeypatch.setattr(auth, "_native_backend", lambda: backend)
    monkeypatch.setattr(auth, "secure_store_name", lambda: "Test Native Store")
    result = CliRunner().invoke(
        cli.app,
        ["auth", "login", "huggingface", "--token-stdin"],
        input="hf_cli_secret\n",
    )
    assert result.exit_code == 0, result.output
    assert backend.get_password(auth.SERVICE, "huggingface") == "hf_cli_secret"
    assert "hf_cli_secret" not in result.output
    assert "hf_cli_secret" not in config.CONFIG_PATH.read_text(encoding="utf-8")
