#!/usr/bin/env python3
"""Environment / secret configuration audit (phase 11, public-demo security).

Compares the four places the backend's configuration is described and reports
drift and unsafe values. It prints variable NAMES only, never values, and it
reads no real environment (.env is never opened):

  config/settings/base.py   the Settings fields (parsed with ast, not imported,
                            so no JWT_SECRET / DATABASE_URL is needed)
  .env.example              the documented template
  render.yaml               the staging service's env vars (value: / sync: false)
  docs/staging_env_map.md   the human map of where every value comes from

Findings
  ERROR  a secret-like key is set with a literal ``value:`` in render.yaml (it
         must be ``sync: false``); a required secret (no code default) is
         missing from render.yaml; a provider secret is missing for a provider
         render.yaml switches on; an unsafe literal in render.yaml (APP_DEBUG
         true, CORS ``*``, ``change-me`` …); a render.yaml key that the app does
         not read (it would be silently ignored)
  WARN   a Settings field missing from .env.example or the env map; staging
         left on the published seed password (intentional for the public demo
         — see docs/runbooks/public-demo-checklist.md); unsafe code defaults
         that production must override (enforced at startup by
         config/settings/base.py::deployment_secrets_are_safe)

Exit status: 1 when any ERROR, else 0 (WARNs never fail).

Usage: python scripts/ops/env_audit.py [--root PATH] [--strict]
       --strict also fails on WARN.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PUBLISHED_SEED_PASSWORD = "change-me-seed-password"

# Name shapes that hold secret values.
# REDIS_URL (phase 12 follow-up) may carry the Redis password.
_SECRET_NAME = re.compile(
    r"(SECRET|PASSWORD|API_KEY|_TOKEN|ACCESS_KEY|DATABASE_URL|REDIS_URL|HOOK_URL)"
)
# Non-secret names that happen to match the pattern above.
_NOT_SECRET = {"JWT_ALGORITHM", "PASSWORD_HASH_ROUNDS", "SEED_DEFAULT_PASSWORD"}

# Keys render.yaml may set that are not Settings fields (read by infra).
_INFRA_KEYS = {"PORT", "SEED_ON_STARTUP"}
# Env-map / .env.example names that are not Settings fields by design.
_NON_SETTINGS_DOC_KEYS = {
    "PORT",
    "SEED_ON_STARTUP",
    "NEXT_PUBLIC_API_BASE_URL",
    "NEXT_PUBLIC_API_URL",
    "NEXT_PUBLIC_APP_NAME",
    "NEXT_PUBLIC_APP_ENV",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
}

# Secrets a provider needs once render.yaml selects it.
_PROVIDER_SECRETS: dict[tuple[str, str], tuple[str, ...]] = {
    ("LLM_PROVIDER", "openrouter"): ("LLM_API_KEY",),
    ("LLM_PROVIDER", "anthropic"): ("LLM_API_KEY",),
    ("EMBEDDINGS_PROVIDER", "openai"): ("EMBEDDINGS_API_KEY",),
    ("EMBEDDINGS_PROVIDER", "openrouter"): ("EMBEDDINGS_API_KEY",),
    ("STORAGE_PROVIDER", "minio"): ("STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY"),
    ("RERANK_PROVIDER", "cohere"): ("RERANK_API_KEY",),
    ("RERANK_PROVIDER", "novita"): ("RERANK_API_KEY",),
    ("RERANK_PROVIDER", "huggingface"): ("HF_API_TOKEN",),
    # The shared rate-limit store (docs/runbooks/rate-limit-redis.md).
    ("RATE_LIMIT_STORE", "redis"): ("REDIS_URL",),
}

_UNSAFE_LITERAL = re.compile(r"change-?me|your-|placeholder|example\.com", re.IGNORECASE)


@dataclass
class SettingsField:
    name: str
    has_default: bool
    default: Any = None


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def is_secret_name(name: str) -> bool:
    return name not in _NOT_SECRET and bool(_SECRET_NAME.search(name))


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def read_settings_fields(path: Path) -> dict[str, SettingsField]:
    """UPPER_CASE annotated attributes of ``class Settings`` (ast, not imported)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fields: dict[str, SettingsField] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            name = stmt.target.id
            if not name.isupper():
                continue
            default: Any = None
            has_default = stmt.value is not None
            if isinstance(stmt.value, ast.Constant):
                default = stmt.value.value
            elif isinstance(stmt.value, ast.Name):
                default = f"<{stmt.value.id}>"
            elif isinstance(stmt.value, ast.Call):
                for kw in stmt.value.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                        default = kw.value.value
            fields[name] = SettingsField(name, has_default, default)
    return fields


