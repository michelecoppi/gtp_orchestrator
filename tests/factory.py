"""Costruttori di fixture GitHub e Promo per i test: nessuna chiamata di rete."""
from __future__ import annotations

from supervisor.collectors.github import API
from supervisor.collectors.http import fixture_key
from supervisor.core.config import GitHubRepo, PromoConfig, Sources

GAME = "michelecoppi/guess_the_player_from_the_path"
PROMO = "michelecoppi/promo_studio"
NOW = "2026-09-28T08:00:00Z"
SHA_MAIN = "a" * 40
SHA_PR = "b" * 40


def sources(*repos: GitHubRepo) -> Sources:
    return Sources(github=repos or (game_repo(),), promo=PromoConfig())


def game_repo() -> GitHubRepo:
    return GitHubRepo(GAME, ci_workflow="ci.yml", deploy_workflow="deploy.yml", workflows=("ci.yml", "deploy.yml"))


def run(run_id: int, conclusion: str = "success", branch: str = "main", sha: str = SHA_MAIN,
        status: str = "completed", workflow: str = "ci.yml", updated: str = "2026-09-28T07:00:00Z",
        attempt: int = 1) -> dict:
    return {
        "id": run_id, "status": status, "conclusion": conclusion if status == "completed" else None,
        "head_branch": branch, "head_sha": sha, "event": "push", "run_number": run_id,
        "run_attempt": attempt, "updated_at": updated, "path": f".github/workflows/{workflow}",
        "html_url": f"https://github.com/{GAME}/actions/runs/{run_id}",
    }


def issue(number: int, state: str = "open", pr: bool = False, merged: bool = False,
          title: str = "Titolo", updated: str = "2026-09-27T10:00:00Z") -> dict:
    item = {"number": number, "state": state, "title": title, "updated_at": updated,
            "html_url": f"https://github.com/{GAME}/issues/{number}", "user": {"login": "michelecoppi"},
            "labels": [{"name": "bug"}]}
    if pr:
        item["pull_request"] = {"merged_at": "2026-09-27T10:00:00Z" if merged else None}
    return item


def pull(number: int, sha: str = SHA_PR, draft: bool = False, created: str = "2026-09-27T09:00:00Z",
         title: str = "feat: qualcosa") -> dict:
    return {"number": number, "title": title, "draft": draft, "head": {"sha": sha}, "created_at": created,
            "html_url": f"https://github.com/{GAME}/pull/{number}", "user": {"login": "michelecoppi"}}


def github_fixtures(repo: str = GAME, default_branch: str = "main", head_sha: str = SHA_MAIN,
                    runs: dict[str, list[dict]] | None = None, issues: list[dict] | None = None,
                    pulls: list[dict] | None = None, sha_runs: dict[str, list[dict]] | None = None,
                    open_issues_count: int = 5) -> dict:
    def key(path: str, params: dict | None = None) -> str:
        return fixture_key("GET", API + path, params)

    out = {
        key(f"/repos/{repo}"): {"body": {
            "default_branch": default_branch, "private": False, "archived": False,
            "pushed_at": "2026-09-28T07:00:00Z", "open_issues_count": open_issues_count,
            "html_url": f"https://github.com/{repo}"}},
        key(f"/repos/{repo}/branches/{default_branch}"): {"body": {"commit": {
            "sha": head_sha, "commit": {"message": "feat: ultimo commit\n\ncorpo",
                                        "committer": {"date": "2026-09-28T06:00:00Z"}}}}},
        key(f"/repos/{repo}/issues"): {"body": issues or []},
        key(f"/repos/{repo}/pulls", {"state": "open", "per_page": 50, "page": 1}): {"body": pulls or []},
    }
    for workflow, items in (runs or {"ci.yml": [run(100)], "deploy.yml": [run(200, workflow="deploy.yml")]}).items():
        out[key(f"/repos/{repo}/actions/workflows/{workflow}/runs", {"per_page": 30, "page": 1})] = {
            "body": {"workflow_runs": items}, "headers": {"ETag": f'W/"{workflow}-{len(items)}-{items[0]["id"] if items else 0}"'},
        }
    sha_runs = {**{p["head"]["sha"]: [] for p in pulls or []}, **(sha_runs or {})}
    for sha, items in sha_runs.items():
        out[key(f"/repos/{repo}/actions/runs", {"head_sha": sha, "per_page": 20})] = {
            "body": {"workflow_runs": items}}
    return out


def post(post_id: str, status: str = "draft", created: str = "2026-09-26T07:00:00Z", **extra) -> dict:
    """Un documento di `promo_posts` conforme a tests/contracts/promo_post.v1.json (come lo scrive Promo)."""
    day = created[:10]
    doc: dict = {
        "id": post_id, "status": status, "format": "who_is", "language": "it", "channel": "telegram_channel",
        "created_at": created, "created_for": day, "scheduled_for": f"{day}T10:00:00Z", "published_at": None,
        "external_url": None, "error": "", "attempts": 0,
        "history": [{"from": None, "to": "draft", "by": "scheduler", "at": created}],
    }
    if status != "draft":
        doc.update(approved_at=f"{day}T08:00:00Z", approved_by="michele")
        doc["history"].append({"from": "draft", "to": "approved", "by": "michele", "at": f"{day}T08:00:00Z"})
    if status == "rejected":
        doc.update(rejected_at=f"{day}T08:30:00Z", rejected_by="michele")
    elif status == "published":
        doc.update(published_at=f"{day}T10:00:00Z", external_url=f"https://t.me/gtp_promo/{post_id}")
    elif status == "failed":
        doc.update(error="HTTP 500", attempts=1)
    if status in ("rejected", "published", "failed"):
        by = "michele" if status == "rejected" else "publisher"
        at = doc.get("rejected_at") or f"{day}T10:00:00Z"
        doc["history"].append({"from": "approved", "to": status, "by": by, "at": at})
    doc.update(extra)
    return doc
