import json
from pathlib import Path

import pytest

from factory import GAME, PROMO, github_fixtures, issue, post, run
from supervisor import cli
from supervisor.core.config import ConfigError, Settings, load_sources
from supervisor.core.scrub import scrub, untrusted

FIXTURES = Path(__file__).parent / "fixtures"


def test_configurazione_versionata_valida():
    sources = load_sources()
    repos = {r.repo: r for r in sources.github}
    assert set(repos) == {GAME, PROMO}
    assert repos[GAME].deploy_workflow == "deploy.yml" and "ci.yml" in repos[GAME].workflows
    assert sources.promo.collection == "promo_posts"


def test_config_non_valida(tmp_path):
    (tmp_path / "sources.toml").write_text('[[github]]\nrepo = "senza-slash"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_sources(tmp_path)


def test_settings_nasconde_i_segreti():
    settings = Settings.from_env({"SUP_GITHUB_TOKEN": "ghp_segretissimo123", "SUP_TELEGRAM_BOT_TOKEN": "1:abcdef"})
    assert "segretissimo" not in repr(settings) and "abcdef" not in repr(settings)
    assert scrub("errore con ghp_segretissimo123") == "errore con [REDACTED]"
    assert not settings.enabled and settings.store == "sqlite"
    with pytest.raises(ConfigError):
        Settings.from_env({"SUP_STORE": "redis"})


def test_testo_non_fidato():
    assert untrusted("a\nb\tc\x00d") == "a b c d"
    assert untrusted("x" * 200, 10) == "x" * 9 + "…"


def write_fixture_files(tmp_path):
    fx = github_fixtures(runs={"ci.yml": [run(101, "failure")], "deploy.yml": [], "backup.yml": [],
                               "restore-verification.yml": []}, issues=[issue(1)])
    fx.update(github_fixtures(repo=PROMO, default_branch="claude/new-session-dorwhk",
                              runs={"ci.yml": [run(5)], "promo.yml": [run(6, workflow="promo.yml")]}))
    gh = tmp_path / "github.json"
    gh.write_text(json.dumps(fx), encoding="utf-8")
    promo = tmp_path / "promo.json"
    promo.write_text(json.dumps([post("p1")]), encoding="utf-8")
    return gh, promo


def test_replay_report_e_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SUP_STORE", "sqlite")
    monkeypatch.setenv("SUP_SQLITE_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.delenv("SUP_ENABLED", raising=False)
    gh, promo = write_fixture_files(tmp_path)
    args = ["replay", "--fixtures", str(gh), "--promo-fixture", str(promo), "--now", "2026-09-28T08:00:00Z"]
    assert cli.main(args) == 0
    first = capsys.readouterr().out
    assert "completed" in first and "ci_failed" not in first and "ci.yml fallito" in first
    assert cli.main(args) == 0
    assert "eventi nuovi 0, finding nuovi 0" in capsys.readouterr().out

    out = tmp_path / "out" / "brief.md"
    assert cli.main(["report", "--out", str(out), "--now", "2026-09-28T08:05:00Z"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "claude/new-session-dorwhk (ATTENZIONE: atteso main)" in text
    assert "Bozze in attesa da oltre 24 ore: 1" in text

    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "lock observe: libero" in status and f"github:{GAME}#issues" in status


def test_observe_disabilitato(monkeypatch, capsys):
    monkeypatch.setenv("SUP_ENABLED", "false")
    assert cli.main(["observe"]) == 0
    assert "disabilitato" in capsys.readouterr().out


def test_config_errata_esce_con_2(monkeypatch):
    monkeypatch.setenv("SUP_STORE", "redis")
    assert cli.main(["status"]) == 2


def test_fixture_di_replay_versionata_e_coerente(tmp_path, monkeypatch):
    monkeypatch.setenv("SUP_STORE", "memory")
    args = ["replay", "--fixtures", str(FIXTURES / "github_replay.json"),
            "--promo-fixture", str(FIXTURES / "promo_posts.json"), "--now", "2026-09-28T08:00:00Z"]
    assert cli.main(args) == 0
