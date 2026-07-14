"""Egress content guard — keep everything Mission Control pushes secret-free.

Every commit we push to a remote (plan docs, unit output, the greenfield bootstrap
seed) is shared across teams, so it must be metadata/spec only. This scans the STAGED
diff about to be committed for high-signal secret shapes and obvious PII, and BLOCKS the
commit on a finding — never auto-redacting, never pushing anyway. It is overridable only
by an explicit operator ack (recorded for audit); the default is block.

Scope (load-bearing): it scans ONLY the content headed for the remote — the staged
index of the repo being committed — never the operator's ambient environment or unrelated
repo history. Repo-agnostic: no allowlists tied to any account/host. It favors PRECISION
over recall: loud on clear secrets (keys, tokens, credentialed URLs, ``SECRET=`` style
assignments), quiet on ordinary prose.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class Finding:
    """One secret/PII hit, named enough for an operator to act — the raw secret is
    never reproduced (only a short masked excerpt)."""

    file: str
    rule: str
    line: int
    excerpt: str


class GuardViolation(RuntimeError):
    """Raised when staged content headed for the remote contains a secret/PII. Carries
    the findings (file + rule) so the caller can surface a distinct blocked state; the
    commit/push does NOT happen."""

    def __init__(self, findings) -> None:
        self.findings = list(findings)
        super().__init__(summarize(self.findings))


# High-signal rules — precision over recall. Each is a (rule-name, compiled-pattern).
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("private-key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws-secret-access-key",
     re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("gcp-service-account-key", re.compile(r'"type"\s*:\s*"service_account"')),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    # scheme://user:password@host — a connection string with an embedded password.
    ("connection-string-password",
     re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]{3,}@[^\s/]+")),
    # .env-style assignment of a sensitive key to a non-trivial value.
    ("secret-assignment",
     re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
                r"private[_-]?key|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}")),
    # Obvious PII: US SSN. (Kept deliberately narrow — precision over recall.)
    ("pii-ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)

# A credentialed URL whose "password" is a well-known placeholder isn't a real leak; and
# our own remote helpers never write URLs into content. This keeps precision high without
# tying to any account/host.
_PLACEHOLDER = re.compile(r"(?i)\b(?:example|changeme|your[_-]?|placeholder|xx+|redacted|dummy)")


def _looks_binary(text: str) -> bool:
    return "\x00" in text


def _excerpt(rule: str, line_text: str, match: re.Match) -> str:
    """A short, MASKED excerpt — enough to locate, never the secret itself."""
    token = match.group(0)
    head = token[:4]
    return f"{rule}: {head}{'*' * min(6, max(1, len(token) - 4))}"


def scan_text(content: str, *, file: str = "") -> list:
    """Scan a blob of text for secret/PII shapes; return :class:`Finding`s. The raw
    secret is never stored on the finding. Binary content is skipped."""
    if not content or _looks_binary(content):
        return []
    findings: list = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if len(line) > 4000:  # skip pathological single-line blobs (minified vendor, etc.)
            continue
        for rule, pattern in _RULES:
            m = pattern.search(line)
            if not m:
                continue
            if rule == "connection-string-password" and _PLACEHOLDER.search(m.group(0)):
                continue  # obvious placeholder, not a real credential
            findings.append(Finding(file=file, rule=rule, line=lineno,
                                    excerpt=_excerpt(rule, line, m)))
    return findings


def _staged_files(repo: Path, pathspec: Optional[str]) -> list:
    """Added/copied/modified files in the index (optionally under ``pathspec``)."""
    args = ["git", "-C", str(repo), "diff", "--cached", "--name-only",
            "--diff-filter=ACM"]
    if pathspec:
        args += ["--", pathspec]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f.strip()]


def scan_staged(repo, *, pathspec: Optional[str] = None) -> list:
    """Scan the STAGED (index) version of the changed files about to be committed — the
    exact content headed for the remote, and nothing else. Returns :class:`Finding`s."""
    repo = Path(repo)
    findings: list = []
    for rel in _staged_files(repo, pathspec):
        shown = subprocess.run(["git", "-C", str(repo), "show", f":{rel}"],
                               capture_output=True, text=True)
        if shown.returncode != 0:
            continue
        findings.extend(scan_text(shown.stdout, file=rel))
    return findings


def enforce_staged(
    repo,
    *,
    pathspec: Optional[str] = None,
    allow: bool = False,
    audit: Optional[Callable[[list], None]] = None,
) -> list:
    """The egress boundary check: scan the staged changes and BLOCK unless clean.

    Raises :class:`GuardViolation` (naming file + rule) on a finding, so the commit does
    not happen — unless ``allow`` (an explicit operator override), in which case the
    findings are passed to ``audit`` (recorded for audit) and the commit proceeds.
    Returns the findings (empty when clean)."""
    findings = scan_staged(repo, pathspec=pathspec)
    if findings and not allow:
        raise GuardViolation(findings)
    if findings and audit is not None:
        audit(findings)
    return findings


def summarize(findings) -> str:
    """A short, operator-facing summary: which rule fired in which file (no secrets)."""
    if not findings:
        return "no findings"
    parts = [f"{f.file or '?'}:{f.line} [{f.rule}]" for f in findings[:10]]
    more = "" if len(findings) <= 10 else f" (+{len(findings) - 10} more)"
    return "content guard blocked egress — " + "; ".join(parts) + more
