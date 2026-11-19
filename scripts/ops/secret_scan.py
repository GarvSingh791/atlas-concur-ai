#!/usr/bin/env python3
"""Secret scanner for the working tree AND the full git history (phase 11).

Pure standard library (gitleaks is not a dependency). It looks for known
credential shapes — OpenRouter / OpenAI / Anthropic / Google / Hugging Face /
Novita keys, AWS / R2 access keys and secrets, JWTs, Postgres URLs with an
embedded password, private keys, GitHub / Render / Neon / Vercel tokens, the
Render deploy-hook URL — plus high-entropy values assigned to a secret-named
variable.

It NEVER prints a secret: each finding shows the file, line (or commit + file +
line for history), the pattern name, a 4-character prefix of the match and its
length, and a stable fingerprint (sha256 prefix) that can be allow-listed.

Allow-list (``scripts/ops/secret_scan_allowlist.txt`` by default), one rule per
line, ``#`` comments:
    value:<regex>        ignore a match whose WHOLE value matches (e.g. change-me.*)
    path:<regex>         ignore every match in a matching file path
    fingerprint:<hex>    ignore exactly one known finding (sha256 prefix)

Scope: tracked files plus untracked, NOT git-ignored files (so a local .env is
never read), and every line ever ADDED in any commit reachable from any ref
(``git log --all -p``). Binary files are skipped.

Exit status: 0 = clean, 1 = findings, 2 = usage / git error.

Usage: python scripts/ops/secret_scan.py [--root PATH] [--allowlist FILE]
                                        [--no-history] [--no-tree]
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# (name, regex). The secret is group "s" when present, else the whole match.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    ),
    ("openrouter-key", re.compile(r"(?P<s>sk-or-v1-[0-9a-f]{32,})")),
    ("anthropic-key", re.compile(r"(?P<s>sk-ant-[A-Za-z0-9_-]{20,})")),
    ("openai-key", re.compile(r"(?P<s>\bsk-(?:proj-|svcacct-)?(?!or-|ant-)[A-Za-z0-9_-]{32,})")),
    ("google-api-key", re.compile(r"(?P<s>\bAIza[0-9A-Za-z_-]{35})")),
    ("huggingface-token", re.compile(r"(?P<s>\bhf_[A-Za-z0-9]{30,})")),
    ("github-token", re.compile(r"(?P<s>\bgh[pousr]_[A-Za-z0-9]{36,})")),
    ("github-pat", re.compile(r"(?P<s>\bgithub_pat_[A-Za-z0-9_]{22,})")),
    ("aws-access-key-id", re.compile(r"(?P<s>\b(?:AKIA|ASIA)[0-9A-Z]{16}\b)")),
    ("render-api-key", re.compile(r"(?P<s>\brnd_[A-Za-z0-9]{20,})")),
    (
        "render-deploy-hook",
        re.compile(r"(?P<s>api\.render\.com/deploy/srv-[a-z0-9]+\?key=[A-Za-z0-9_-]{6,})"),
    ),
    ("neon-api-key", re.compile(r"(?P<s>\bnapi_[A-Za-z0-9]{30,})")),
    ("neon-db-password", re.compile(r"(?P<s>\bnpg_[A-Za-z0-9]{10,})")),
    (
        "jwt",
        re.compile(r"(?P<s>\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
    ),
    (
        "postgres-url-password",
        re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s/@'\"]+:(?P<s>[^@\s'\"]+)@[^\s/'\"]+"),
    ),
    (
        "r2-or-s3-secret",
        re.compile(
            r"(?i)(?:secret_access_key|aws_secret|r2_secret)[A-Z_]*[\"']?\s*[=:]\s*[\"']?"
            r"(?P<s>[A-Za-z0-9/+]{40,64})\b"
        ),
    ),
    (
        "r2-access-key-id",
        re.compile(r"(?i)access_key_id[\"']?\s*[=:]\s*[\"']?(?P<s>[0-9a-f]{32})\b"),
    ),
    (
        "vercel-token",
        re.compile(r"(?i)vercel[_-]?token[\"']?\s*[=:]\s*[\"']?(?P<s>[A-Za-z0-9]{24,})"),
    ),
)

# A secret-named variable assigned a long, random-looking value.
_ASSIGNMENT = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|TOKEN|PRIVATE_KEY)[A-Z0-9_]*"
    r"[\"']?\s*[=:]\s*[\"']?(?P<s>[A-Za-z0-9/+_\-.=]{24,})"
)
_ENTROPY_MIN = 3.6

_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff", ".woff2", ".ttf",
    ".zip", ".gz", ".mp4", ".webm", ".lock", ".svg",
)  # fmt: skip
_SKIP_FILES = ("package-lock.json",)

DEFAULT_ALLOWLIST = Path(__file__).resolve().with_name("secret_scan_allowlist.txt")


@dataclass(frozen=True)
class Finding:
    where: str  # "path:line" or "commit <sha> path:line"
    pattern: str
    masked: str
    fingerprint: str

    def render(self) -> str:
        return f"{self.where}  [{self.pattern}]  {self.masked}  fp={self.fingerprint}"


@dataclass
class Allowlist:
    values: list[re.Pattern[str]]
    paths: list[re.Pattern[str]]
    fingerprints: set[str]

    @classmethod
    def load(cls, path: Path | None) -> Allowlist:
        values: list[re.Pattern[str]] = []
        paths: list[re.Pattern[str]] = []
        fps: set[str] = set()
        if path is not None and path.exists():
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                kind, _, rule = line.partition(":")
                if kind == "value":
                    values.append(re.compile(rule))
                elif kind == "path":
                    paths.append(re.compile(rule))
                elif kind == "fingerprint":
                    fps.add(rule.strip().lower())
        return cls(values, paths, fps)

    def path_ignored(self, path: str) -> bool:
        return any(p.search(path) for p in self.paths)

    def allows(self, value: str, fingerprint: str) -> bool:
        if fingerprint in self.fingerprints:
            return True
        return any(v.fullmatch(value) for v in self.values)


def entropy(value: str) -> float:
    counts = {c: value.count(c) for c in set(value)}
    n = len(value)
    return -sum(k / n * math.log2(k / n) for k in counts.values())


def mask(value: str) -> str:
    return f"{value[:4]}…(len {len(value)})"


def fingerprint(pattern: str, value: str) -> str:
    return hashlib.sha256(f"{pattern}\0{value}".encode()).hexdigest()[:16]


def scan_line(line: str) -> Iterator[tuple[str, str]]:
    """Yield (pattern name, secret value) for every hit on one line."""
    seen: set[str] = set()
    for name, rx in PATTERNS:
        for m in rx.finditer(line):
            value = m.groupdict().get("s") or m.group(0)
            if value in seen:
                continue
            seen.add(value)
            yield name, value
    for m in _ASSIGNMENT.finditer(line):
        value = m.group("s")
        if value in seen or entropy(value) < _ENTROPY_MIN:
            continue
        if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
            continue  # identifiers / prose, not keys
        seen.add(value)
        yield "high-entropy-assignment", value


def _skip_path(path: str) -> bool:
    low = path.lower()
    return (
        low.endswith(_SKIP_SUFFIXES)
        or low.rsplit("/", 1)[-1] in _SKIP_FILES
        or ("node_modules/" in low)
    )


def _findings_for(
    where_prefix: str, path: str, lines: Iterable[tuple[int, str]], allow: Allowlist
) -> Iterator[Finding]:
    if _skip_path(path) or allow.path_ignored(path):
        return
    for lineno, line in lines:
        for name, value in scan_line(line):
            fp = fingerprint(name, value)
            if allow.allows(value, fp):
                continue
            yield Finding(f"{where_prefix}{path}:{lineno}", name, mask(value), fp)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def scan_tree(root: Path, allow: Allowlist) -> list[Finding]:
    files = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    findings: list[Finding] = []
    for rel in sorted({f for f in files.split("\0") if f}):
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        findings.extend(_findings_for("", rel, enumerate(text.splitlines(), 1), allow))
    return findings


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(root: Path, allow: Allowlist) -> list[Finding]:
    """Every line ever added, in every commit reachable from any ref."""
    proc = subprocess.Popen(
        [
            "git", "-C", str(root), "log", "--all", "-p", "-U0", "--no-color",
            "--no-ext-diff", "--format=@@COMMIT %H",
        ],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    assert proc.stdout is not None
    findings: dict[tuple[str, str], Finding] = {}
    commit = path = ""
    lineno = 0
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith("@@COMMIT "):
            commit, path = line.split()[1][:10], ""
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            for f in _findings_for(f"commit {commit} ", path, [(lineno, line[1:])], allow):
                # One report per (fingerprint, path): the oldest commit wins below.
                findings[(f.fingerprint, path)] = f
            lineno += 1
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(proc.returncode, "git log")
    return sorted(findings.values(), key=lambda f: f.where)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan the tree and git history for secrets.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--no-tree", action="store_true")
    args = parser.parse_args(argv)
    allow = Allowlist.load(args.allowlist)
    try:
        tree = [] if args.no_tree else scan_tree(args.root, allow)
        history = [] if args.no_history else scan_history(args.root, allow)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"secret scan: git failed ({exc.__class__.__name__})", file=sys.stderr)
        return 2
    for f in tree:
        print(f"TREE     {f.render()}")
    for f in history:
        print(f"HISTORY  {f.render()}")
    print(f"secret scan: {len(tree)} working-tree finding(s), {len(history)} history finding(s)")
    return 1 if tree or history else 0


if __name__ == "__main__":
    sys.exit(main())
