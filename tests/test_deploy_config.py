"""Regression tests for the deployment/config contract.

The IP-mode deployment once shipped REFRESH_COOKIE_SECURE=false with
APP_ENV=production, which the Settings validator rejects — the documented
deployment path could never start. These tests pin the contract between
deploy/linux/configure.sh's transformations and app.core.config.Settings.
"""

from pathlib import Path

import pytest

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ENV_EXAMPLE = PROJECT_ROOT / "deploy" / "linux" / "app.env.example"

PROD_KWARGS = {
    "app_env": "production",
    "secret_key": "a" * 40,
    "judge_result_secret": "b" * 40,
    "cors_origins": "https://oj.example.com",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**PROD_KWARGS, **overrides})


def test_production_rejects_insecure_cookie_without_explicit_opt_in(monkeypatch):
    for key in ("APP_ENV", "SECRET_KEY", "JUDGE_RESULT_SECRET",
                "REFRESH_COOKIE_SECURE", "ALLOW_INSECURE_REFRESH_COOKIE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="REFRESH_COOKIE_SECURE"):
        _settings(refresh_cookie_secure=False)


def test_ip_mode_combination_is_accepted(monkeypatch):
    for key in ("APP_ENV", "SECRET_KEY", "JUDGE_RESULT_SECRET",
                "REFRESH_COOKIE_SECURE", "ALLOW_INSECURE_REFRESH_COOKIE"):
        monkeypatch.delenv(key, raising=False)
    settings = _settings(
        refresh_cookie_secure=False,
        allow_insecure_refresh_cookie=True,
        cors_origins="http://154.222.21.206",
    )
    assert settings.is_production
    assert settings.allow_insecure_refresh_cookie is True


def test_opt_in_does_not_waive_other_production_checks(monkeypatch):
    for key in ("APP_ENV", "SECRET_KEY", "JUDGE_RESULT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="SECRET_KEY"):
        _settings(
            secret_key="development-only-change-me-32-characters",
            refresh_cookie_secure=False,
            allow_insecure_refresh_cookie=True,
        )


def test_configure_sh_markers_exist_in_env_template():
    """configure.sh replaces exact marker strings; if the template drifts,
    the IP-mode substitutions silently stop applying. Pin them here."""
    content = APP_ENV_EXAMPLE.read_text(encoding="utf-8")
    for marker in (
        "APP_ENV=production",
        "REFRESH_COOKIE_SECURE=true",
        "ALLOW_INSECURE_REFRESH_COOKIE=false",
        "REFRESH_TOKEN_EXPIRE_DAYS=30",
        "CORS_ORIGINS=https://",
    ):
        assert marker in content, f"configure.sh marker missing from app.env.example: {marker}"


def test_ip_mode_transformed_template_passes_validation(monkeypatch):
    """Apply the same substitutions configure.sh performs in IP mode and
    verify the result is a valid production configuration."""
    for key in ("APP_ENV", "SECRET_KEY", "JUDGE_RESULT_SECRET", "REFRESH_COOKIE_SECURE",
                "ALLOW_INSECURE_REFRESH_COOKIE", "CORS_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    content = APP_ENV_EXAMPLE.read_text(encoding="utf-8")
    content = content.replace("REFRESH_COOKIE_SECURE=true", "REFRESH_COOKIE_SECURE=false")
    content = content.replace("ALLOW_INSECURE_REFRESH_COOKIE=false", "ALLOW_INSECURE_REFRESH_COOKIE=true")
    content = content.replace("REFRESH_TOKEN_EXPIRE_DAYS=30", "REFRESH_TOKEN_EXPIRE_DAYS=1")
    content = content.replace("CORS_ORIGINS=https://", "CORS_ORIGINS=http://")
    content = content.replace("CHANGE_ME_SECRET", "c" * 40)
    content = content.replace("CHANGE_ME_JUDGE_SECRET", "d" * 40)
    content = content.replace("__DOMAIN__", "154.222.21.206")
    env: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    settings = Settings(_env_file=None, **env)
    assert settings.is_production
    assert settings.refresh_cookie_secure is False
    assert settings.allow_insecure_refresh_cookie is True
    assert settings.refresh_token_expire_days == 1