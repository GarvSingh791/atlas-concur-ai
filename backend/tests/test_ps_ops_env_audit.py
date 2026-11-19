"""Phase 11 — scripts/ops/env_audit.py finds unsafe / drifting configuration.

Hermetic: every case builds a tiny synthetic repo (settings, .env.example,
render.yaml, env map) in tmp_path; one case audits the real repository.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ps_ops_env_audit", REPO_ROOT / "scripts" / "ops" / "env_audit.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


env_audit = _load()

# Built at runtime so this file holds no secret-shaped literal for
# scripts/ops/secret_scan.py to flag.
FAKE_KEY = "sk-" + "or-v1-" + "q7" * 16

SETTINGS = """
class Settings(BaseSettings):
    APP_ENV: str = "development"
    APP_DEBUG: bool = False
    CORS_ALLOWED_ORIGINS: str = ""
    JWT_SECRET: str
    DATABASE_URL: str
    SEED_DEFAULT_PASSWORD: str = "change-me-seed-password"
    STORAGE_PROVIDER: str = "local"
    STORAGE_ACCESS_KEY_ID: str = ""
    STORAGE_SECRET_ACCESS_KEY: str = ""
"""

RENDER_OK = """
services:
  - type: web
    envVars:
      - key: APP_ENV
        value: staging
      - key: APP_DEBUG
        value: "false"
      - key: JWT_SECRET
        sync: false
      - key: DATABASE_URL
        sync: false
      - key: CORS_ALLOWED_ORIGINS
        sync: false
      - key: STORAGE_PROVIDER
        value: minio
      - key: STORAGE_ACCESS_KEY_ID
        sync: false
      - key: STORAGE_SECRET_ACCESS_KEY
        sync: false
      - key: PORT
        value: "8000"
