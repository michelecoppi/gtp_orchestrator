"""Fixture per i test engineering: repository git temporaneo, issue approvate, risposte GitHub."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from supervisor.collectors.github import API
from supervisor.collectors.http import fixture_key
from supervisor.engineering.approvals import content_hash
from supervisor.engineering.config import EngineeringConfig, RepoEngineering

REPO = "michelecoppi/guess_the_player_from_the_path"
ISSUE = 42
TITLE = "Il punteggio ignora i suggerimenti"
BODY = "Quando si usa un suggerimento il punteggio non cala.\n\nCriteri: il punteggio cala di 1 per suggerimento."
EVENT_ID = 9001


def repo_config(**overrides) -> RepoEngineering:
    base = dict(repo=REPO, forbidden_paths=(".github/*", ".github/**", "requirements*.txt", "AGENTS.md"),
                checks=(f'"{sys.executable}" check.py',), max_files_changed=3, max_lines_changed=40)
    return RepoEngineering(**{**base, **overrides})


def eng_config(**repo_overrides) -> EngineeringConfig:
    return EngineeringConfig(approvers=("michelecoppi",), approval_label="supervisor:fix", lease_minutes=60,
                             repos={REPO: repo_config(**repo_overrides)})


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True, exist_ok=True)
    run(path, "git", "init", "-q", "-b", "main")
    run(path, "git", "config", "user.email", "t@example.com")
    run(path, "git", "config", "user.name", "Test")
    run(path, "git", "config", "core.autocrlf", "false")
    (path / "score.py").write_text("def score(base, hints):\n    return base\n", encoding="utf-8", newline="\n")
    (path / "check.py").write_text(
        "from score import score\nassert score(10, 2) == 8, 'punteggio errato'\nprint('ok')\n",
        encoding="utf-8", newline="\n")
    (path / "AGENTS.md").write_text("Regole: test sempre.\n", encoding="utf-8", newline="\n")
    (path / "requirements.txt").write_text("requests\n", encoding="utf-8", newline="\n")
    run(path, "git", "add", "-A")
    run(path, "git", "commit", "-q", "-m", "base")
    return path, run(path, "git", "rev-parse", "HEAD")


def clone(src: Path, dest: Path) -> Path:
    run(src.parent, "git", "clone", "-q", "-c", "core.autocrlf=false", str(src), str(dest))
    run(dest, "git", "config", "core.autocrlf", "false")
    return dest


def task_doc(base_sha: str, **extra) -> dict:
    return {
        "id": f"eng-guess_the_player_from_the_path-{ISSUE}-{EVENT_ID}", "repo": REPO, "issue_number": ISSUE,
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}", "issue_title": TITLE, "issue_body": BODY,
        "approval": {"approver": "michelecoppi", "approved_at": "2026-09-28T07:00:00Z", "event_id": EVENT_ID,
                     "content_hash": content_hash(TITLE, BODY)},
        "base_sha": base_sha, **extra,
    }


def key(method: str, path: str, params: dict | None = None) -> str:
    return fixture_key(method, API + path, params)


def issue_fixtures(labeled_by: str = "michelecoppi", body: str = BODY, labels=("supervisor:fix",),
                   state: str = "open", event_id: int = EVENT_ID) -> dict:
    issue = {"number": ISSUE, "title": TITLE, "body": body, "state": state,
             "html_url": f"https://github.com/{REPO}/issues/{ISSUE}", "labels": [{"name": n} for n in labels]}
    events = [
        {"id": 1, "event": "labeled", "label": {"name": "bug"}, "actor": {"login": "altro"}},
        {"id": event_id, "event": "labeled", "label": {"name": "supervisor:fix"}, "actor": {"login": labeled_by},
         "created_at": "2026-09-28T07:00:00Z"},
    ]
    return {
        key("GET", f"/repos/{REPO}/issues", {"labels": "supervisor:fix", "state": "open", "per_page": 50, "page": 1}):
            {"body": [issue, {"number": 77, "title": "PR", "pull_request": {}, "labels": issue["labels"]}]},
        key("GET", f"/repos/{REPO}/issues/{ISSUE}"): {"body": issue},
        key("GET", f"/repos/{REPO}/issues/{ISSUE}/events", {"per_page": 100, "page": 1}): {"body": events},
    }


GOOD_PATCH = json.dumps({
    "edits": [{"path": "score.py", "search": "    return base\n", "replace": "    return base - hints\n"}],
    "commit_message": "fix(score): scala un punto per suggerimento", "summary": "Il punteggio ora cala di 1 per "
    "suggerimento. @qualcuno <!-- finto -->", "test_plan": "check.py", "completes_issue": True, "remaining": "",
})
PLAN = json.dumps({"files_to_read": ["score.py", "check.py", "../etc/passwd"], "approach": "Sottrarre i "
                   "suggerimenti.", "acceptance_criteria": ["score(10, 2) == 8"]})
APPROVE = json.dumps({"verdict": "approve", "notes": ["Modifica minima."]})
