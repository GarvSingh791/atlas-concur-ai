"""Phase 11 (public-demo security) — CI workflows must not leak credentials.

The E2E job used to run Playwright with ``--reporter=list,html`` on the command
line (which overrides ``frontend/playwright.artifact-policy.ts``) and upload the
HTML report as an artifact. That report records every request verbatim — the
login POST body (the shared demo password) and live bearer / refresh tokens —
so the artifact was a credential dump readable by anyone with repo read access.

This module parses every workflow under ``.github/workflows`` and fails when:

* a Playwright run asks for the ``html`` / ``blob`` reporter or a trace, or a
  workflow sets the env switches that turn them on;
* an ``upload-artifact`` step has no ``retention-days`` (or more than 3), uploads a
  directory / glob instead of an explicit allow-list of files, or uploads a path
  that can hold credentials (``.auth``, storage states, ``.env``, Playwright
  reports / test-results / traces, compose logs);
* a ``run:`` script prints a secret: ``${{ secrets.* }}`` inlined into a script,
  an ``echo`` / ``printf`` of a known secret env var, or a step that calls
  ``/auth/login`` and prints the response body;
* the published seed/demo password appears as a literal in a workflow;
* a job that runs Playwright does not replace and mask ``SEED_DEFAULT_PASSWORD``.

The checker is exercised twice: against the real workflow files (the gate) and
against small synthetic workflows (proving each rule actually fires).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The published demo / seed password (config default + .env.example). It must
# never appear literally in a workflow: CI E2E uses a per-run throwaway value and
# the staging smoke reads it from a secret.
PUBLISHED_SEED_PASSWORD = "change-me-seed-password"

# Env var names whose VALUES are secrets. Printing them (echo/printf) is a leak
# even when GitHub masking would usually hide them (masking misses transformed
# values, e.g. base64 or a substring).
SECRET_ENV_NAMES = (
    "JWT_SECRET",
    "DATABASE_URL",
    "SEED_DEFAULT_PASSWORD",
    "DEMO_PASSWORD",
    "STAGING_DEMO_PASSWORD",
    "RENDER_DEPLOY_HOOK_URL",
    "LLM_API_KEY",
    "OCR_API_KEY",
    "EMBEDDINGS_API_KEY",
    "RERANK_API_KEY",
    "HF_API_TOKEN",
    "STORAGE_ACCESS_KEY_ID",
    "STORAGE_SECRET_ACCESS_KEY",
    "POSTGRES_PASSWORD",
    "GITHUB_TOKEN",
    "VERCEL_TOKEN",
    "RENDER_API_KEY",
    "NEON_API_KEY",
)

MAX_RETENTION_DAYS = 3

# Upload paths that can carry credentials or session state.
FORBIDDEN_UPLOAD_FRAGMENTS = (
    ".auth",
    "storagestate",
    "storage-state",
    "storage_state",
    ".env",
    "playwright-report",
    "test-results",
    "blob-report",
    "e2e/.artifacts",
    "trace",
    ".zip",
    "compose",
    "docker-logs",
    ".log",
)

_PRINT_CMD = re.compile(r"^\s*(?:echo|printf|cat\s+<<)\b")
# "printf '%s' "$BODY" | python3 -c ..." feeds a parser; the FIRST pipe decides.
_PIPED_TO_PARSER = re.compile(r"^\s*(?:echo|printf)\b[^|]*\|\s*(?:python3?|jq)\b")
_REPORTER_FLAG = re.compile(r"--reporter(?:=|\s+)(\S+)")
_TRACE_FLAG = re.compile(r"--trace(?:=|\s+)(\S+)")
_SENSITIVE_PW_ENV = ("E2E_HTML_REPORT", "E2E_TRACE", "E2E_ALLOW_SENSITIVE_ARTIFACTS")


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in job.get("steps") or [] if isinstance(s, dict)]


def _env_blocks(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every ``env:`` mapping in the workflow, labelled for messages."""
    blocks: list[tuple[str, dict[str, Any]]] = []
    if isinstance(workflow.get("env"), dict):
        blocks.append(("workflow", workflow["env"]))
    for job_name, job in (workflow.get("jobs") or {}).items():
        if isinstance(job.get("env"), dict):
            blocks.append((f"job {job_name}", job["env"]))
        for i, step in enumerate(_steps(job)):
            if isinstance(step.get("env"), dict):
                blocks.append((f"job {job_name} step {i}", step["env"]))
    return blocks


