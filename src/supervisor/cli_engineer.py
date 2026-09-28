"""Comandi `python -m supervisor engineer ...` (M3).

Il workflow `.github/workflows/engineer.yml` li usa in tre job separati:
- `claim`   (token in lettura): `engineer scan` + `engineer claim`;
- `work`    (token in lettura, chiavi AI, controlli in Docker senza rete): `engineer work`;
- `open-pr` (token di scrittura, niente chiavi AI, nessun codice eseguito): `engineer open-pr`.
`engineer verify` gira nel workflow Observe e segue le draft PR aperte.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from supervisor.cli import _now, build_gateway
from supervisor.collectors.github import GitHubApi
from supervisor.collectors.http import RequestsHttp
from supervisor.core.budget import load_budget
from supervisor.core.config import Settings, load_sources
from supervisor.core.policy import load_policy
from supervisor.engineering.config import load_engineering
from supervisor.engineering.executor import ExecutorBlocked, GitHubWriter, open_pr
from supervisor.engineering.patch import sha256
from supervisor.engineering.runner import DockerRunner, LocalRunner, dockerfile
from supervisor.engineering.service import claim, scan_approvals, verify_prs
from supervisor.engineering.tasks import TaskConflict, TaskQueue
from supervisor.engineering.worker import Models, run_work
from supervisor.llm.catalog import load_routing
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state import open_store


def _github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def _enabled(settings: Settings) -> bool:
    if not settings.enabled:
        print("SUP_ENABLED=false: engineer disabilitato")
    return settings.enabled


def cmd_scan(args, settings: Settings) -> int:
    if not _enabled(settings):
        return 0
    store = open_store(settings)
    outcome = scan_approvals(TaskQueue(store), GitHubApi(RequestsHttp(), settings.github_token),
                             load_engineering(settings.config_dir), _now(args))
    for task_id in outcome.created:
        print(f"nuovo task: {task_id}")
    for rejected in outcome.rejected:
        print(f"ignorata {rejected.repo}#{rejected.number}: {rejected.reason}")
    if not outcome.created:
        print("nessuna nuova approvazione")
    return 0


def cmd_claim(args, settings: Settings) -> int:
    _github_output(task_id="")
    if not _enabled(settings):
        return 0
    if load_policy().decide("create_branch_or_draft_pr") == "deny":
        print("policy: create_branch_or_draft_pr = deny, nessun task reclamato")
        return 0
    config = load_engineering(settings.config_dir)
    owner = os.environ.get("GITHUB_RUN_ID") or f"local-{uuid.uuid4().hex[:8]}"
    task = claim(TaskQueue(open_store(settings)), GitHubApi(RequestsHttp(), settings.github_token), config, owner,
                 _now(args), load_budget(settings.config_dir).tasks.max_fix_attempts)
    if task is None:
        print("nessun task da lavorare")
        return 0
    print(f"reclamato {task['id']} ({task['repo']}#{task['issue_number']}) base {task['base_sha'][:7]}")
    _github_output(task_id=task["id"], repo=task["repo"], base_sha=task["base_sha"])
    return 0


def cmd_dockerfile(args, settings: Settings) -> int:
    repo = load_engineering(settings.config_dir).repos[args.repo]
    Path(args.out).write_text(dockerfile(repo), encoding="utf-8")
    print(f"Dockerfile scritto in {args.out}")
    return 0


def _models(settings: Settings) -> Models:
    routing = load_routing(settings.config_dir)
    plan, patch, review = routing["engineer_plan"], routing["engineer_patch"], routing.get("engineer_review")
    return Models(author=patch.model or plan.model, reviewer=review.model if review else None,
                  plan_tokens=plan.max_output_tokens, patch_tokens=patch.max_output_tokens,
                  review_tokens=review.max_output_tokens if review else 1500)


def cmd_work(args, settings: Settings) -> int:
    if not _enabled(settings):
        return 0
    store = open_store(settings)
    queue = TaskQueue(store)
    task = queue.get(args.task)
    if task is None or task["state"] != "running" or task.get("phase") != "work":
        print(f"task {args.task} non in lavorazione")
        return 1
    repo = load_engineering(settings.config_dir).repos[task["repo"]]
    runner = DockerRunner(args.image) if args.runner == "docker" else LocalRunner()
    now = _now(args)
    models = _models(settings)
    result = run_work(task, Path(args.workspace), repo, build_gateway(settings, store), runner, models, now,
                      max_attempts=load_budget(settings.config_dir).tasks.max_fix_attempts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {**result.metadata(), "task_id": task["id"], "base_sha": task["base_sha"], "author_model": models.author}
    (out / "result.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.status == "patch_ready":
        (out / "patch.diff").write_text(result.patch, encoding="utf-8", newline="")
        queue.transition(task["id"], expect_states=("running",), expect_phase="work", now=now, phase="patch_ready",
                         note=f"patch pronta: {len(result.files)} file, {result.lines_changed} righe",
                         patch_sha256=result.patch_sha256, work=meta)
        print(f"patch pronta: {', '.join(result.files)} ({result.lines_changed} righe)")
        return 0
    queue.transition(task["id"], expect_states=("running",), now=now, state=result.status, note=result.error,
                     error=result.error, work=meta)
    print(f"{result.status}: {result.error}")
    return 0


def cmd_open_pr(args, settings: Settings) -> int:
    if not _enabled(settings):
        return 0
    store = open_store(settings)
    queue = TaskQueue(store)
    task = queue.get(args.task)
    if task is None:
        print(f"task {args.task} sconosciuto")
        return 1
    config = load_engineering(settings.config_dir)
    folder = Path(args.input)
    patch_text = (folder / "patch.diff").read_text(encoding="utf-8")
    meta = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    http = RequestsHttp()
    try:
        pr = open_pr(task=task, queue=queue, api=GitHubApi(http, settings.github_token),
                     writer=GitHubWriter(http, settings.github_write_token), config=config,
                     repo=config.repos[task["repo"]], policy=load_policy(), workspace=Path(args.workspace),
                     patch_text=patch_text, meta=meta, now=_now(args))
    except (ExecutorBlocked, TaskConflict) as exc:
        print(f"bloccato: {exc}")
        return 1
    print(f"draft PR #{pr.number} {'ritrovata' if pr.reused else 'aperta'}: {pr.url} (patch {sha256(patch_text)[:12]})")
    notifier = TelegramNotifier(http, settings.telegram_bot_token, settings.admin_chat_id)
    if not pr.reused:
        notifier.send(store, "engineer", f"GTP Supervisor: draft PR #{pr.number} per la issue #{task['issue_number']} "
                                         f"— {pr.url}\nLa CI e' in corso: ti avviso quando e' verde.", _now(args))
    return 0


def cmd_verify(args, settings: Settings) -> int:
    if not _enabled(settings):
        return 0
    store = open_store(settings)
    http = RequestsHttp()
    updates = verify_prs(TaskQueue(store), GitHubApi(http, settings.github_token), load_sources(settings.config_dir),
                         _now(args))
    notifier = TelegramNotifier(http, settings.telegram_bot_token, settings.admin_chat_id)
    for update in updates:
        print(update.message)
        if args.notify and update.kind in ("ci_green", "ci_failed"):
            notifier.send(store, "engineer", f"GTP Supervisor: {update.message}", _now(args))
    if not updates:
        print("nessun cambiamento sulle PR del supervisore")
    return 0


def cmd_list(args, settings: Settings) -> int:
    tasks = TaskQueue(open_store(settings)).list()
    for task in tasks:
        pr = f" PR #{task['pr_number']}" if task.get("pr_number") else ""
        error = f" — {task['error']}" if task.get("error") else ""
        print(f"{task['id']}: {task['state']}/{task.get('phase')}{pr} ({task['repo']}#{task['issue_number']}){error}")
    if not tasks:
        print("nessun task engineering")
    return 0


def register(sub) -> None:
    p = sub.add_parser("engineer", help="worker engineering: fix piccoli in draft PR (M3)")
    eng = p.add_subparsers(dest="engineer_command", required=True)

    def add(name: str, fn, help_text: str) -> argparse.ArgumentParser:
        parser = eng.add_parser(name, help=help_text)
        parser.add_argument("--now", help=argparse.SUPPRESS)
        parser.set_defaults(fn=fn)
        return parser

    add("scan", cmd_scan, "crea task dalle issue approvate con etichetta")
    add("claim", cmd_claim, "reclama il prossimo task (uno attivo per repository)")
    d = add("dockerfile", cmd_dockerfile, "Dockerfile dell'immagine di controllo")
    d.add_argument("--repo", required=True)
    d.add_argument("--out", required=True)
    w = add("work", cmd_work, "prepara e verifica la patch (nessuna scrittura su GitHub)")
    w.add_argument("--task", required=True)
    w.add_argument("--workspace", required=True, help="checkout del repository allo SHA di partenza")
    w.add_argument("--out", required=True, help="cartella per patch.diff e result.json")
    w.add_argument("--runner", choices=("docker", "local"), default="docker")
    w.add_argument("--image", default="gtp-check:latest")
    o = add("open-pr", cmd_open_pr, "verifica e apre la draft PR (unico comando con permessi di scrittura)")
    o.add_argument("--task", required=True)
    o.add_argument("--workspace", required=True, help="checkout pulito allo SHA di partenza")
    o.add_argument("--in", dest="input", required=True, help="cartella con patch.diff e result.json")
    v = add("verify", cmd_verify, "segue CI e stato delle draft PR del supervisore")
    v.add_argument("--notify", action="store_true")
    add("list", cmd_list, "elenco dei task engineering")