"""

NAMES = (
    "APP_ENV",
    "APP_DEBUG",
    "CORS_ALLOWED_ORIGINS",
    "JWT_SECRET",
    "DATABASE_URL",
    "SEED_DEFAULT_PASSWORD",
    "STORAGE_PROVIDER",
    "STORAGE_ACCESS_KEY_ID",
    "STORAGE_SECRET_ACCESS_KEY",
)


def _repo(tmp_path: Path, *, settings: str = SETTINGS, render: str = RENDER_OK) -> Path:
    (tmp_path / "config" / "settings").mkdir(parents=True)
    (tmp_path / "config" / "settings" / "base.py").write_text(settings)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".env.example").write_text("\n".join(f"{n}=x" for n in NAMES) + "\n")
    (tmp_path / "docs" / "staging_env_map.md").write_text(
        "\n".join(f"| `{n}` | code-default | x | x |" for n in NAMES) + "\n"
    )
    (tmp_path / "render.yaml").write_text(render)
    return tmp_path


def test_the_real_repository_has_no_errors() -> None:
    report = env_audit.audit(REPO_ROOT)
    assert report.errors == []


def test_clean_synthetic_repo_has_no_errors(tmp_path: Path) -> None:
    report = env_audit.audit(_repo(tmp_path))
    assert report.errors == []
    # Staging on the published seed password is reported, but only as a warning.
    assert any("SEED_DEFAULT_PASSWORD" in w for w in report.warnings)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "secret as literal value",
            "      - key: JWT_SECRET\n        sync: false\n",
            "      - key: JWT_SECRET\n        value: " + FAKE_KEY + "\n",
        ),
        ("required secret missing", "      - key: DATABASE_URL\n        sync: false\n", ""),
        (
            "provider secret missing",
            "      - key: STORAGE_SECRET_ACCESS_KEY\n        sync: false\n",
            "",
        ),
        (
            "debug on",
            '      - key: APP_DEBUG\n        value: "false"\n',
            '      - key: APP_DEBUG\n        value: "true"\n',
        ),
        (
            "cors wildcard",
            "      - key: CORS_ALLOWED_ORIGINS\n        sync: false\n",
            '      - key: CORS_ALLOWED_ORIGINS\n        value: "*"\n',
        ),
        (
            "unread key",
            '      - key: PORT\n        value: "8000"\n',
            "      - key: OPENROUTER_API_KEY\n        sync: false\n",
        ),
        (
            "placeholder literal",
            "      - key: APP_ENV\n        value: staging\n",
            "      - key: APP_ENV\n        value: change-me\n",
        ),
    ],
)
def test_render_findings_are_errors(tmp_path: Path, label: str, old: str, new: str) -> None:
    assert old in RENDER_OK, label
    report = env_audit.audit(_repo(tmp_path, render=RENDER_OK.replace(old, new)))
    assert report.errors, f"no error for: {label}"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("APP_DEBUG: bool = False", "APP_DEBUG: bool = True"),
        ('CORS_ALLOWED_ORIGINS: str = ""', 'CORS_ALLOWED_ORIGINS: str = "*"'),
        ('STORAGE_SECRET_ACCESS_KEY: str = ""', 'STORAGE_SECRET_ACCESS_KEY: str = "minioadmin"'),
    ],
)
def test_unsafe_code_defaults_are_errors(tmp_path: Path, old: str, new: str) -> None:
    report = env_audit.audit(_repo(tmp_path, settings=SETTINGS.replace(old, new)))
    assert report.errors


def test_drift_is_a_warning(tmp_path: Path) -> None:
    root = _repo(tmp_path, settings=SETTINGS + '    NEW_KNOB: str = "x"\n')
    report = env_audit.audit(root)
    assert report.errors == []
    assert any("NEW_KNOB" in w and ".env.example" in w for w in report.warnings)
    assert any("NEW_KNOB" in w and "staging_env_map" in w for w in report.warnings)


def test_output_never_contains_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = FAKE_KEY
    render = RENDER_OK.replace(
        "      - key: JWT_SECRET\n        sync: false\n",
        f"      - key: JWT_SECRET\n        value: {secret}\n",
    )
    code = env_audit.main(["--root", str(_repo(tmp_path, render=render))])
    out = capsys.readouterr().out
    assert code == 1
    assert "JWT_SECRET" in out
    assert secret not in out


def test_strict_fails_on_warnings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert env_audit.main(["--root", str(root)]) == 0
    assert env_audit.main(["--root", str(root), "--strict"]) == 1


# ---------------------------------------------------------------------------
# Phase 12 follow-up: the Redis rate-limit store settings
# ---------------------------------------------------------------------------


def test_redis_url_is_a_secret_name() -> None:
    assert env_audit.is_secret_name("REDIS_URL")
    assert not env_audit.is_secret_name("RATE_LIMIT_REDIS_PREFIX")
    assert not env_audit.is_secret_name("RATE_LIMIT_STORE")


def test_redis_url_as_a_literal_value_is_an_error(tmp_path: Path) -> None:
    settings = SETTINGS + '    REDIS_URL: str = ""\n    RATE_LIMIT_STORE: str = "memory"\n'
    render = RENDER_OK + "      - key: REDIS_URL\n        value: redis://cache:6379/0\n"
    report = env_audit.audit(_repo(tmp_path, settings=settings, render=render))
    assert any("REDIS_URL" in e and "literal" in e for e in report.errors)


def test_redis_store_without_a_redis_url_is_an_error(tmp_path: Path) -> None:
    settings = SETTINGS + '    REDIS_URL: str = ""\n    RATE_LIMIT_STORE: str = "memory"\n'
    render = RENDER_OK + "      - key: RATE_LIMIT_STORE\n        value: redis\n"
    report = env_audit.audit(_repo(tmp_path, settings=settings, render=render))
    assert any("RATE_LIMIT_STORE=redis" in e and "REDIS_URL" in e for e in report.errors)
    ok = render + "      - key: REDIS_URL\n        sync: false\n"
    assert env_audit.audit(_repo(tmp_path / "ok", settings=settings, render=ok)).errors == []


def test_the_real_repository_knows_the_redis_settings() -> None:
    fields = env_audit.read_settings_fields(REPO_ROOT / "config" / "settings" / "base.py")
    names = {
        "RATE_LIMIT_STORE",
        "REDIS_URL",
        "RATE_LIMIT_REDIS_TIMEOUT_MS",
        "RATE_LIMIT_REDIS_PREFIX",
        "RATE_LIMIT_REDIS_FAILURE",
    }
    assert names <= set(fields)
    report = env_audit.audit(REPO_ROOT)
    assert not [w for w in report.warnings if any(n in w for n in names)]
