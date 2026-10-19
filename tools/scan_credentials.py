"""Layered, redacted repository credential review (stdlib + Git).

Scans the worktree, staged/index blobs, and every reachable Git commit tree.
This is a detection aid, not a guarantee that no unknown secret exists.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess


OSM_PATH = "data/shade_buildings_spb_pilot.sqlite"
SENSITIVE_NAME = re.compile(r"api[_-]?key|token|secret|password|private[_-]?key|credential", re.I)
SIGNATURES = {
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    "jwt-token": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "url-credential": re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9_.+/=-]{20,}", re.I),
}
ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?[\"']?([A-Za-z_][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*(.*?)\s*$"
)
ENTROPY_TOKEN = re.compile(r"[\"']([A-Za-z0-9_+/=-]{28,200})[\"']")


@dataclass(frozen=True)
class Finding:
    scope: str
    path: str
    line: int
    rule: str


class ScanError(RuntimeError):
    pass


def forbidden_path(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example") or name == "development.env":
        return "forbidden-env-path"
    if name.endswith((".keystore", ".jks", ".key", ".pem", ".p12", ".pfx")):
        return "forbidden-key-path"
    if re.search(r"\.(?:db|sqlite|sqlite3)(?:[-.].*)?$", name) and path != OSM_PATH:
        return "forbidden-database-path"
    return None


def _example(value: str) -> bool:
    return not value or value in {"***", "None", "null"} or value.upper().startswith(("YOUR_", "REPLACE_", "PLACEHOLDER")) or (
        value.startswith("${") and value.endswith("}")) or (value.startswith("<") and value.endswith(">"))


def _entropy(value: str) -> float:
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in Counter(value).values())


def _ast_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def load_allowlist(path: Path) -> set[tuple[str, str, str]]:
    if not path.is_file():
        return set()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
        result = set()
        for item in entries:
            # No wildcard exemptions, signatures or private keys can be bypassed.
            reviewed_fixture = (item["path"].startswith("tests/")
                                and item["rule"] in {"structured-value", "url-credential"})
            reviewed_public_id = (item["path"] == "data/scenic_features.json"
                                  and item["rule"] == "high-entropy-literal")
            if (not (reviewed_fixture or reviewed_public_id) or "*" in item["path"]
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or not item["reason"].strip()):
                raise ValueError()
            result.add((item["path"], item["rule"], item["sha256"]))
        return result
    except (KeyError, TypeError, ValueError):
        raise ScanError("Invalid credential scan allowlist") from None


def scan_content(path: str, content: bytes, scope: str, allowlist=()) -> list[Finding]:
    findings = set()
    rule = forbidden_path(path)
    if rule:
        findings.add(Finding(scope, path, 0, rule))
    if path == OSM_PATH:
        if not content.startswith(b"SQLite format 3\x00"):
            findings.add(Finding(scope, path, 0, "invalid-osm-sqlite-header"))
        # The exact reproducible read-only dataset is validated separately by
        # its SHA-256 manifest/OSM verification; scanning binary data is noisy.
        return sorted(findings, key=lambda f: (f.line, f.rule))
    text = content.decode("utf-8", errors="replace")

    def emit(line: int, rule: str, value: str = ""):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if (path, rule, digest) not in allowlist:
            findings.add(Finding(scope, path, line, rule))

    def structured(name: str, value: str | None, line: int):
        if name.lower() in {"persist-credentials", "allow-credentials", "allow_credentials"} and value in {"true", "false"}:
            return
        if value is not None and SENSITIVE_NAME.search(name) and not _example(value.strip()):
            emit(line, "structured-value", value.strip())

    def assignments(value: str, offset: int):
        for index, line in enumerate(value.splitlines(), offset):
            match = ASSIGNMENT.match(line)
            if match:
                raw = match[2].strip()
                if raw.startswith(('"', "'")):
                    closing = raw.find(raw[0], 1)
                    raw = raw[1:closing] if closing > 0 else raw[1:]
                else:
                    raw = raw.split(" #", 1)[0].strip()
                # Source code expressions are handled by AST, not treated as
                # credentials merely because they mention a sensitive name.
                if path.endswith(".py"):
                    continue
                structured(match[1], raw, index)

    assignments(text, 1)
    for rule, pattern in SIGNATURES.items():
        for match in pattern.finditer(text):
            emit(text.count("\n", 0, match.start()) + 1, rule, match[0])
    for match in ENTROPY_TOKEN.finditer(text):
        value = match[1]
        if (not re.fullmatch(r"[0-9a-fA-F]{32,128}", value)
                and not re.fullmatch(r"[0-9a-fA-F-]{36}", value)
                and _entropy(value) >= 4.4 and any(c.isdigit() for c in value)
                and any(c.isalpha() for c in value)):
            emit(text.count("\n", 0, match.start()) + 1, "high-entropy-literal", value)
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None

        def inspect_json(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    structured(str(key), child if isinstance(child, str) else None, 1)
                    inspect_json(child)
            elif isinstance(value, list):
                for child in value:
                    inspect_json(child)

        inspect_json(data)
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            emit(0, "python-parse-failed")
        else:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        structured(_ast_name(target), _literal(node.value), node.lineno)
                elif isinstance(node, ast.AnnAssign):
                    structured(_ast_name(node.target), _literal(node.value), node.lineno)
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        structured(_ast_name(key), _literal(value), node.lineno)
                elif isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        structured(keyword.arg or "", _literal(keyword.value), node.lineno)
                    method = _ast_name(node.func)
                    if method in {"setattr", "object"} and len(node.args) >= 3:
                        structured(_ast_name(node.args[1]), _literal(node.args[2]), node.lineno)
                    elif method in {"setenv"} and len(node.args) >= 2:
                        structured(_ast_name(node.args[0]), _literal(node.args[1]), node.lineno)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "\n" in node.value:
                    # Embedded dotenv/JSON test strings are also reviewed.
                    for offset, line in enumerate(node.value.splitlines()):
                        match = ASSIGNMENT.match(line)
                        if match:
                            structured(match[1], match[2].strip().strip("\"'"), node.lineno + offset)
    return sorted(findings, key=lambda f: (f.line, f.rule))


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input_bytes)
    if result.returncode:
        raise ScanError("Git repository inspection failed; no partial pass is reported")
    return result.stdout


def scan_repository(repo: Path, *, history: bool = True) -> dict:
    repo = repo.resolve()
    if _git(repo, "rev-parse", "--show-toplevel").decode().strip().replace("\\", "/").lower() != repo.as_posix().lower():
        raise ScanError("Scanner must run against the repository root")
    allowlist = load_allowlist(repo / "tools" / "credential_scan_allowlist.json")
    findings = []
    scanned = {"worktree_files": 0, "index_blobs": 0, "history_commits": 0, "history_blobs": 0}
    paths = set(_git(repo, "ls-files", "-z").split(b"\x00"))
    paths.update(_git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\x00"))
    for raw in sorted(paths - {b""}):
        path = raw.decode("utf-8", errors="replace")
        absolute = repo / path
        if absolute.is_symlink():
            findings.append(Finding("worktree", path, 0, "symlink-not-reviewed"))
        elif absolute.is_file():
            findings.extend(scan_content(path, absolute.read_bytes(), "worktree", allowlist))
            scanned["worktree_files"] += 1
    descriptors = []
    for entry in _git(repo, "ls-files", "--stage", "-z").split(b"\x00"):
        if entry:
            header, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            if stage != "0":
                raise ScanError("Unmerged index cannot receive a credential scan pass")
            descriptors.append((raw_path.decode("utf-8", errors="replace"), oid, "index", mode))
    if history:
        commits = _git(repo, "rev-list", "--all").decode().splitlines()
        scanned["history_commits"] = len(commits)
        history_pairs = set()
        for commit in commits:
            for entry in _git(repo, "ls-tree", "-r", "-z", commit).split(b"\x00"):
                if not entry:
                    continue
                header, raw_path = entry.split(b"\t", 1)
                mode, kind, oid = header.decode().split()
                if kind != "blob":
                    findings.append(Finding("commit:" + commit, raw_path.decode(), 0, "non-blob-not-reviewed"))
                else:
                    path = raw_path.decode("utf-8", errors="replace")
                    if (path, oid) not in history_pairs:
                        descriptors.append((path, oid, "commit:" + commit, mode))
                        history_pairs.add((path, oid))
    # One Git invocation for all unique objects avoids hundreds of subprocesses
    # on Windows and reviews historical blobs even when they match the index.
    identifiers = sorted({oid for _path, oid, _scope, mode in descriptors if mode != "120000"})
    contents = {}
    if identifiers:
        raw = _git(repo, "cat-file", "--batch", input_bytes=("\n".join(identifiers) + "\n").encode())
        offset = 0
        for oid in identifiers:
            newline = raw.find(b"\n", offset)
            header = raw[offset:newline].decode().split()
            if len(header) != 3 or header[0] != oid or header[1] != "blob":
                raise ScanError("Git blob review could not complete")
            size = int(header[2])
            start = newline + 1
            contents[oid] = raw[start:start + size]
            offset = start + size + 1
    for path, oid, scope, mode in descriptors:
        if mode == "120000":
            findings.append(Finding(scope, path, 0, "symlink-not-reviewed"))
        else:
            findings.extend(scan_content(path, contents[oid], scope, allowlist))
            scanned["history_blobs" if scope.startswith("commit:") else "index_blobs"] += 1
    return {"status": "fail" if findings else "pass", "coverage": scanned,
            "findings": [asdict(finding) for finding in findings],
            "limitations": "Heuristic review; unknown, encoded, split, binary or external secrets may evade detection. Ignored/unreachable files are outside scope."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--no-history", action="store_true", help="Explicitly limit review to worktree and index")
    args = parser.parse_args()
    try:
        result = scan_repository(args.repo, history=not args.no_history)
        print(json.dumps(result, indent=2))
        return 1 if result["findings"] else 0
    except (ScanError, OSError):
        print(json.dumps({"status": "error", "reason": "Repository credential scan could not complete"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
