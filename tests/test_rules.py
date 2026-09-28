from factory import GAME, NOW, SHA_PR, sources
from supervisor.core.clock import parse_iso
from supervisor.core.models import Event, Finding, SourceReport
from supervisor.rules.engine import evaluate

now = parse_iso(NOW)
SRC = f"github:{GAME}"


def run_event(run_id: str, conclusion: str, workflow: str = "ci.yml", default_branch: bool = True) -> Event:
    return Event(SRC, "workflow_run", run_id, f"{conclusion}#1", NOW, NOW, {
        "workflow": workflow, "branch": "main" if default_branch else "feat/x", "head_sha": "a" * 40,
        "conclusion": conclusion, "url": f"https://x/{run_id}", "run_number": run_id, "default_branch": default_branch,
    })


def game_report(ok: bool = True, **facts) -> SourceReport:
    base = {"repo": GAME, "default_branch": "main", "expected_default_branch": "main", "open_prs": [],
            "workflows": {}}
    return SourceReport(SRC, "github", ok, {**base, **facts}, [] if ok else ["issues: HTTP 500"])


def promo_report(ok: bool = True, configured: bool = True, **facts) -> SourceReport:
    base = {"stale_drafts": [], "failed": [], "approved_overdue": [], "stale_draft_hours": 24}
    return SourceReport("promo:promo_posts", "promo", ok, {**base, **facts} if ok else {}, [], configured)


def test_run_fallite_sul_branch_di_default():
    events = [run_event("1", "failure"), run_event("2", "failure", "deploy.yml"),
              run_event("3", "failure", "backup.yml"), run_event("4", "failure", default_branch=False),
              run_event("5", "cancelled")]
    out = evaluate(events, [game_report()], [], sources(), now, "r1")
    assert sorted((f.rule, f.severity) for f in out.findings) == [
        ("ci_failed", "alta"), ("deploy_failed", "alta"), ("workflow_failed", "media")]


def test_fallimento_gia_superato_da_una_run_verde_non_e_un_finding():
    green = game_report(workflows={"ci.yml": {"id": 2, "conclusion": "success"}})
    assert evaluate([run_event("1", "failure")], [green], [], sources(), now, "r1").findings == []


def test_finding_di_workflow_risolto_da_una_run_verde_successiva():
    failed = evaluate([run_event("1", "failure")], [game_report()], [], sources(), now, "r1").findings[0]
    same_run = game_report(workflows={"ci.yml": {"id": 1, "conclusion": "failure"}})
    assert evaluate([], [same_run], [failed], sources(), now, "r2").resolve == []
    green = game_report(workflows={"ci.yml": {"id": 2, "conclusion": "success"}})
    assert evaluate([], [green], [failed], sources(), now, "r3").resolve == [failed.id]


def test_pr_senza_ci_verde_dopo_il_margine():
    prs = [
        {"number": 1, "draft": False, "ci": "failure", "head_sha": SHA_PR, "created_at": "2026-09-27T00:00:00Z"},
        {"number": 2, "draft": False, "ci": "none", "head_sha": SHA_PR, "created_at": "2026-09-27T00:00:00Z"},
        {"number": 3, "draft": False, "ci": "failure", "head_sha": SHA_PR, "created_at": "2026-09-28T07:30:00Z"},
        {"number": 4, "draft": True, "ci": "failure", "head_sha": SHA_PR, "created_at": "2026-09-27T00:00:00Z"},
        {"number": 5, "draft": False, "ci": "pending", "head_sha": SHA_PR, "created_at": "2026-09-27T00:00:00Z"},
    ]
    out = evaluate([], [game_report(open_prs=prs)], [], sources(), now, "r1")
    assert sorted((f.key.split(":")[0], f.severity) for f in out.findings) == [("1", "media"), ("2", "bassa")]


def test_nuovo_push_sulla_pr_e_un_finding_diverso():
    pr = {"number": 1, "draft": False, "ci": "failure", "created_at": "2026-09-27T00:00:00Z"}
    a = evaluate([], [game_report(open_prs=[{**pr, "head_sha": "c" * 40}])], [], sources(), now, "r").findings
    b = evaluate([], [game_report(open_prs=[{**pr, "head_sha": "d" * 40}])], a, sources(), now, "r")
    assert a[0].id != b.findings[0].id and b.resolve == [a[0].id]


def test_branch_di_default_inatteso():
    out = evaluate([], [game_report(default_branch="claude/new-session")], [], sources(), now, "r1")
    assert [f.rule for f in out.findings] == ["default_branch_unexpected"]


def test_regole_promo():
    report = promo_report(stale_drafts=[{"id": "p1", "age_hours": 30}],
                          failed=[{"id": "p2", "attempts": 2, "error": "x"}],
                          approved_overdue=[{"id": "p3", "scheduled_for": "2026-09-27T10:00:00Z"}])
    rules = sorted(f.rule for f in evaluate([], [report], [], sources(), now, "r1").findings)
    assert rules == ["promo_approved_overdue", "promo_drafts_stale", "promo_post_failed"]


def test_dati_mancanti_non_valgono_come_risolto():
    stale = evaluate([], [promo_report(stale_drafts=[{"id": "p1", "age_hours": 30}])], [], sources(), now,
                     "r1").findings
    out = evaluate([], [promo_report(ok=False)], stale, sources(), now, "r2")
    assert out.resolve == []
    assert [f.rule for f in out.findings] == ["source_unavailable"]
    ok = evaluate([], [promo_report()], stale + out.findings, sources(), now, "r3")
    assert sorted(ok.resolve) == sorted(f.id for f in stale + out.findings)


def test_sorgente_non_configurata_non_e_un_problema():
    out = evaluate([], [promo_report(ok=False, configured=False)], [], sources(), now, "r1")
    assert out.findings == []


def test_finding_con_id_stabile():
    a = Finding("ci_failed", GAME, "ci.yml:1", "x", "alta")
    b = Finding("ci_failed", GAME, "ci.yml:1", "testo diverso", "media")
    assert a.id == b.id
