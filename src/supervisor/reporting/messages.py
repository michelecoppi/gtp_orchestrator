"""Messaggi Telegram brevi e strutturati (HTML di Telegram: grassetto, corsivo, link).

Principi:
- la prima riga dice subito se c'e' qualcosa da fare;
- poche righe per sezione, solo cio' che serve a decidere; il resto sta nel report completo (link alla run);
- ogni testo dinamico passa da `esc()`: titoli di PR, errori e testi dei modelli sono dati non fidati e non
  possono aggiungere formattazione o link;
- un messaggio non supera mai il limite di Telegram: si tagliano prima i dettagli meno importanti.
"""
from __future__ import annotations

import html
import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from supervisor.core.clock import ROME, iso
from supervisor.core.models import Event, Finding, SourceReport

LIMIT = 3800
SEVERITY_ICON = {"alta": "🔴", "media": "🟠", "bassa": "🟡"}
SEVERITY_ORDER = {"alta": 0, "media": 1, "bassa": 2}
WEEKDAYS = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
SUBJECTS = {"guess_the_player_from_the_path": "gioco", "promo_studio": "Promo", "promo": "Promo"}
WORKFLOW_NAMES = {"ci.yml": "CI", "deploy.yml": "Deploy", "backup.yml": "Backup",
                  "restore-verification.yml": "Restore", "promo.yml": "Cron Promo"}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def link(url: Optional[str], text: str) -> str:
    if not url or not str(url).startswith(("https://", "http://")):
        return esc(text)
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def run_url() -> str:
    """Link alla run di GitHub Actions che ha prodotto il messaggio (vuoto fuori da Actions)."""
    server, repo, run = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def day_label(now: datetime) -> str:
    local = now.astimezone(ROME)
    return f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]}"


def subject_name(subject: str) -> str:
    short = subject.rsplit("/", 1)[-1].split(":", 1)[-1]
    if subject.startswith("posthog"):
        return "PostHog"
    return SUBJECTS.get(short, short)


def finding_title(f: Finding) -> str:
    if f.rule == "ci_failed":
        return "CI rossa su main"
    if f.rule == "deploy_failed":
        return "Deploy in produzione fallito"
    if f.rule == "workflow_failed":
        workflow = f.key.split(":", 1)[0]
        return f"{WORKFLOW_NAMES.get(workflow, workflow)} fallito su main"
    if f.rule == "pr_without_green_ci":
        return f"PR #{f.key.split(':', 1)[0]} senza CI verde"
    return {
        "default_branch_unexpected": "Branch di default inatteso",
        "promo_drafts_stale": "Bozze Promo in attesa da oltre 24 ore",
        "promo_drafts_missing": "Bozze Promo di oggi mancanti",
        "promo_post_failed": "Pubblicazione Promo fallita",
        "promo_approved_overdue": "Post Promo approvato ma non pubblicato",
        "source_unavailable": "Dati non disponibili da una sorgente",
        "analytics_data_quality": "Problema nei dati di PostHog",
    }.get(f.rule, f.statement)


def _join(blocks: list[str]) -> str:
    """Unisce i blocchi fermandosi prima del limite (i blocchi sono gia' in ordine di importanza)."""
    out: list[str] = []
    for block in blocks:
        if sum(len(b) + 2 for b in out) + len(block) > LIMIT:
            out.append("<i>… messaggio accorciato: dettagli nel report completo.</i>")
            break
        out.append(block)
    return "\n\n".join(out)


def _finding_lines(findings: list[Finding], new_ids: set[str], decisions: dict[str, dict], limit: int) -> list[str]:
    ordered = sorted(findings, key=lambda f: (f.id not in new_ids, SEVERITY_ORDER.get(f.severity, 9), f.created_at))
    lines = []
    for f in ordered[:limit]:
        new = "🆕 " if f.id in new_ids else ""
        evidence = next((e for e in f.evidence if str(e).startswith("http")), None)
        line = f"{SEVERITY_ICON.get(f.severity, '•')} {new}<b>{esc(finding_title(f))}</b> · {esc(subject_name(f.subject))}"
        if evidence:
            line += f" — {link(evidence, 'apri')}"
        decision = decisions.get(f.id)
        if decision and decision.get("state") == "proposed":
            line += f"\n    ↳ <i>{esc(decision.get('proposed_action', ''))[:160]}</i>"
        lines.append(line)
    if len(ordered) > limit:
        lines.append(f"<i>…e altri {len(ordered) - limit}</i>")
    return lines