def read_env_example(path: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        # A commented "# KEY=" line documents an optional key that is unset by default.
        m = re.match(r"^\s*(?:#\s*)?([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            keys[m.group(1)] = m.group(2).split("#", 1)[0].strip()
    return keys


@dataclass
class RenderVar:
    key: str
    value: str | None
    sync_false: bool


def read_render_env(path: Path) -> dict[str, RenderVar]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, RenderVar] = {}
    for svc in doc.get("services") or []:
        for var in svc.get("envVars") or []:
            key = str(var.get("key"))
            value = var.get("value")
            out[key] = RenderVar(
                key=key,
                value=None if value is None else str(value),
                sync_false=var.get("sync") is False,
            )
    return out


def read_env_map_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*`([A-Z_][A-Z0-9_]*)`\s*\|", line)
        if m:
            names.add(m.group(1))
    return names


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit(root: Path) -> Report:
    report = Report()
    settings = read_settings_fields(root / "config" / "settings" / "base.py")
    example = read_env_example(root / ".env.example")
    render = read_render_env(root / "render.yaml")
    env_map = read_env_map_names(root / "docs" / "staging_env_map.md")

    # ---- render.yaml: secrets must be sync:false, never a literal value ------
    for key, var in sorted(render.items()):
        if is_secret_name(key) and var.value is not None:
            report.error(f"render.yaml sets secret {key} with a literal value (use sync: false)")
        if var.value is not None and _UNSAFE_LITERAL.search(var.value):
            report.error(f"render.yaml {key} has a placeholder-looking literal value")
        if key not in settings and key not in _INFRA_KEYS:
            report.error(f"render.yaml sets {key}, which the app does not read (silently ignored)")

    debug = render.get("APP_DEBUG")
    if debug is not None and str(debug.value).strip().lower() in ("true", "1", "yes"):
        report.error("render.yaml APP_DEBUG is true")
    cors = render.get("CORS_ALLOWED_ORIGINS")
    if cors is not None and cors.value is not None and "*" in cors.value:
        report.error("render.yaml CORS_ALLOWED_ORIGINS contains *")

    # ---- required secrets (no code default) must be provisioned -------------
    for name, f in sorted(settings.items()):
        if not f.has_default and name not in render:
            report.error(f"required setting {name} is missing from render.yaml")

    # ---- provider secrets for providers render.yaml switches on -------------
    for (provider_key, provider_value), needed in _PROVIDER_SECRETS.items():
        var = render.get(provider_key)
        if var is None or (var.value or "").strip().lower() != provider_value:
            continue
        for secret in needed:
            if secret not in render:
                report.error(
                    f"render.yaml selects {provider_key}={provider_value} but does not "
                    f"provision {secret} (sync: false)"
                )
            elif not render[secret].sync_false:
                report.error(f"render.yaml {secret} must be sync: false")

    # ---- unsafe code defaults production must override ----------------------
    seed_default = settings.get("SEED_DEFAULT_PASSWORD")
    if seed_default is not None and "SEED_DEFAULT_PASSWORD" not in render:
        report.warn(
            "staging does not set SEED_DEFAULT_PASSWORD: the demo accounts use the "
            "published demo password (intentional for the public demo; production "
            "refuses to start with it)"
        )
    debug_default = settings.get("APP_DEBUG")
    if debug_default is not None and debug_default.default is True:
        report.error("Settings.APP_DEBUG defaults to True (must default to False)")
    cors_default = settings.get("CORS_ALLOWED_ORIGINS")
    if cors_default is not None and "*" in str(cors_default.default or ""):
        report.error("Settings.CORS_ALLOWED_ORIGINS defaults to a wildcard")
    for name, f in sorted(settings.items()):
        if is_secret_name(name) and isinstance(f.default, str) and f.default:
            report.error(f"Settings.{name} has a non-empty code default for a secret")

    # ---- documentation drift ------------------------------------------------
    for name in sorted(settings):
        if name not in example:
            report.warn(f"Settings field {name} is missing from .env.example")
        if name not in env_map:
            report.warn(f"Settings field {name} is missing from docs/staging_env_map.md")
    for name in sorted(example):
        if name not in settings and name not in _NON_SETTINGS_DOC_KEYS:
            report.warn(f".env.example documents {name}, which the app does not read")
    if example.get("SEED_DEFAULT_PASSWORD") not in (None, PUBLISHED_SEED_PASSWORD):
        report.warn(
            ".env.example SEED_DEFAULT_PASSWORD differs from the published default the "
            "settings guard compares against"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    args = parser.parse_args(argv)
    report = audit(args.root)
    for msg in report.errors:
        print(f"ERROR  {msg}")
    for msg in report.warnings:
        print(f"WARN   {msg}")
    print(f"env audit: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
