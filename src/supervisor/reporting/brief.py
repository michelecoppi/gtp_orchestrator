"""Il brief: un riepilogo costruito solo dai record salvati, senza modelli linguistici.

Ogni riga risale a un fatto dello snapshot, a un evento o a un finding. Un dato che manca si
scrive "non disponibile", mai zero. La stessa struttura si rende in Markdown (artifact di
Actions) e in testo semplice (Telegram, senza parse_mode: i titoli di issue e PR sono dati non
fidati e non devono diventare formattazione o link attivi).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from supervisor.core.clock import hours_between, iso, parse_iso, rome_label
from supervisor.core.models import Event, Finding, SourceReport

NA = "non disponibile"
STALE_SNAPSHOT_HOURS = 6
SEVERITY_ORDER = {"alta": 0, "media": 1, "bassa": 2}
TELEGRAM_LIMIT = 3900


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Brief:
    title: str
    intro: list[str]
    sections: list[Section]
    new_findings: int = 0
    open_findings: int = 0


def build_brief(snapshot: Optional[dict], open_findings: list[Finding], events_24h: list[Event],
                now: datetime, decisions: Optional[dict[str, dict]] = None,
                budget: Optional[dict] = None, tasks: Optional[list[dict]] = None) -> Brief:
    title = f"Brief GTP Supervisor — {rome_label(now)} (Europe/Rome)"
    if not snapshot:
        return Brief(title, ["Nessuno snapshot salvato: eseguire prima `python -m supervisor observe`."], [])
    since = iso(now - timedelta(hours=24))
    new = [f for f in open_findings if f.created_at >= since]
    intro = [f"Dati dello snapshot {snapshot['run_id']} ({rome_label(parse_iso(snapshot['collected_at']))})."]
    age = hours_between(snapshot["collected_at"], now)
    if age > STALE_SNAPSHOT_HOURS:
        intro.append(f"ATTENZIONE: snapshot vecchio di {age:.0f} ore, i fatti potrebbero non essere attuali.")
    reports = [SourceReport.from_dict(s) for s in snapshot.get("sources", [])]
    # Conta cio' che e' successo nelle 24 ore, non cio' che e' stato raccolto (il primo giro
    # recupera anche giorni precedenti).
    events_24h = [e for e in events_24h if e.occurred_at >= since]
    sections = [_findings_section(open_findings, {f.id for f in new}, decisions or {})]
    for report in reports:
        if report.kind == "github":
            sections.append(_github_section(report, [e for e in events_24h if e.source == report.source]))
        elif report.kind == "promo":
            sections.append(_promo_section(report))
    if tasks:
        sections.append(_engineering_section(tasks, now))
    if budget is not None:
        sections.append(_budget_section(budget))
    sections.append(Section("Completezza delle sorgenti", [
        f"{r.source}: {r.completeness}" + (f" — {'; '.join(r.errors)}" if r.errors else "") for r in reports
    ]))
    return Brief(title, intro, sections, new_findings=len(new), open_findings=len(open_findings))


def _findings_section(findings: list[Finding], new_ids: set[str], decisions: dict[str, dict]) -> Section:
    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.created_at, f.id))
    lines = []
    for f in ordered:
        mark = "NUOVO " if f.id in new_ids else ""
        evidence = f" — {f.evidence[0]}" if f.evidence else ""
        lines.append(f"{mark}[{f.severity}] {f.subject}: {f.statement}{evidence}")
        decision = decisions.get(f.id)
        if decision and decision.get("state") == "proposed":
            human = ", serve Michele" if decision.get("needs_human") else ""
            lines.append(f"  proposta triage ({decision['priority']}, {decision['role']}{human}, "
                         f"{decision['model']}): {decision['rationale_summary']} → {decision['proposed_action']}")
    return Section(f"Finding aperti ({len(findings)}, nuovi nelle ultime 24h: {len(new_ids)})",
                   lines or ["Nessun finding aperto."])


def _engineering_section(tasks: list[dict], now: datetime) -> Section:
    week_ago = iso(now - timedelta(days=7))
    shown = [t for t in tasks if t["state"] not in ("completed", "failed", "blocked") or t["updated_at"] >= week_ago]
    labels = {"ci_pending": "CI in corso", "ci_green": "CI verde: tocca a te la review", "ci_failed": "CI fallita",
              "work": "in lavorazione", "patch_ready": "patch pronta, PR in apertura"}
    lines = []
    for task in shown:
        where = f" — PR #{task['pr_number']} {task.get('pr_url', '')}" if task.get("pr_number") else ""
        phase = labels.get(task.get("phase", ""), task.get("phase", ""))
        state = task["state"] if task["state"] in ("completed", "failed", "blocked", "queued") else phase
        error = f" ({task['error']})" if task.get("error") and task["state"] in ("failed", "blocked") else ""
        lines.append(f"{task['repo'].split('/')[-1]}#{task['issue_number']}: {state}{error}{where}")
    return Section("Engineering (draft PR del supervisore)", lines or ["Nessun task negli ultimi 7 giorni."])


def _budget_section(b: dict) -> Section:
    def usd(micros: int) -> str:
        return f"{micros / 1_000_000:.2f}"

    lines = [
        f"Mese {b['month']}: speso {usd(b['month_actual'])} + prenotato {usd(b['month_reserved'])} "
        f"su {usd(b['month_hard'])} USD (attenzione a {usd(b['month_soft'])})",
        f"Oggi: speso {usd(b['day_actual'])} + prenotato {usd(b['day_reserved'])} su {usd(b['day_hard'])} USD",
    ]
    if not b.get("approved"):
        lines.append("Budget non approvato: nessuna chiamata AI a pagamento.")
    if b.get("open_reservations"):
        lines.append(f"Chiamate dall'esito incerto da riconciliare: {b['open_reservations']} "
                     "(`python -m supervisor budget`)")
    return Section("Budget AI", lines)


def _workflow_line(name: str, latest: Optional[dict]) -> str:
    if not latest:
        return f"{name}: {NA}"
    return f"{name}: {latest.get('conclusion')} (SHA {str(latest.get('head_sha'))[:7]})"


def _github_section(report: SourceReport, events: list[Event]) -> Section:
    facts = report.facts
    lines = []
    branch = facts.get("default_branch")
    if branch:
        warn = "" if branch == facts.get("expected_default_branch") else \
            f" (ATTENZIONE: atteso {facts.get('expected_default_branch')})"
        lines.append(f"Branch di default: {branch}{warn}")
    else:
        lines.append(f"Branch di default: {NA}")
    head = facts.get("head")
    lines.append(f"Testa: {head['sha'][:7]} «{head['message']}» ({head['committed_at']})" if head else f"Testa: {NA}")
    workflows = facts.get("workflows") or {}
    if workflows:
        lines.append("Ultima run sul branch di default — " + "; ".join(
            _workflow_line(name, latest) for name, latest in sorted(workflows.items())))
    else:
        lines.append(f"Workflow: {NA}")
    prs = facts.get("open_prs")
    if prs is None:
        lines.append(f"PR aperte: {NA}")
    else:
        more = " (elenco troncato)" if facts.get("open_prs_truncated") else ""
        lines.append(f"PR aperte: {len(prs)}{more}")
        for pr in prs:
            draft = " [bozza]" if pr["draft"] else ""
            lines.append(f"  #{pr['number']}{draft} CI {pr['ci']}: «{pr['title']}»")
    lines.append(f"Issue aperte: {facts['open_issues']}" if "open_issues" in facts else f"Issue aperte: {NA}")
    changes = Counter((e.type, e.state.split(":", 1)[0]) for e in events if e.type in ("issue", "pull_request"))
    lines.append(
        f"Ultime 24h: issue aperte {changes[('issue', 'open')]}, chiuse {changes[('issue', 'closed')]}; "
        f"PR aperte {changes[('pull_request', 'open')]}, unite {changes[('pull_request', 'merged')]}, "
        f"chiuse senza merge {changes[('pull_request', 'closed')]}"
    )
    return Section(f"{facts.get('repo', report.source)} ({report.completeness})", lines)


def _promo_section(report: SourceReport) -> Section:
    title = f"Promo Studio ({report.completeness})"
    if not report.configured:
        return Section(title, ["Collector non configurato (SUP_GAME_FIRESTORE_PROJECT vuoto)."])
    if not report.facts:
        return Section(title, [f"Coda promo_posts: {NA}"])
    facts = report.facts
    by_status = facts.get("by_status") or {}
    return Section(title, [
        "Post in coda: " + ", ".join(f"{k} {v}" for k, v in by_status.items()) + f" (totale {facts.get('total')})",
        f"Bozze in attesa da oltre {facts.get('stale_draft_hours', 24):g} ore: {len(facts.get('stale_drafts') or [])}",
        f"Pubblicazioni fallite: {len(facts.get('failed') or [])}",
        f"Approvati ma non pubblicati: {len(facts.get('approved_overdue') or [])}",
        f"Pubblicati negli ultimi 7 giorni: {facts.get('published_last_7d')}",
    ])


def render_markdown(brief: Brief) -> str:
    out = [f"# {brief.title}", ""]
    out += [line for line in brief.intro] + [""]
    for section in brief.sections:
        out += [f"## {section.title}", ""]
        out += [("  - " + line.strip()) if line.startswith("  ") else f"- {line}" for line in section.lines]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_telegram(brief: Brief) -> str:
    out = [brief.title, *brief.intro]
    for section in brief.sections:
        out += ["", f"▸ {section.title}"]
        out += [("   ◦ " + line.strip()) if line.startswith("  ") else f"• {line}" for line in section.lines]
    text = "\n".join(out)
    if len(text) > TELEGRAM_LIMIT:
        text = text[:TELEGRAM_LIMIT] + "\n… (troncato: il brief completo e' nell'artifact del workflow)"
    return text


def render_findings_alert(findings: list[Finding]) -> str:
    lines = [f"GTP Supervisor: {len(findings)} nuovi finding"]
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id)):
        evidence = f"\n   {f.evidence[0]}" if f.evidence else ""
        lines.append(f"• [{f.severity}] {f.subject}: {f.statement}{evidence}")
    return "\n".join(lines)[:TELEGRAM_LIMIT]