def _icon(latest: Optional[dict]) -> str:
    if not latest:
        return "➖"
    return "✅" if latest.get("conclusion") == "success" else "❌"


def _repo_block(report: SourceReport, events: list[Event], title: str) -> str:
    facts = report.facts
    if not facts:
        return f"<b>{title}</b>\n⚠️ dati non disponibili"
    workflows = facts.get("workflows") or {}
    status = " · ".join(f"{WORKFLOW_NAMES.get(name, name)} {_icon(latest)}" for name, latest in workflows.items())
    lines = [f"<b>{title}</b>", status or "Workflow: n/d"]
    if facts.get("default_branch") and facts.get("default_branch") != facts.get("expected_default_branch"):
        lines.append(f"⚠️ branch di default: {esc(facts['default_branch'])}")
    prs = facts.get("open_prs")
    if prs is not None:
        red = sum(1 for p in prs if p.get("ci") == "failure")
        pr_text = f"PR aperte: {len(prs)}" + (f" ({red} con CI rossa)" if red else "")
        issues = facts.get("open_issues")
        lines.append(pr_text + (f" · Issue aperte: {issues}" if issues is not None else ""))
    changes = Counter((e.type, e.state.split(":", 1)[0]) for e in events if e.type in ("issue", "pull_request"))
    done = []
    if changes[("pull_request", "merged")]:
        done.append(f"{changes[('pull_request', 'merged')]} PR unite")
    if changes[("issue", "closed")]:
        done.append(f"{changes[('issue', 'closed')]} issue chiuse")
    if changes[("issue", "open")]:
        done.append(f"{changes[('issue', 'open')]} issue nuove")
    if done:
        lines.append("Ultime 24h: " + ", ".join(done))
    if report.completeness != "completa":
        lines.append(f"⚠️ dati {esc(report.completeness)}")
    return "\n".join(lines)


def _promo_block(queue: Optional[SourceReport], repo: Optional[SourceReport]) -> str:
    lines = ["<b>🎬 Promo Studio</b>"]
    if repo and repo.facts.get("workflows"):
        lines.append(" · ".join(f"{WORKFLOW_NAMES.get(n, n)} {_icon(latest)}"
                                for n, latest in repo.facts["workflows"].items()))
    if queue is None or not queue.configured:
        lines.append("Coda dei post: non collegata")
    elif not queue.facts:
        lines.append("⚠️ coda dei post non disponibile")
    else:
        f = queue.facts
        by = f.get("by_status") or {}
        waiting, failed = by.get("draft", 0), by.get("failed", 0)
        lines.append(f"Bozze da approvare: {waiting}" + (f" ({len(f.get('stale_drafts') or [])} ferme da oltre 24h)"
                                                        if f.get("stale_drafts") else "")
                     + f" · Fallite: {failed}")
        lines.append(f"Pubblicati negli ultimi 7 giorni: {f.get('published_last_7d', 0)}")
    return "\n".join(lines)


def _rate_text(r: dict[str, Any]) -> str:
    if r.get("value") is None:
        return "n/d"
    text = f"{r['value']:.0%} ({r['numerator']} su {r['denominator']})"
    return text + (" · pochi dati" if r.get("status") == "sotto_soglia" else "")