def _is_upload(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith("actions/upload-artifact")


def _check_playwright_reporting(where: str, script: str) -> list[str]:
    problems: list[str] = []
    if "playwright" not in script:
        return problems
    for value in _REPORTER_FLAG.findall(script):
        reporters = {r.strip().strip("'\"") for r in value.split(",")}
        if reporters & {"html", "blob"}:
            problems.append(f"{where}: Playwright --reporter {value} writes a token-bearing report")
    for value in _TRACE_FLAG.findall(script):
        if value.strip("'\"") != "off":
            problems.append(f"{where}: Playwright --trace {value} records credentials")
    if re.search(r"--video(?:=|\s+)(?!off)", script):
        problems.append(f"{where}: Playwright video recording is on")
    return problems


def _check_upload(where: str, step: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    with_ = step.get("with") or {}
    days = with_.get("retention-days")
    if days is None:
        problems.append(f"{where}: upload-artifact has no retention-days")
    else:
        try:
            if int(days) > MAX_RETENTION_DAYS:
                problems.append(f"{where}: retention-days {days} > {MAX_RETENTION_DAYS}")
        except (TypeError, ValueError):
            problems.append(f"{where}: retention-days {days!r} is not a number")
    raw = str(with_.get("path", ""))
    paths = [p.strip() for p in raw.splitlines() if p.strip()]
    if not paths:
        problems.append(f"{where}: upload-artifact has no path")
    for p in paths:
        low = p.lower()
        if p.startswith("!"):
            continue  # an exclusion only narrows the upload
        if p.endswith("/") or "*" in p or "?" in p:
            problems.append(f"{where}: upload path {p!r} is a directory/glob, not a listed file")
        for frag in FORBIDDEN_UPLOAD_FRAGMENTS:
            if frag in low:
                problems.append(f"{where}: upload path {p!r} can hold credentials ({frag})")
                break
    return problems


def _check_script_printing(where: str, script: str) -> list[str]:
    problems: list[str] = []
    if "${{ secrets." in script.replace("${{secrets.", "${{ secrets."):
        problems.append(f"{where}: secrets inlined into a run script (pass them through env:)")
    calls_login = "/auth/login" in script
    for line in script.splitlines():
        if not _PRINT_CMD.search(line) or "::add-mask::" in line:
            continue
        if _PIPED_TO_PARSER.search(line):
            continue  # fed to a parser on stdin, not written to the log
        for name in SECRET_ENV_NAMES:
            if re.search(rf"\$\{{?{name}\b", line):
                problems.append(f"{where}: prints secret env {name}: {line.strip()[:80]}")
        if calls_login and re.search(r"\$\{?(BODY|RESP|RESPONSE|TOKEN\w*)\b", line):
            problems.append(f"{where}: prints an /auth/login response: {line.strip()[:80]}")
    return problems


def check_workflow(name: str, text: str) -> list[str]:
    """Return every hygiene violation in one workflow file."""
    problems: list[str] = []
    if PUBLISHED_SEED_PASSWORD in text:
        problems.append(f"{name}: the published seed password appears literally")
    workflow = yaml.safe_load(text) or {}
    for label, env in _env_blocks(workflow):
        for key in _SENSITIVE_PW_ENV:
            if key in env and str(env[key]).strip() not in ("", "0", "off"):
                problems.append(f"{name} {label}: sets {key} (token-bearing artifacts)")
        for key, value in env.items():
            if str(key).upper() == "SEED_DEFAULT_PASSWORD" and "secrets." not in str(value):
                problems.append(f"{name} {label}: SEED_DEFAULT_PASSWORD hard-coded in env")
    for job_name, job in (workflow.get("jobs") or {}).items():
        runs_playwright = False
        replaces_seed_pw = False
        for i, step in enumerate(_steps(job)):
            where = f"{name} job {job_name} step {i} ({step.get('name', step.get('uses', ''))})"
            # Shell comment lines are documentation, not commands.
            script = "\n".join(
                line
                for line in str(step.get("run") or "").splitlines()
                if not line.lstrip().startswith("#")
            )
            if "playwright test" in script:
                runs_playwright = True
            if (
                "::add-mask::" in script
                and "SEED_DEFAULT_PASSWORD=" in script
                and "GITHUB_ENV" in script
            ):
                replaces_seed_pw = True
            problems += _check_playwright_reporting(where, script)
            problems += _check_script_printing(where, script)
            if _is_upload(step):
                problems += _check_upload(where, step)
        if runs_playwright and not replaces_seed_pw:
            problems.append(
                f"{name} job {job_name}: runs Playwright without a masked, per-run "
                "SEED_DEFAULT_PASSWORD (it would log in with the published demo password)"
            )
    return problems


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])


