"""Phase 11 — scripts/ops/secret_scan.py finds secrets without printing them.

Hermetic: each case builds a throwaway git repository in tmp_path. Every fake
credential is assembled at runtime, so this file itself holds no secret-shaped
literal (the scanner runs on this repository in CI).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ps_ops_secret_scan", REPO_ROOT / "scripts" / "ops" / "secret_scan.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load()

# name -> a fake value of that shape (assembled, never a literal).
FAKES: dict[str, str] = {
    "openrouter-key": "sk-" + "or-v1-" + "0a1b2c3d" * 8,
    "openai-key": "sk-" + "proj-" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm",
    "anthropic-key": "sk-" + "ant-" + "api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2",
    "google-api-key": "AI" + "za" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "github-token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "github-pat": "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
    "aws-access-key-id": "AK" + "IA" + "Q3EGRZ7XN4KP2MTS",
    "render-api-key": "rn" + "d_" + "A1b2C3d4E5f6G7h8I9j0K1",
    "neon-api-key": "na" + "pi_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8",
    "neon-db-password": "np" + "g_" + "A1b2C3d4E5f6",
    "huggingface-token": "h" + "f_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3j",
}
PG_PASSWORD = "Np7" + "xQ2vL9kR4sW"
PG_URL = "postgresql://neondb_owner:" + PG_PASSWORD + "@ep-x.neon.tech/neondb"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
ASSIGNMENT = "MY_SERVICE_" + "API_KEY=" + "q8Zr2Lm5Wt9Xv3Np7Ks1Hd4Fj6Bc0Gy"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _empty_allowlist(tmp_path: Path) -> Path:
    path = tmp_path / "allow.txt"
    path.write_text("")
    return path


@pytest.mark.parametrize("name", sorted(FAKES))
def test_each_known_shape_is_found(name: str) -> None:
    hits = list(scan.scan_line(f'token = "{FAKES[name]}"'))
    assert any(pattern == name for pattern, _ in hits), hits


def test_postgres_url_private_key_and_assignment_are_found() -> None:
    assert ("postgres-url-password", PG_PASSWORD) in list(scan.scan_line(PG_URL))
    assert any(p == "private-key" for p, _ in scan.scan_line(PRIVATE_KEY))
    assert any(p == "high-entropy-assignment" for p, _ in scan.scan_line(ASSIGNMENT))


def test_placeholders_are_not_findings() -> None:
    for line in (
        "JWT_SECRET=change-me-use-a-64-char-random-hex-string",
        "DATABASE_URL=postgresql://atlas_user:${POSTGRES_PASSWORD}@postgres:5432/atlas",
        "LLM_API_KEY=your-llm-api-key-here",
        "title = 'Quarterly business review'",
    ):
        hits = list(scan.scan_line(line))
        allow = scan.Allowlist.load(scan.DEFAULT_ALLOWLIST)
        assert all(allow.allows(v, scan.fingerprint(p, v)) for p, v in hits), line


def test_tree_and_history_findings_never_print_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    secret = FAKES["openrouter-key"]
    # Committed, then deleted: only the history still has it.
    (root / "old.env.txt").write_text(f"LLM_API_KEY={secret}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "oops")
    (root / "old.env.txt").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "remove")
    # Untracked but not ignored: the tree scan sees it.
    (root / "notes.txt").write_text(f"db: {PG_URL}\n")
    # Ignored: never read.
    (root / ".gitignore").write_text(".env\n")
    (root / ".env").write_text(f"JWT_SECRET={FAKES['jwt']}\n")

    code = scan.main(["--root", str(root), "--allowlist", str(_empty_allowlist(tmp_path))])
    out = capsys.readouterr().out
    assert code == 1
    assert "HISTORY  commit" in out and "old.env.txt:1" in out and "[openrouter-key]" in out
    assert "TREE     notes.txt:1" in out and "[postgres-url-password]" in out
    assert ".env:" not in out.replace("old.env.txt", "")
    for value in (secret, PG_PASSWORD, FAKES["jwt"]):
        assert value not in out
    assert secret[:4] + "…" in out  # the masked prefix


def test_allowlist_value_and_fingerprint(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    secret = FAKES["github-token"]
    (root / "a.txt").write_text(f"x={secret}\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("")
    assert scan.main(["--root", str(root), "--allowlist", str(allow), "--no-history"]) == 1
    fp = scan.fingerprint("github-token", secret)
    allow.write_text(f"fingerprint:{fp}\n")
    assert scan.main(["--root", str(root), "--allowlist", str(allow), "--no-history"]) == 0
    # A value rule must match the WHOLE value, so a short rule cannot hide a key.
    allow.write_text("value:gh\n")
    assert scan.main(["--root", str(root), "--allowlist", str(allow), "--no-history"]) == 1


def test_clean_repo_exits_zero(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert scan.main(["--root", str(root), "--allowlist", str(_empty_allowlist(tmp_path))]) == 0


def test_this_repository_is_clean() -> None:
    """The working tree of this repository has no unallowed findings (history is
    scanned by the CI secret-scan job, which has the full clone)."""
    allow = scan.Allowlist.load(scan.DEFAULT_ALLOWLIST)
    findings = scan.scan_tree(REPO_ROOT, allow)
    assert findings == [], "\n".join(f.render() for f in findings)