def _product_block(report: SourceReport) -> str:
    lines = ["<b>📊 Giocatori</b>"]
    if not report.configured:
        return lines[0] + "\nMetriche PostHog non collegate"
    facts = report.facts
    if not facts:
        return lines[0] + "\n⚠️ metriche non disponibili"
    for note in facts.get("data_quality") or []:
        lines.append(f"⚠️ {esc(note)}")
    completion = (facts.get("completion") or {}).get("completion")
    if completion:
        lines.append(f"Daily completate (7 giorni): {_rate_text(completion)}")
    north = [w for w in facts.get("north_star") or [] if w.get("mature")]
    if north:
        last = north[-1]
        lines.append(f"Nuovi giocatori attivati (settimana del {esc(last['week'])}): {last['activated']} su {last['mature']}")
    else:
        lines.append("Nuovi giocatori attivati: nessuno da misurare ancora")
    campaigns = facts.get("activation_by_campaign") or []
    if campaigns:
        best = campaigns[0]
        lines.append(f"Campagna principale: {esc(best['campaign_id'])} — {_rate_text(best)}")
    return "\n".join(lines)


def _tasks_block(tasks: list[dict], now: datetime) -> Optional[str]:
    active = [t for t in tasks if t["state"] in ("queued", "running", "awaiting_approval")
              or (t["state"] in ("failed", "blocked") and t.get("updated_at", "") >= iso(now - timedelta(days=1)))]
    if not active:
        return None
    labels = {"ci_green": "✅ CI verde: tocca a te la review", "ci_pending": "⏳ CI in corso", "ci_failed": "❌ CI fallita",
              "work": "⚙️ in lavorazione", "patch_ready": "⚙️ apertura PR in corso"}
    lines = ["<b>🛠 Lavori del supervisore</b>"]
    for t in active[:5]:
        state = labels.get(t.get("phase", ""), t["state"]) if t["state"] not in ("failed", "blocked", "queued") else {
            "failed": "❌ non riuscito", "blocked": "⛔ bloccato", "queued": "🕒 in coda"}[t["state"]]
        pr = f" — {link(t.get('pr_url'), 'PR #' + str(t['pr_number']))}" if t.get("pr_number") else ""
        lines.append(f"• {esc(subject_name(t['repo']))} #{t['issue_number']}: {state}{pr}")
    return "\n".join(lines)


def _budget_line(b: dict) -> str:
    def usd(micros: int) -> str:
        return f"{micros / 1_000_000:.2f}".replace(".", ",")

    text = (f"💰 Budget AI: oggi {usd(b['day_actual'])} $ su {usd(b['day_hard'])} · mese {usd(b['month_actual'])} $ "
            f"su {usd(b['month_hard'])}")
    if b.get("open_reservations"):
        text += f"\n⚠️ {b['open_reservations']} chiamate da riconciliare"
    return text


def brief_message(snapshot: Optional[dict], open_findings: list[Finding], events_24h: list[Event], now: datetime,
                  decisions: Optional[dict[str, dict]] = None, budget: Optional[dict] = None,
                  tasks: Optional[list[dict]] = None, details: str = "") -> str:
    header = f"<b>☀️ Brief GTP · {day_label(now)}</b>"
    if not snapshot:
        return header + "\n\n⚠️ Nessun dato raccolto ancora."
    since = iso(now - timedelta(hours=24))
    new_ids = {f.id for f in open_findings if f.created_at >= since}
    if open_findings:
        summary = f"🔴 <b>{len(open_findings)} {'problema aperto' if len(open_findings) == 1 else 'problemi aperti'}</b>"
        summary += f" ({len(new_ids)} {'nuovo' if len(new_ids) == 1 else 'nuovi'})" if new_ids else ""
    else:
        summary = "✅ <b>Tutto a posto</b>: nessun problema aperto"
    blocks = [header + "\n" + summary]
    if open_findings:
        blocks.append("<b>Da guardare</b>\n" + "\n".join(_finding_lines(open_findings, new_ids, decisions or {}, 5)))
    reports = {r.source: r for r in (SourceReport.from_dict(s) for s in snapshot.get("sources", []))}
    events_24h = [e for e in events_24h if e.occurred_at >= since]
    game = next((r for s, r in reports.items() if s.endswith("guess_the_player_from_the_path")), None)
    promo_repo = next((r for s, r in reports.items() if s.endswith("/promo_studio")), None)
    queue = next((r for r in reports.values() if r.kind == "promo"), None)
    product = next((r for r in reports.values() if r.kind == "posthog"), None)
    if game:
        blocks.append(_repo_block(game, [e for e in events_24h if e.source == game.source], "🎮 Gioco"))
    blocks.append(_promo_block(queue, promo_repo))
    if product:
        blocks.append(_product_block(product))
    task_block = _tasks_block(tasks or [], now)
    if task_block:
        blocks.append(task_block)
    footer = [_budget_line(budget)] if budget else []
    if details:
        footer.append(link(details, "📄 Report completo"))
    if footer:
        blocks.append("\n".join(footer))
    return _join(blocks)


