"""Orchestrazione del ciclo engineering: scoperta delle approvazioni, reclamo, verifica delle PR.

- `scan_approvals`: issue con etichetta di approvazione valida -> task `queued` (uno per evento di
  approvazione: un'approvazione rinnovata crea un task nuovo e rende non valido il vecchio);
- `claim`: reclama il prossimo task (un solo task attivo per repository), fissa lo SHA di partenza
  corrente del branch base e ricontrolla l'approvazione;
- `verify_prs`: segue le draft PR aperte. La CI conta solo se riferita allo SHA di testa attuale; merge
  e chiusura restano decisioni di Michele e chiudono il task.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from supervisor.collectors.github import GitHubApi, GitHubError
from supervisor.core.config import Sources
from supervisor.core.scrub import untrusted
from supervisor.engineering import approvals
from supervisor.engineering.config import EngineeringConfig
from supervisor.engineering.tasks import TaskConflict, TaskQueue, task_id_for

MAX_ISSUE_BODY = 20000


@dataclass
class ScanOutcome:
    created: list[str]
    rejected: list[approvals.RejectedLabel]


def scan_approvals(queue: TaskQueue, api: GitHubApi, config: EngineeringConfig, now: datetime) -> ScanOutcome:
    created, rejected = [], []
    for repo in config.repos:
        approved, refused = approvals.scan(api, config, repo)
        rejected.extend(refused)
        for issue in approved:
            task_id = task_id_for(repo, issue.number, issue.event_id)
            if queue.create({
                "id": task_id, "repo": repo, "issue_number": issue.number, "issue_url": issue.url,
                "issue_title": untrusted(issue.title, 200), "issue_body": issue.body[:MAX_ISSUE_BODY],
                "approval": {"approver": issue.approver, "approved_at": issue.approved_at,
                             "event_id": issue.event_id, "content_hash": issue.content_hash},
            }, now):
                created.append(task_id)
    return ScanOutcome(created, rejected)


def claim(queue: TaskQueue, api: GitHubApi, config: EngineeringConfig, owner: str, now: datetime,
          max_attempts: int) -> Optional[dict]:
    while True:
        task = queue.claim_next(owner, now, config.lease_minutes, max_attempts)
        if task is None:
            return None
        repo = config.repos.get(task["repo"])
        if repo is None:
            queue.transition(task["id"], expect_states=("running",), now=now, state="blocked",
                             note="repository non configurato", error="repository non configurato")
            continue
        reason = approvals.verify(api, config, task)
        if reason:
            queue.transition(task["id"], expect_states=("running",), now=now, state="blocked", note=reason, error=reason)
            continue
        head = api.get(f"/repos/{task['repo']}/branches/{repo.base_branch}").body["commit"]["sha"]
        return queue.transition(task["id"], expect_states=("running",), now=now, note=f"base {head[:7]}",
                                base_sha=head)


@dataclass
class PrUpdate:
    task_id: str
    message: str
    kind: str  # ci_green | ci_failed | merged | closed | error
    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    head_sha: str = ""


def _ci_for_sha(api: GitHubApi, repo: str, sha: str, workflow: str) -> str:
    body = api.get(f"/repos/{repo}/actions/runs", {"head_sha": sha, "per_page": 20}).body or {}
    runs = [r for r in body.get("workflow_runs") or [] if (r.get("path") or "").rsplit("/", 1)[-1] == workflow]
    if not runs:
        return "none"
    run = runs[0]
    if run.get("status") != "completed":
        return "pending"
    return "success" if run.get("conclusion") == "success" else "failure"


def verify_prs(queue: TaskQueue, api: GitHubApi, sources: Sources, now: datetime) -> list[PrUpdate]:
    ci_workflow = {r.repo: r.ci_workflow for r in sources.github}
    updates = []
    for task in queue.list("awaiting_approval"):
        repo, number = task["repo"], task["pr_number"]
        try:
            pr = api.get(f"/repos/{repo}/pulls/{number}").body
            if pr.get("merged_at"):
                queue.transition(task["id"], expect_states=("awaiting_approval",), now=now, state="completed",
                                 note="PR unita da una persona", outcome="merged")
                updates.append(PrUpdate(task["id"], f"PR #{number} unita: task {task['id']} completato", "merged"))
                continue
            if pr.get("state") == "closed":
                queue.transition(task["id"], expect_states=("awaiting_approval",), now=now, state="completed",
                                 note="PR chiusa senza merge", outcome="closed")
                updates.append(PrUpdate(task["id"], f"PR #{number} chiusa senza merge", "closed"))
                continue
            head = pr["head"]["sha"]
            ci = _ci_for_sha(api, repo, head, ci_workflow.get(repo, "ci.yml"))
        except (GitHubError, KeyError, TypeError) as exc:
            updates.append(PrUpdate(task["id"], f"PR #{number}: verifica non riuscita ({untrusted(exc, 120)})",
                                    "error"))
            continue
        phase = {"success": "ci_green", "failure": "ci_failed"}.get(ci, "ci_pending")
        if head == task.get("head_sha") and phase == task.get("phase"):
            continue
        try:
            queue.transition(task["id"], expect_states=("awaiting_approval",), now=now, phase=phase, head_sha=head,
                             note=f"CI {ci} su {head[:7]}")
        except TaskConflict:
            continue
        extra = {"issue_number": task["issue_number"], "pr_number": number, "pr_url": pr.get("html_url") or "",
                 "head_sha": head}
        if phase == "ci_green":
            updates.append(PrUpdate(task["id"], f"PR #{number} pronta per la tua review: CI verde su {head[:7]} "
                                                f"— {pr.get('html_url')}", "ci_green", **extra))
        elif phase == "ci_failed":
            updates.append(PrUpdate(task["id"], f"PR #{number}: CI fallita su {head[:7]} — {pr.get('html_url')}",
                                    "ci_failed", **extra))
    return updates
