"""Approvazioni su GitHub: l'etichetta `supervisor:fix` messa da Michele su una issue aperta.

L'etichetta da sola non basta:
- l'evento `labeled` piu' recente deve essere di un approvatore configurato (chiunque abbia accesso in
  scrittura puo' mettere etichette, ma solo gli approvatori contano);
- l'approvazione copre il contenuto preciso: si registra l'hash di titolo e corpo al momento della
  scoperta e lo si ricontrolla prima di aprire la PR. Se la issue cambia serve una nuova approvazione
  (togliere e rimettere l'etichetta);
- togliere l'etichetta o chiudere la issue revoca l'approvazione.
Il testo della issue resta un dato non fidato: definisce il compito, non amplia i permessi.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from supervisor.collectors.github import GitHubApi
from supervisor.core.scrub import untrusted
from supervisor.engineering.config import EngineeringConfig

MAX_EVENT_PAGES = 5


def content_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovedIssue:
    repo: str
    number: int
    title: str
    body: str
    url: str
    content_hash: str
    approver: str
    approved_at: str
    event_id: int


@dataclass(frozen=True)
class RejectedLabel:
    repo: str
    number: int
    reason: str


def _latest_label_event(api: GitHubApi, repo: str, number: int, label: str) -> Optional[dict]:
    latest = None
    for page in range(1, MAX_EVENT_PAGES + 1):
        events = api.get(f"/repos/{repo}/issues/{number}/events", {"per_page": 100, "page": page}).body or []
        for event in events:
            if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label:
                latest = event
        if len(events) < 100:
            break
    return latest


def scan(api: GitHubApi, config: EngineeringConfig, repo: str) -> tuple[list[ApprovedIssue], list[RejectedLabel]]:
    issues = api.get(f"/repos/{repo}/issues", {"labels": config.approval_label, "state": "open",
                                                "per_page": 50, "page": 1}).body or []
    approved, rejected = [], []
    for issue in issues:
        if "pull_request" in issue:
            continue  # solo issue: le PR (anche quelle del supervisore) non sono compiti
        event = _latest_label_event(api, repo, issue["number"], config.approval_label)
        actor = ((event or {}).get("actor") or {}).get("login", "")
        if not event or actor not in config.approvers:
            rejected.append(RejectedLabel(repo, issue["number"],
                                          f"etichetta messa da '{untrusted(actor, 40)}', non da un approvatore"))
            continue
        title, body = issue.get("title") or "", issue.get("body") or ""
        approved.append(ApprovedIssue(
            repo=repo, number=issue["number"], title=title, body=body, url=issue.get("html_url", ""),
            content_hash=content_hash(title, body), approver=actor, approved_at=event.get("created_at", ""),
            event_id=int(event["id"]),
        ))
    return approved, rejected


def verify(api: GitHubApi, config: EngineeringConfig, task: dict) -> str:
    """Motivo per cui l'approvazione del task non vale piu', o stringa vuota."""
    repo, number, approval = task["repo"], task["issue_number"], task["approval"]
    issue = api.get(f"/repos/{repo}/issues/{number}").body or {}
    if issue.get("state") != "open":
        return f"la issue #{number} non e' piu' aperta"
    labels = {label.get("name") for label in issue.get("labels") or []}
    if config.approval_label not in labels:
        return f"etichetta {config.approval_label} rimossa: approvazione revocata"
    if content_hash(issue.get("title") or "", issue.get("body") or "") != approval["content_hash"]:
        return f"la issue #{number} e' cambiata dopo l'approvazione: togliere e rimettere l'etichetta"
    event = _latest_label_event(api, repo, number, config.approval_label)
    if not event or int(event["id"]) != approval["event_id"]:
        return "l'approvazione e' stata rinnovata: vale il nuovo task"
    return ""
