import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from tools.scan_credentials import Finding, ScanError, forbidden_path, load_allowlist, scan_content, scan_repository


def test_structured_json_env_and_python_values_are_reviewed_and_redacted():
    sample = "synthetic-detection-fixture"
    sources = {
        "settings.json": json.dumps({"nested": {"SUGGEST_API_KEY": sample}}),
        "settings.txt": "GEOCODER_API_KEY=" + sample,
        "settings.py": "credential = " + repr(sample),
        "patch.py": "monkeypatch.setenv('GEOCODER_API_KEY', " + repr(sample) + ")",
    }
    for path, text in sources.items():
        findings = scan_content(path, text.encode(), "test")
        assert any(f.rule == "structured-value" for f in findings)
        assert sample not in repr(findings)
    assert scan_content(".env.example", b"GEOCODER_API_KEY=\nSUGGEST_API_KEY=YOUR_KEY\n", "test") == []
    assert scan_content("workflow.yml", b"persist-credentials: false\n", "test") == []
    assert scan_content("workflow.yml", b"persist-credentials: unexpected-value\n", "test")


def test_signature_private_key_and_entropy_are_independent_detection_layers():
    sample = "ghp_" + "A9" * 18
    assert any(f.rule == "github-token" for f in scan_content("code.txt", sample.encode(), "test"))
    sample = "-----BEGIN " + "PRIVATE KEY-----"
    assert any(f.rule == "private-key" for f in scan_content("code.txt", sample.encode(), "test"))
    sample = "aZ8+" + "cD4/eF1=gH2_iJ3+" + "kL5/mN6=oP7_qR9"
    assert any(f.rule == "high-entropy-literal" for f in scan_content("code.txt", repr(sample).encode(), "test"))
    # A checksum is not a generic entropy finding; sensitive named hex values
    # are still reviewed by the structured detector.
    checksum = hashlib.sha256(b"public-content").hexdigest()
    assert scan_content("manifest.json", json.dumps({"sha256": checksum}).encode(), "test") == []
    assert any(f.rule == "structured-value" for f in scan_content(
        "settings.json", json.dumps({"API_KEY": checksum}).encode(), "test"))


@pytest.mark.parametrize("path", [".env", "app/.env.dev", "development.env", "debug.keystore",
                                   "release.jks", "private.pem", "main.db", "data/other.sqlite",
                                   "data/shade_buildings_spb_pilot.sqlite-wal"])
def test_forbidden_paths_and_exact_dataset_exception(path):
    assert forbidden_path(path)
    assert forbidden_path(".env.example") is None
    assert forbidden_path("data/shade_buildings_spb_pilot.sqlite") is None
    assert forbidden_path("other/shade_buildings_spb_pilot.sqlite")
    assert scan_content("data/shade_buildings_spb_pilot.sqlite", b"wrong-content", "test")[0].rule == "invalid-osm-sqlite-header"


def git(repo, *args):
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    assert completed.returncode == 0, "Git test fixture setup failed"
    return completed.stdout


def test_deleted_historical_credentials_and_staged_only_content_are_detected(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Scanner fixture")
    sample = "ghp_" + "Q8" * 18
    (tmp_path / "old.txt").write_text(sample)
    git(tmp_path, "add", "old.txt")
    git(tmp_path, "commit", "-m", "Synthetic detector fixture")
    (tmp_path / "old.txt").unlink()
    git(tmp_path, "add", "-u")
    git(tmp_path, "commit", "-m", "Delete detector fixture")
    (tmp_path / "next.txt").write_text("API_KEY=" + "staged-fixture-only")
    git(tmp_path, "add", "next.txt")
    (tmp_path / "next.txt").write_text("safe worktree")
    result = scan_repository(tmp_path)
    assert result["status"] == "fail" and result["coverage"]["history_commits"] == 2
    assert any(f["scope"].startswith("commit:") and f["rule"] == "github-token" for f in result["findings"])
    assert any(f["scope"] == "index" and f["rule"] == "structured-value" for f in result["findings"])
    assert sample not in json.dumps(result) and "staged-fixture-only" not in json.dumps(result)


def test_allowlist_is_exact_reviewed_path_rule_and_hash_only(tmp_path):
    sample = "reviewed-test-value"
    digest = hashlib.sha256(sample.encode()).hexdigest()
    path = "tests/test_fixture.py"
    content = ("secret = " + repr(sample)).encode()
    assert scan_content(path, content, "test", {(path, "structured-value", digest)}) == []
    assert scan_content("app/test_fixture.py", content, "test", {(path, "structured-value", digest)})
    assert scan_content(path, b"secret = 'changed-value'", "test", {(path, "structured-value", digest)})
    file = tmp_path / "allowlist.json"
    file.write_text(json.dumps({"entries": [{"path": "tests/*", "rule": "structured-value",
                                           "sha256": digest, "reason": "Invalid blanket exemption"}]}))
    with pytest.raises(ScanError):
        load_allowlist(file)