# ---------------------------------------------------------------------------
# The gate — the real workflows
# ---------------------------------------------------------------------------


def test_there_are_workflows_to_check() -> None:
    assert _workflow_files(), "no workflow files found — the hygiene gate would be vacuous"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_is_credential_hygienic(path: Path) -> None:
    problems = check_workflow(path.name, path.read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


def test_ci_e2e_runs_playwright_and_uploads_no_report() -> None:
    """The E2E lane still exists (the gate is not vacuous) and uploads nothing
    from the Playwright run."""
    ci = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    e2e = ci["jobs"]["e2e"]
    scripts = "\n".join(str(s.get("run") or "") for s in _steps(e2e))
    assert "playwright test" in scripts
    for step in _steps(e2e):
        if _is_upload(step):
            assert "playwright" not in str(step.get("with", {}).get("path", "")).lower()


def test_e2e_password_helper_reads_the_env() -> None:
    """The E2E suite must log in with the CI throwaway password, not a hard-coded
    copy of the published default."""
    helpers = (REPO_ROOT / "frontend" / "e2e" / "helpers.ts").read_text(encoding="utf-8")
    assert "process.env.SEED_DEFAULT_PASSWORD" in helpers


# ---------------------------------------------------------------------------
# The rules fire — synthetic workflows
# ---------------------------------------------------------------------------

_GOOD_E2E = """
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - name: throwaway seed password
        run: |
          PW="ci-$(openssl rand -hex 16)"
          echo "::add-mask::${PW}"
          echo "SEED_DEFAULT_PASSWORD=${PW}" >> "$GITHUB_ENV"
      - run: npx playwright test --project=setup
"""


def test_synthetic_clean_workflow_passes() -> None:
    assert check_workflow("good.yml", _GOOD_E2E) == []


@pytest.mark.parametrize(
    ("label", "snippet"),
    [
        ("html reporter", "      - run: npx playwright test --reporter=list,html\n"),
        ("html reporter spaced", "      - run: npx playwright test --reporter html\n"),
        ("blob reporter", "      - run: npx playwright test --reporter=blob\n"),
        ("trace on", "      - run: npx playwright test --trace on\n"),
        ("video on", "      - run: npx playwright test --video=on\n"),
        (
            "inline secret",
            '      - run: |\n          curl -H "x: ${{ secrets.TOKEN }}" http://x\n',
        ),
        ("echo secret env", '      - run: echo "jwt=$JWT_SECRET"\n'),
        ("echo braced secret env", '      - run: echo "${RENDER_DEPLOY_HOOK_URL}"\n'),
        (
            "login body printed",
            "      - run: |\n"
            "          BODY=$(curl -s http://x/api/v1/auth/login)\n"
            '          echo "body: $BODY"\n',
        ),
        ("literal seed password", "      - run: echo change-me-seed-password > /dev/null\n"),
        (
            "upload without retention",
            "      - uses: actions/upload-artifact@v4\n"
            "        with:\n          name: x\n          path: out/metrics.json\n",
        ),
        (
            "upload retention too long",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: out/metrics.json\n          retention-days: 30\n",
        ),
        (
            "upload a directory",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: evals/reports/\n          retention-days: 3\n",
        ),
        (
            "upload playwright report",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: frontend/playwright-report/index.html\n          retention-days: 1\n",
        ),
        (
            "upload auth state",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: frontend/.auth/employee.json\n          retention-days: 1\n",
        ),
        (
            "upload env file",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: .env\n          retention-days: 1\n",
        ),
        (
            "upload test-results",
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: x\n"
            "          path: frontend/test-results/results.json\n          retention-days: 1\n",
        ),
    ],
)
def test_each_rule_fires(label: str, snippet: str) -> None:
    problems = check_workflow("bad.yml", _GOOD_E2E + snippet)
    assert problems, f"rule did not fire: {label}"


@pytest.mark.parametrize(
    "env_line",
    [
        "      E2E_HTML_REPORT: '1'\n",
        "      E2E_TRACE: retain-on-failure\n",
        "      E2E_ALLOW_SENSITIVE_ARTIFACTS: '1'\n",
        "      SEED_DEFAULT_PASSWORD: hunter2-hunter2\n",
    ],
)
def test_sensitive_env_switches_fire(env_line: str) -> None:
    text = _GOOD_E2E.replace(
        "    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    env:\n" + env_line, 1
    )
    assert check_workflow("bad.yml", text)


def test_playwright_job_without_masked_password_fires() -> None:
    text = """
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --project=setup
"""
    problems = check_workflow("bad.yml", text)
    assert any("SEED_DEFAULT_PASSWORD" in p for p in problems)