def findings_message(findings: list[Finding], details: str = "") -> str:
    count = len(findings)
    lines = [f"<b>🔔 GTP · {count} {'nuovo problema' if count == 1 else 'nuovi problemi'}</b>"]
    lines += _finding_lines(findings, {f.id for f in findings}, {}, 8)
    if details:
        lines.append(link(details, "📄 Dettagli"))
    return _join(["\n".join(lines)])


def pr_opened_message(issue_number: int, issue_title: str, pr_number: int, pr_url: str) -> str:
    return (f"<b>🛠 Draft PR aperta</b> · gioco #{issue_number}\n<i>{esc(issue_title)}</i>\n\n"
            f"La CI è in corso: ti avviso quando è verde.\n{link(pr_url, f'Apri la PR #{pr_number}')}")


def pr_update_message(kind: str, issue_number: int, pr_number: int, pr_url: str, head_sha: str) -> str:
    if kind == "ci_green":
        head = "<b>✅ PR pronta per la tua review</b>"
        body = f"CI verde sull'ultimo commit <code>{esc(head_sha[:7])}</code>. Il merge lo decidi tu."
    else:
        head = "<b>❌ CI fallita sulla PR del supervisore</b>"
        body = f"Commit <code>{esc(head_sha[:7])}</code>. Puoi chiudere la PR o correggerla."
    return f"{head} · gioco #{issue_number}\n{body}\n{link(pr_url, f'Apri la PR #{pr_number}')}"


METRIC_NAMES = {"activation_24h": "attivazione nelle prime 24 ore", "daily_completion": "Daily completate",
                "hint_usage": "uso dei suggerimenti", "return_7d": "ritorno entro 7 giorni",
                "north_star_activation": "nuovi giocatori attivati", "referral_conversion": "conversione referral"}


def growth_message(week: str, proposal: dict[str, Any], brief: Optional[dict[str, Any]], details: str = "") -> str:
    kind = "🧪 esperimento misurabile" if proposal.get("kind") == "esperimento" else "🔍 verifica qualitativa"
    blocks = [f"<b>📈 Review settimanale · {esc(week)}</b>",
              f"<b>Priorità:</b> {esc(proposal['intervention'])}\n<b>Perché:</b> {esc(proposal['observation'])}",
              f"<b>Tipo:</b> {kind}\n<i>{esc(proposal.get('feasibility', {}).get('reason', ''))}</i>\n"
              f"<b>Metrica:</b> {esc(METRIC_NAMES.get(proposal['primary_metric'], proposal['primary_metric']))}"]
    if proposal.get("unverified_numbers"):
        blocks.append("⚠️ Numeri da verificare: " + esc(", ".join(proposal["unverified_numbers"])))
    if brief:
        blocks.append(f"<b>🎬 Brief per Promo</b> (bozza): <code>{esc(brief['campaign_id'])}</code>\n"
                      f"{esc(brief['channel'])} · {esc(brief['language'])} · {esc(brief['format'])} — "
                      f"«{esc(brief['cta'])}»\nFile per <code>brief-import</code> nel report.")
    if details:
        blocks.append(link(details, "📄 Review completa"))
    return _join(blocks)
