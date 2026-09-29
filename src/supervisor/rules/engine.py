"""Regole deterministiche: dai fatti ai finding, senza modelli linguistici.

Due famiglie:
- regole su eventi nuovi (una run fallita sul branch di default): il finding nasce una volta e si
  risolve quando l'ultima run conclusa dello stesso workflow sul branch di default e' verde;
- regole di stato (PR senza CI verde, bozze Promo ferme, sorgente non disponibile): il finding
  esiste finche' la condizione vale e si risolve da solo quando sparisce, ma solo se i dati di
  quella sorgente sono completi. Un dato mancante non vale come "risolto".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from supervisor.core.clock import hours_between, iso
from supervisor.core.config import Sources
from supervisor.core.models import Event, Finding, SourceReport, stable_hash

FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
# Una PR appena aperta ha la CI ancora in coda: la si segnala solo dopo questo margine.
PR_CI_GRACE_HOURS = 1.0
STATEFUL_RULES = (
    "default_branch_unexpected", "pr_without_green_ci", "promo_drafts_stale", "promo_drafts_missing", "promo_post_failed",
    "promo_approved_overdue", "source_unavailable", "analytics_data_quality",
)
WORKFLOW_RULES = ("ci_failed", "deploy_failed", "workflow_failed")


def stable_key(text: str) -> str:
    return stable_hash(text)[:12]


@dataclass
class RuleOutcome:
    findings: list[Finding] = field(default_factory=list)
    resolve: list[str] = field(default_factory=list)


def evaluate(new_events: list[Event], reports: list[SourceReport], open_findings: list[Finding],
             sources: Sources, now: datetime, run_id: str) -> RuleOutcome:
    at = iso(now)
    by_source = {r.source: r for r in reports}
    repos = {r.source: r for r in sources.github}
    findings: list[Finding] = []

    def add(rule: str, subject: str, key: str, statement: str, severity: str, evidence: list,
            stateful: bool = False) -> None:
        findings.append(Finding(rule, subject, key, statement, severity, [e for e in evidence if e],
                                created_at=at, run_id=run_id, stateful=stateful))

    # --- eventi: run fallite sul branch di default ------------------------------------------
    for event in new_events:
        if event.type != "workflow_run" or not event.data.get("default_branch"):
            continue
        if event.data.get("conclusion") not in FAILED_CONCLUSIONS:
            continue
        repo = repos.get(event.source)
        if repo is None:
            continue
        workflow = event.data["workflow"]
        # Al primo giro arrivano anche run vecchie: un fallimento gia' seguito da una run verde
        # non e' un problema attuale.
        report = by_source.get(event.source)
        latest = (report.facts.get("workflows") or {}).get(workflow) if report else None
        if latest and str(latest.get("id")) != event.source_id and latest.get("conclusion") == "success":
            continue
        rule, severity = "workflow_failed", "media"
        if workflow == repo.ci_workflow:
            rule, severity = "ci_failed", "alta"
        elif workflow == repo.deploy_workflow:
            rule, severity = "deploy_failed", "alta"
        add(rule, repo.repo, f"{workflow}:{event.source_id}",
            f"{workflow} fallito su {event.data.get('branch')} (run #{event.data.get('run_number')}, "
            f"{event.data.get('conclusion')}, SHA {str(event.data.get('head_sha'))[:7]})",
            severity, [event.data.get("url")])

    # --- stato: GitHub -----------------------------------------------------------------------
    for repo in sources.github:
        report = by_source.get(repo.source)
        if report is None:
            continue
        facts = report.facts
        branch = facts.get("default_branch")
        if branch and branch != repo.expected_default_branch:
            add("default_branch_unexpected", repo.repo, branch,
                f"Il branch di default e' '{branch}', atteso '{repo.expected_default_branch}'",
                "media", [facts.get("html_url")], stateful=True)
        for pr in facts.get("open_prs") or []:
            if pr["draft"] or pr["ci"] in ("success", "pending"):
                continue
            if pr.get("created_at") and hours_between(pr["created_at"], now) < PR_CI_GRACE_HOURS:
                continue
            what = "CI fallita" if pr["ci"] == "failure" else "nessuna CI"
            add("pr_without_green_ci", repo.repo, f"{pr['number']}:{pr['head_sha']}",
                f"PR #{pr['number']} aperta con {what} sullo SHA di testa {pr['head_sha'][:7]}",
                "media" if pr["ci"] == "failure" else "bassa", [pr.get("url")], stateful=True)

    # --- stato: Promo ------------------------------------------------------------------------
    promo = by_source.get(sources.promo.source)
    if promo is not None and promo.ok:
        facts = promo.facts
        stale = facts.get("stale_drafts") or []
        if stale:
            add("promo_drafts_stale", "promo", "drafts",
                f"{len(stale)} bozze Promo in attesa di approvazione da oltre "
                f"{facts.get('stale_draft_hours', 24):g} ore", "bassa",
                [f"promo_posts/{d['id']}" for d in stale[:10]], stateful=True)
        if facts.get("drafts_missing"):
            add("promo_drafts_missing", "promo", str(facts.get("today")),
                f"Nessuna bozza Promo per oggi alle {facts.get('drafts_expected_by')}: il lavoro delle bozze "
                "non e' partito o e' fallito (Cloud Scheduler, servizio promo-approvals o workflow)",
                "media", ["promo_posts"], stateful=True)
        for post in facts.get("failed") or []:
            add("promo_post_failed", "promo", f"{post['id']}:{post['attempts']}",
                f"Pubblicazione fallita per {post['id']} (tentativi: {post['attempts']})",
                "media", [f"promo_posts/{post['id']}"], stateful=True)
        for post in facts.get("approved_overdue") or []:
            add("promo_approved_overdue", "promo", post["id"],
                f"{post['id']} approvato ma non pubblicato (previsto {post['scheduled_for']})",
                "media", [f"promo_posts/{post['id']}"], stateful=True)

    # --- stato: analytics (M4) ------------------------------------------------------------------
    for analytics in reports:
        if analytics.kind != "posthog" or not analytics.ok:
            continue
        for note in analytics.facts.get("data_quality") or []:
            add("analytics_data_quality", analytics.source, stable_key(note), f"Qualita' dati analytics: {note}",
                "media", ["docs/product-analytics.md §15b (repository del gioco)"], stateful=True)

    # --- stato: sorgenti ---------------------------------------------------------------------
    for report in reports:
        if report.configured and not report.ok:
            add("source_unavailable", report.source, "unavailable",
                f"Sorgente {report.completeness}: {report.source} ({'; '.join(report.errors)[:200]})",
                "media", [], stateful=True)

    return RuleOutcome(findings, _resolutions(findings, reports, open_findings, sources))


def _resolutions(current: list[Finding], reports: list[SourceReport], open_findings: list[Finding],
                 sources: Sources) -> list[str]:
    current_ids = {f.id for f in current}
    by_source = {r.source: r for r in reports}
    subject_source = {r.repo: r.source for r in sources.github}
    subject_source["promo"] = sources.promo.source
    for source_report in reports:
        if source_report.kind == "posthog":
            subject_source[source_report.source] = source_report.source
    resolve = []
    for finding in open_findings:
        if finding.id in current_ids:
            continue
        if finding.rule == "source_unavailable":
            report = by_source.get(finding.subject)
            if report is not None and (report.ok or not report.configured):
                resolve.append(finding.id)
        elif finding.rule in STATEFUL_RULES:
            report = by_source.get(subject_source.get(finding.subject, ""))
            if report is not None and report.ok:
                resolve.append(finding.id)
        elif finding.rule in WORKFLOW_RULES:
            if _workflow_recovered(finding, by_source.get(subject_source.get(finding.subject, ""))):
                resolve.append(finding.id)
    return resolve


def _workflow_recovered(finding: Finding, report: Optional[SourceReport]) -> bool:
    if report is None:
        return False
    workflow, _, run_id = finding.key.partition(":")
    latest = (report.facts.get("workflows") or {}).get(workflow)
    if not latest:
        return False
    return latest.get("conclusion") == "success" and str(latest.get("id")) != run_id
