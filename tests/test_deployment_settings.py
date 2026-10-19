"""Configuration errors must fail early without printing supplied secrets."""
import pytest
from pydantic import ValidationError
from app.core.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field, raising=False)


def test_defaults_without_optional_keys():
    config = Settings(_env_file=None)
    assert config.SUGGEST_API_KEY is None and config.GEOCODER_API_KEY is None
    assert config.WORKERS == 1 and not config.RELOAD
    assert config.ALLOWED_ORIGINS == [] and config.TRUSTED_PROXY_IPS == ""


@pytest.mark.parametrize("name,value", [
    ("PORT", 0), ("PORT", 65536), ("PORT", "oops"), ("PORT", ""),
    ("BACKEND_ENV", "unknown"), ("WORKERS", 2), ("LOG_LEVEL", "verbose"),
    ("YANDEX_TIMEOUT_SECONDS", 0), ("YANDEX_TIMEOUT_SECONDS", float("nan")),
    ("YANDEX_CONNECT_TIMEOUT_SECONDS", -1), ("YANDEX_MAX_CONCURRENT", 0),
    ("YANDEX_SERVER_RATE_LIMIT", 0), ("YANDEX_RATE_WINDOW_SECONDS", 86401),
    ("YANDEX_GEOCODER_DAILY_BUDGET", -1), ("REQUEST_MAX_BYTES", 0),
    ("DATABASE_URL", "postgresql://private:secret@host/db"),
    ("DATABASE_URL", "sqlite+aiosqlite://"), ("DATABASE_URL", "sqlite:///db"),
    ("DATABASE_URL", "sqlite+aiosqlite:///db?uri=true"),
    ("DATABASE_URL", "sqlite+aiosqlite://private:secret@host/private-db"),
    ("TRUSTED_PROXY_IPS", "*"), ("TRUSTED_PROXY_IPS", "0.0.0.0/0"),
    ("TRUSTED_PROXY_IPS", "::/0"), ("TRUSTED_PROXY_IPS", "hostname"),
    ("ALLOWED_ORIGINS", ["*"]), ("ALLOWED_ORIGINS", ["https://host/path"]),
    ("YANDEX_SUGGEST_URL", "http://suggest-maps.yandex.ru/v1/suggest"),
    ("YANDEX_GEOCODER_URL", "https://attacker.example/v1/"),
])
def test_invalid_setting(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


@pytest.mark.parametrize("name", ["SUGGEST_API_KEY", "GEOCODER_API_KEY"])
@pytest.mark.parametrize("value", ["", "   ", "YOUR_KEY"])
def test_empty_and_example_keys_are_unconfigured(name, value):
    assert getattr(Settings(_env_file=None, **{name: value}), name) is None


def test_environment_overrides_dotenv_and_sensitive_repr(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PORT=8001\nSUGGEST_API_KEY=synthetic-secret\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "8002")
    config = Settings(_env_file=env)
    assert config.PORT == 8002 and config.SUGGEST_API_KEY == "synthetic-secret"
    assert "synthetic-secret" not in repr(config)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, DATABASE_URL="private-secret")
    assert "private-secret" not in str(error.value)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, DATABASE_URL="sqlite+aiosqlite://user:private-secret@host/db")
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_rejects_reload_and_volatile_database(environment):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BACKEND_ENV=environment, RELOAD=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BACKEND_ENV=environment,
                 DATABASE_URL="sqlite+aiosqlite:///:memory:")
    config = Settings(_env_file=None, BACKEND_ENV=environment,
                      DATABASE_URL="sqlite+aiosqlite:////data/catalog.db")
    assert config.BACKEND_ENV == environment


def test_explicit_proxy_networks_and_cors():
    config = Settings(_env_file=None, TRUSTED_PROXY_IPS="127.0.0.1, 10.5.0.0/24",
                      ALLOWED_ORIGINS=["https://client.example/"])
    assert config.TRUSTED_PROXY_IPS == "127.0.0.1,10.5.0.0/24"
    assert config.ALLOWED_ORIGINS == ["https://client.example"]


def test_launcher_proxy_and_no_access_logging(monkeypatch):
    import run
    monkeypatch.setattr(run.settings, "TRUSTED_PROXY_IPS", "")
    assert run.server_options()["proxy_headers"] is False
    assert run.server_options()["access_log"] is False
    monkeypatch.setattr(run.settings, "TRUSTED_PROXY_IPS", "127.0.0.1")
    assert run.server_options()["proxy_headers"] is True
    assert run.server_options()["forwarded_allow_ips"] == "127.0.0.1"


def test_worker_count_parses_actual_environment(monkeypatch):
    monkeypatch.setenv("WORKERS", "1")
    assert Settings(_env_file=None).WORKERS == 1
    monkeypatch.setenv("WORKERS", "2")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_credential_free_dotenv_example_is_usable():
    from pathlib import Path
    config = Settings(_env_file=Path(__file__).parents[1] / ".env.example")
    assert config.WORKERS == 1 and config.PORT == 8000
    assert config.GEOCODER_API_KEY is None and config.SUGGEST_API_KEY is None
