"""Collector GitHub, in sola lettura (REST v3).

Per ogni repository osservato produce quattro flussi, ciascuno con il proprio cursore:
- `#repo`: metadati e SHA di testa del branch di default (evento `head` a ogni nuovo commit);
- `#runs:<workflow>`: run concluse del workflow (evento `workflow_run`), con ETag: se GitHub
  risponde 304 non si consuma quota e i fatti si prendono dal cursore;
- `#issues`: issue e PR aggiornate dopo il cursore (`since`), evento a ogni cambio di stato;
- `#pulls`: PR aperte e stato della CI sul loro SHA di testa (solo fatti).

Un flusso che fallisce non avanza il cursore e rende la sorgente "incompleta"; gli altri flussi
proseguono. Titoli e testi sono dati non fidati: si salvano troncati e non si interpretano mai.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from supervisor.collectors.http import HttpClient, HttpResponse
from supervisor.core.clock import iso
from supervisor.core.config import GitHubRepo
from supervisor.core.models import CollectResult, Event, SourceReport, StreamResult
from supervisor.core.scrub import scrub, untrusted

API = "https://api.github.com"
INITIAL_LOOKBACK = timedelta(days=7)
MAX_ISSUE_PAGES = 5
RUNS_PER_PAGE = 30


class GitHubError(RuntimeError):
    pass


class GitHubApi:
    def __init__(self, http: HttpClient, token: str = "") -> None:
        self.http = http
        self.token = token

    def get(self, path: str, params: Optional[dict] = None, etag: Optional[str] = None) -> HttpResponse:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self.http.request("GET", API + path, params=params, headers=headers)
        except Exception as exc:  # errore di rete: diventa un flusso fallito, non un crash
            raise GitHubError(scrub(f"{path}: {type(exc).__name__}: {exc}")) from exc
        if response.status == 304 or response.status < 400:
            return response
        detail = response.body.get("message", "") if isinstance(response.body, dict) else ""
        if response.status in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            detail = "rate limit esaurito"
        raise GitHubError(scrub(f"{path}: HTTP {response.status} {untrusted(detail, 80)}".strip()))


class GitHubCollector:
    kind = "github"

    def __init__(self, repo: GitHubRepo, api: GitHubApi) -> None:
        self.repo = repo
        self.api = api
        self.source = repo.source

    def stream(self, part: str) -> str:
        return f"{self.source}#{part}"

    def collect(self, cursors: dict[str, Optional[dict]], now: datetime) -> CollectResult:
        facts: dict[str, Any] = {}
        streams: list[StreamResult] = []
        received = iso(now)

        repo_stream = self._guard(self.stream("repo"), lambda: self._repo(facts, received))
        streams.append(repo_stream)
        branch = facts.get("default_branch") or self.repo.expected_default_branch

        workflows: dict[str, Any] = {}
        ci_by_sha: dict[str, str] = {}
        for workflow in self.repo.workflows:
            name = self.stream(f"runs:{workflow}")
            streams.append(self._guard(name, lambda w=workflow, n=name: self._runs(
                w, branch, cursors.get(n), workflows, ci_by_sha, received)))

        issues = self.stream("issues")
        streams.append(self._guard(issues, lambda: self._issues(cursors.get(issues), now, received)))
        streams.append(self._guard(self.stream("pulls"), lambda: self._pulls(facts, ci_by_sha)))

        if workflows:
            facts["workflows"] = workflows
        if "open_issues_and_prs" in facts and "open_prs" in facts:
            facts["open_issues"] = max(0, facts["open_issues_and_prs"] - len(facts["open_prs"]))
        errors = [f"{s.stream.split('#', 1)[1]}: {s.error}" for s in streams if not s.ok]
        if facts:  # l'identita' si aggiunge solo se c'e' almeno un fatto: vuoto = "non disponibile"
            facts.update(repo=self.repo.repo, expected_default_branch=self.repo.expected_default_branch)
        report = SourceReport(self.source, self.kind, ok=not errors, facts=facts, errors=errors)
        return CollectResult(streams, report)

    @staticmethod
    def _guard(name: str, fn) -> StreamResult:
        try:
            events, cursor = fn()
            return StreamResult(name, events, cursor)
        except GitHubError as exc:
            return StreamResult(name, ok=False, error=str(exc))

    # --- flussi ----------------------------------------------------------------------------
    def _repo(self, facts: dict, received: str):
        meta = self.api.get(f"/repos/{self.repo.repo}").body
        branch = meta["default_branch"]
        facts.update(
            default_branch=branch, private=meta.get("private"), archived=meta.get("archived"),
            pushed_at=meta.get("pushed_at"), open_issues_and_prs=meta.get("open_issues_count", 0),
            html_url=meta.get("html_url"),
        )
        head = self.api.get(f"/repos/{self.repo.repo}/branches/{branch}").body
        sha = head["commit"]["sha"]
        commit = head["commit"].get("commit") or {}
        committed_at = (commit.get("committer") or {}).get("date") or received
        facts["head"] = {"branch": branch, "sha": sha, "committed_at": committed_at,
                         "message": untrusted((commit.get("message") or "").split("\n", 1)[0], 100)}
        event = Event(self.source, "head", branch, sha, committed_at, received,
                      {"message": facts["head"]["message"]})
        return [event], {"sha": sha}

    def _runs(self, workflow: str, branch: str, cursor: Optional[dict], workflows: dict,
              ci_by_sha: dict[str, str], received: str):
        cursor = cursor or {}
        response = self.api.get(
            f"/repos/{self.repo.repo}/actions/workflows/{workflow}/runs",
            {"per_page": RUNS_PER_PAGE, "page": 1}, etag=cursor.get("etag"),
        )
        if response.status == 304:
            summary = cursor.get("summary") or {}
        else:
            summary = _summarize_runs(response.body.get("workflow_runs") or [], branch)
        workflows[workflow] = summary["latest_default"] if summary else None
        if workflow == self.repo.ci_workflow and summary:
            ci_by_sha.update(summary.get("by_sha") or {})
        if response.status == 304:
            return [], cursor

        events = []
        for run in response.body.get("workflow_runs") or []:
            if run.get("status") != "completed":
                continue
            events.append(Event(
                self.source, "workflow_run", str(run["id"]),
                f"{run.get('conclusion')}#{run.get('run_attempt', 1)}",
                run.get("updated_at") or received, received,
                {"workflow": workflow, "branch": run.get("head_branch"), "head_sha": run.get("head_sha"),
                 "conclusion": run.get("conclusion"), "event": run.get("event"), "url": run.get("html_url"),
                 "run_number": run.get("run_number"), "default_branch": run.get("head_branch") == branch},
            ))
        return events, {"etag": response.headers.get("etag"), "summary": summary}

    def _issues(self, cursor: Optional[dict], now: datetime, received: str):
        since = (cursor or {}).get("since") or iso(now - INITIAL_LOOKBACK)
        events, newest = [], since
        for page in range(1, MAX_ISSUE_PAGES + 1):
            items = self.api.get(f"/repos/{self.repo.repo}/issues", {
                "state": "all", "sort": "updated", "direction": "asc", "since": since,
                "per_page": 100, "page": page,
            }).body or []
            for item in items:
                events.append(_issue_event(self.source, item, received))
                newest = max(newest, item.get("updated_at") or newest)
            if len(items) < 100:
                break
        # Se le pagine non bastano il cursore si ferma all'ultimo elemento letto: il resto arriva
        # al giro successivo. `since` e' inclusivo, i doppioni li scarta la deduplicazione.
        return events, {"since": newest}

    def _pulls(self, facts: dict, ci_by_sha: dict[str, str]):
        pulls = self.api.get(f"/repos/{self.repo.repo}/pulls",
                             {"state": "open", "per_page": 50, "page": 1}).body or []
        open_prs = []
        for pr in pulls[: self.repo.max_open_prs]:
            sha = pr["head"]["sha"]
            ci = ci_by_sha.get(sha) or self._ci_for_sha(sha)
            open_prs.append({
                "number": pr["number"], "title": untrusted(pr.get("title")), "draft": bool(pr.get("draft")),
                "head_sha": sha, "url": pr.get("html_url"), "created_at": pr.get("created_at"),
                "author": untrusted((pr.get("user") or {}).get("login"), 40), "ci": ci,
            })
        facts["open_prs"] = open_prs
        facts["open_prs_truncated"] = len(pulls) > self.repo.max_open_prs
        return [], None

    def _ci_for_sha(self, sha: str) -> str:
        body = self.api.get(f"/repos/{self.repo.repo}/actions/runs", {"head_sha": sha, "per_page": 20}).body or {}
        runs = [r for r in body.get("workflow_runs") or [] if _workflow_file(r) == self.repo.ci_workflow]
        return _ci_state(runs[0]) if runs else "none"


def _workflow_file(run: dict) -> str:
    return (run.get("path") or "").rsplit("/", 1)[-1].split("@", 1)[0]


def _ci_state(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    return "success" if run.get("conclusion") == "success" else "failure"


def _summarize_runs(runs: list[dict], branch: str) -> dict:
    """Ultima run conclusa sul branch di default e stato CI per SHA (la run piu' recente vince)."""
    latest_default = None
    by_sha: dict[str, str] = {}
    for run in runs:  # GitHub le restituisce dalla piu' recente
        sha = run.get("head_sha")
        if sha and sha not in by_sha:
            by_sha[sha] = _ci_state(run)
        if latest_default is None and run.get("head_branch") == branch and run.get("status") == "completed":
            latest_default = {"id": run["id"], "conclusion": run.get("conclusion"), "head_sha": sha,
                              "url": run.get("html_url"), "at": run.get("updated_at")}
    return {"latest_default": latest_default, "by_sha": by_sha}


def _issue_event(source: str, item: dict, received: str) -> Event:
    is_pr = "pull_request" in item
    state = item.get("state", "open")
    if is_pr and state == "closed" and (item["pull_request"] or {}).get("merged_at"):
        state = "merged"
    elif not is_pr and state == "closed" and item.get("state_reason"):
        state = f"closed:{item['state_reason']}"
    return Event(
        source, "pull_request" if is_pr else "issue", str(item["number"]), state,
        item.get("updated_at") or received, received,
        {"title": untrusted(item.get("title")), "url": item.get("html_url"),
         "author": untrusted((item.get("user") or {}).get("login"), 40),
         "labels": [untrusted(label.get("name"), 40) for label in item.get("labels") or []][:10]},
    )
