"""Backend entrypoint: which boot steps may stop the container (deploy blocker 2).

The policy_corpus boot runs two REPAIRS of derived/pending state
(backfill_submitted_approvals, refresh_draft_presubmit_flags). They exit 1 when
any single row fails; under ``set -e`` that used to stop the container, so one
bad legacy row on staging would block every later deploy. Repairs now only warn.
Migrations, the demo seed, the corpus reindex and the card-feed top-up stay fatal.

Hermetic: the real entrypoint runs under /bin/sh with stub ``alembic`` and
``python`` executables on PATH, so no database or Python app is involved.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = (
    Path(__file__).resolve().parents[2] / "docker" / "entrypoints" / "backend-entrypoint.sh"
)

_STUB_PYTHON = """#!/bin/sh
# python -m <module>: fail when the module is listed in FAIL_MODULES.
echo "ran $2" >> "$TRACE"
echo "$*" >> "$TRACE.args"
# Phase 10: the sweep reads ELAPSED_TRIP_SWEEP itself — record what it was given.
[ "$2" = "database.seeds.complete_elapsed_trips" ] && \\
  echo "${ELAPSED_TRIP_SWEEP-<unset>}" >> "$TRACE.sweep"
case " $FAIL_MODULES " in *" $2 "*) exit 1 ;; esac
exit 0
"""
_STUB_ALEMBIC = """#!/bin/sh
echo "ran alembic" >> "$TRACE"
[ -n "$FAIL_ALEMBIC" ] && exit 1
exit 0
"""
_STUB_CMD = """#!/bin/sh
echo "ran app" >> "$TRACE"
exit 0
"""


def _run(
    tmp_path: Path,
    seed: str,
    fail_modules: str = "",
    fail_alembic: bool = False,
    extra_env: dict[str, str] | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("python", _STUB_PYTHON), ("alembic", _STUB_ALEMBIC), ("app", _STUB_CMD)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    trace = tmp_path / "trace.log"
    trace.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TRACE": str(trace),
        "SEED_ON_STARTUP": seed,
        "FAIL_MODULES": fail_modules,
        "FAIL_ALEMBIC": "1" if fail_alembic else "",
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "app"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, trace.read_text().splitlines()


BACKFILL = "database.seeds.backfill_submitted_approvals"
GRADE_ROUTING = "database.seeds.backfill_grade_routing"
REFRESH = "database.seeds.refresh_draft_presubmit_flags"
REINDEX = "database.seeds.reindex_policy_corpus"
TOPUP = "database.seeds.topup_demo_card_feed"
SEED_ALL = "database.seeds.seed_all"
# Product gap 8: approved trips that ended > 1 day ago -> completed (a repair).
COMPLETE = "database.seeds.complete_elapsed_trips"
# Demo accounts' traveller grades (AIR-04) on the policy_corpus boot path.
GRADES = "database.seeds.seed_demo_grades"
# Operator-controlled staging test-data cleanup (TEST_DATA_CLEANUP=off|dry-run|apply).
CLEANUP = "database.seeds.cleanup_test_data"


pytestmark = pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs /bin/sh")


def test_policy_corpus_happy_path_runs_every_step_then_the_app(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "policy_corpus")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert trace == [
        "ran alembic",
        f"ran {REINDEX}",
        f"ran {TOPUP}",
        f"ran {BACKFILL}",
        f"ran {GRADE_ROUTING}",
        f"ran {REFRESH}",
        f"ran {GRADES}",
        f"ran {COMPLETE}",
        "ran app",
    ]


@pytest.mark.parametrize(
    "failing",
    [
        BACKFILL,
        GRADE_ROUTING,
        REFRESH,
        GRADES,
        COMPLETE,
        f"{BACKFILL} {GRADE_ROUTING} {REFRESH} {GRADES} {COMPLETE}",
    ],
)
def test_failed_repair_warns_and_the_app_still_starts(tmp_path: Path, failing: str) -> None:
    proc, trace = _run(tmp_path, "policy_corpus", fail_modules=failing)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert trace[-1] == "ran app"
    # A failed backfill does not skip the repairs after it.
    assert f"ran {REFRESH}" in trace and f"ran {GRADE_ROUTING}" in trace
    assert f"ran {GRADES}" in trace and f"ran {COMPLETE}" in trace
    assert "WARNING" in proc.stdout


@pytest.mark.parametrize("failing", [REINDEX, TOPUP])
def test_failed_reindex_or_topup_stops_the_container(tmp_path: Path, failing: str) -> None:
    proc, trace = _run(tmp_path, "policy_corpus", fail_modules=failing)
    assert proc.returncode != 0
    assert "ran app" not in trace


def test_failed_migration_stops_the_container_before_any_seed_step(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "policy_corpus", fail_alembic=True)
    assert proc.returncode != 0
    assert trace == ["ran alembic"]


def test_full_seed_mode_refresh_failure_is_a_warning_but_seed_failure_is_fatal(
    tmp_path: Path,
) -> None:
    proc, trace = _run(tmp_path, "true", fail_modules=REFRESH)
    assert proc.returncode == 0 and trace[-1] == "ran app"
    proc, trace = _run(tmp_path, "true", fail_modules=COMPLETE)
    assert proc.returncode == 0 and trace[-2:] == [f"ran {COMPLETE}", "ran app"]
    proc, trace = _run(tmp_path, "true", fail_modules=SEED_ALL)
    assert proc.returncode != 0 and "ran app" not in trace


# ---------------------------------------------------------------------------
# Phase 10 — operator control of the elapsed-trip sweep (ELAPSED_TRIP_SWEEP)
# ---------------------------------------------------------------------------


def _sweep_env(tmp_path: Path) -> list[str]:
    path = tmp_path / "trace.log.sweep"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("seed", ["policy_corpus", "true"])
@pytest.mark.parametrize(
    ("value", "passed"),
    # Empty = unset = the default (on), in the shell and in the module alike.
    [(None, "<unset>"), ("", ""), ("on", "on"), ("dry-run", "dry-run"), ("DRY-RUN", "DRY-RUN")],
)
def test_sweep_runs_when_on_or_dry_run_and_gets_the_mode(
    tmp_path: Path, seed: str, value: str | None, passed: str
) -> None:
    env = {} if value is None else {"ELAPSED_TRIP_SWEEP": value}
    proc, trace = _run(tmp_path, seed, extra_env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert trace[-2:] == [f"ran {COMPLETE}", "ran app"]
    # The module itself decides between applying and previewing.
    assert _sweep_env(tmp_path) == [passed]


@pytest.mark.parametrize("seed", ["policy_corpus", "true"])
@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_sweep_off_skips_the_step_and_the_app_still_starts(
    tmp_path: Path, seed: str, value: str
) -> None:
    proc, trace = _run(tmp_path, seed, extra_env={"ELAPSED_TRIP_SWEEP": value})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ran {COMPLETE}" not in trace
    assert trace[-1] == "ran app"
    assert "ELAPSED_TRIP_SWEEP=off" in proc.stdout
    assert "WARNING" not in proc.stdout


@pytest.mark.parametrize("seed", ["policy_corpus", "true"])
@pytest.mark.parametrize("value", ["yes", "true", "dryrun"])
def test_invalid_sweep_value_is_treated_as_off_with_a_warning(
    tmp_path: Path, seed: str, value: str
) -> None:
    proc, trace = _run(tmp_path, seed, extra_env={"ELAPSED_TRIP_SWEEP": value})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ran {COMPLETE}" not in trace
    assert trace[-1] == "ran app"
    assert "WARNING" in proc.stdout and "ELAPSED_TRIP_SWEEP" in proc.stdout
    # The other repairs are unaffected.
    if seed == "policy_corpus":
        assert f"ran {BACKFILL}" in trace and f"ran {REFRESH}" in trace


def test_sweep_off_has_no_effect_without_a_seed_mode(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "false", extra_env={"ELAPSED_TRIP_SWEEP": "on"})
    assert proc.returncode == 0
    assert trace == ["ran alembic", "ran app"]


def _cleanup_args(tmp_path: Path) -> list[str]:
    f = tmp_path / "trace.log.args"
    return [ln for ln in f.read_text().splitlines() if CLEANUP in ln] if f.exists() else []


def test_cleanup_is_off_by_default(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "policy_corpus")
    assert proc.returncode == 0
    assert f"ran {CLEANUP}" not in trace


def test_cleanup_dry_run_never_passes_apply(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "policy_corpus", extra_env={"TEST_DATA_CLEANUP": "dry-run"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ran {CLEANUP}" in trace and trace[-1] == "ran app"
    (line,) = _cleanup_args(tmp_path)
    assert "--org Atlas Demo Corp" in line and "--apply" not in line


def test_cleanup_apply_passes_apply_and_a_custom_org(tmp_path: Path) -> None:
    env = {"TEST_DATA_CLEANUP": " APPLY ", "TEST_DATA_CLEANUP_ORG": "Other Org"}
    proc, trace = _run(tmp_path, "policy_corpus", extra_env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (line,) = _cleanup_args(tmp_path)
    assert "--org Other Org" in line and line.rstrip().endswith("--apply")


@pytest.mark.parametrize("value", ["yes", "true", "dryrun", "apply-now"])
def test_invalid_cleanup_value_is_off_with_a_warning(tmp_path: Path, value: str) -> None:
    proc, trace = _run(tmp_path, "policy_corpus", extra_env={"TEST_DATA_CLEANUP": value})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ran {CLEANUP}" not in trace and trace[-1] == "ran app"
    assert "WARNING" in proc.stdout and "TEST_DATA_CLEANUP" in proc.stdout


def test_failed_cleanup_only_warns_and_the_app_starts(tmp_path: Path) -> None:
    proc, trace = _run(
        tmp_path, "policy_corpus", fail_modules=CLEANUP, extra_env={"TEST_DATA_CLEANUP": "apply"}
    )
    assert proc.returncode == 0 and trace[-1] == "ran app"
    assert "WARNING" in proc.stdout


def test_cleanup_does_not_run_without_a_seed_mode(tmp_path: Path) -> None:
    proc, trace = _run(tmp_path, "false", extra_env={"TEST_DATA_CLEANUP": "apply"})
    assert proc.returncode == 0
    assert trace == ["ran alembic", "ran app"]
