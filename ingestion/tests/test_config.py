"""Settings precedence: .env > environment > defaults, then the secret scope."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from nfs_ingest.config import _SECRET_KEYS, Settings, env_files

ALL_CREDENTIALS = "\n".join(
    f"{field.upper()}=from-dotenv-{field}" for field in _SECRET_KEYS
)


class FakeSecrets:
    """Stands in for WorkspaceClient().secrets; records every lookup."""

    def __init__(self, values: dict[str, str]):
        self.values, self.calls = values, []

    def get_secret(self, scope: str, key: str):
        self.calls.append((scope, key))
        if key not in self.values:
            raise KeyError(key)
        encoded = base64.b64encode(self.values[key].encode()).decode()
        return SimpleNamespace(value=encoded)


def client(values: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(secrets=FakeSecrets(values or {}))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for field in [*_SECRET_KEYS, "mcp_port", "nms_timeout_seconds", "nfs_secret_scope",
                  "nfs_env_file"]:
        monkeypatch.delenv(field.upper(), raising=False)


def settings_from(tmp_path, text: str) -> Settings:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return Settings(_env_file=str(path))


def test_dotenv_beats_environment_for_secret_and_plain_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("NMS_CLIENT_ID", "from-env")
    monkeypatch.setenv("MCP_PORT", "9000")
    monkeypatch.setenv("NMS_TIMEOUT_SECONDS", "45")
    s = settings_from(tmp_path, "NMS_CLIENT_ID=from-dotenv\nMCP_PORT=8123\n")
    assert s.nms_client_id == "from-dotenv"  # credential
    assert s.mcp_port == 8123  # non-secret setting
    assert s.nms_timeout_seconds == 45  # not in .env: environment still applies


def test_empty_dotenv_value_does_not_blank_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NMS_CLIENT_SECRET", "from-env")
    s = settings_from(tmp_path, "NMS_CLIENT_SECRET=\nNMS_CLIENT_ID=\n")
    assert s.nms_client_secret == "from-env"
    assert s.nms_client_id == ""  # default, still missing
    assert "nms_client_id" in s.missing_credentials()


def test_full_dotenv_never_touches_the_secret_scope(tmp_path):
    s = settings_from(tmp_path, ALL_CREDENTIALS)
    fake = client({"nms-client-id": "from-scope"})
    assert s.missing_credentials() == []
    assert s.apply_secret_scope(fake) == []
    assert fake.secrets.calls == []
    assert s.nms_client_id == "from-dotenv-nms_client_id"


def test_secret_scope_fills_only_what_dotenv_left_out(tmp_path):
    s = settings_from(
        tmp_path, "NMS_CLIENT_ID=from-dotenv\nNMS_CLIENT_SECRET=dotenv-secret\n"
    )
    fake = client({"nms-client-id": "from-scope", "swim-password": "scope-pw"})
    applied = s.apply_secret_scope(fake)
    assert applied == ["swim_password"]
    assert s.nms_client_id == "from-dotenv"  # .env wins over the scope
    assert s.swim_password == "scope-pw"
    looked_up = {key for _, key in fake.secrets.calls}
    assert "nms-client-id" not in looked_up and "nms-client-secret" not in looked_up


def test_secret_scope_can_be_disabled(tmp_path):
    s = settings_from(tmp_path, "NFS_SECRET_SCOPE=none\n")
    fake = client({"nms-client-id": "from-scope"})
    assert s.apply_secret_scope(fake) == []
    assert fake.secrets.calls == []


def test_env_file_locations(monkeypatch, tmp_path):
    files = env_files()
    assert files[-1] == ".env"  # working-directory .env wins over repo root
    assert files[0].endswith(".env") and len(files) == 2
    custom = tmp_path / "local.env"
    monkeypatch.setenv("NFS_ENV_FILE", str(custom))
    assert env_files() == (str(custom),)
