"""Evaluation dei modelli sul lavoro engineering reale (specifica, sez. 14).

Ogni caso e' una PR storica del gioco, unita con squash e collegata a una issue:
- **base**: il genitore del commit di fix; il modello lavora li' e vede solo la issue, mai la soluzione;
- **test nascosti**: i file `tests/` della fix, applicati *dopo* la patch del candidato (sovrascrivono
  quelli che il candidato ha eventualmente scritto);
- **FAIL_TO_PASS**: test che passano con la fix e non con la base: il candidato deve farli passare;
- **PASS_TO_PASS**: test di quei file che passavano gia': il candidato non deve romperli.
Le liste si calcolano una volta con `eval prepare` (Docker, nessun costo AI) e si versionano.

Il punteggio e' deterministico (test), non il giudizio di un modello. Contaminazione possibile: il
repository e' pubblico e i modelli potrebbero averlo visto; il report lo ricorda.
"""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from supervisor.engineering.config import RepoEngineering
from supervisor.engineering.patch import add_local_excludes, git, reset_workspace
from supervisor.engineering.runner import CheckRunner, dockerfile
from supervisor.engineering.worker import Models, run_work
from supervisor.llm.gateway import LLMGateway

JUNIT = ".eval-junit.xml"


@dataclass
class Case:
    id: str
    pr: int
    issue: int
    fix_sha: str
    base_sha: str = ""
    hidden_tests: list[str] = field(default_factory=list)
    f2p: list[str] = field(default_factory=list)
    p2p: list[str] = field(default_factory=list)
    issue_title: str = ""
    issue_body: str = ""
    usable: bool = True
    note: str = ""


def load_cases(cases_file: Path, lock_file: Optional[Path] = None) -> list[Case]:
    with open(cases_file, "rb") as fh:
        raw = tomllib.load(fh)
    locked: dict[str, dict] = {}
    if lock_file and lock_file.exists():
        locked = {c["id"]: c for c in json.loads(lock_file.read_text(encoding="utf-8"))}
    cases = []
    for item in raw.get("cases", []):
        base = {"id": item["id"], "pr": int(item["pr"]), "issue": int(item["issue"]), "fix_sha": item["fix_sha"]}
        cases.append(Case(**{**base, **{k: v for k, v in locked.get(item["id"], {}).items() if k not in base}}))
    return cases


def save_lock(cases: list[Case], lock_file: Path) -> None:
    lock_file.write_text(json.dumps([asdict(c) for c in cases], ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")


# --- immagini e workspace ---------------------------------------------------------------------------
def image_tag(sha: str) -> str:
    return f"gtp-check:{sha[:12]}"


def ensure_image(repo_dir: Path, sha: str, repo: RepoEngineering) -> str:
    """Immagine con le dipendenze dello SHA dato (riusata se esiste gia')."""
    tag = image_tag(sha)
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    archive = subprocess.run(["git", "-C", str(repo_dir), "archive", "--format=tar", sha], capture_output=True,
                             check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        (Path(tmp) / "eval.Dockerfile").write_text(dockerfile(repo), encoding="utf-8")
        subprocess.run(["docker", "build", "-q", "-f", str(Path(tmp) / "eval.Dockerfile"), "-t", tag, tmp],
                       check=True, capture_output=True)
    return tag


def workspace_at(repo_dir: Path, sha: str, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", "-c", "core.autocrlf=false",
                    str(repo_dir), str(dest)], check=True, capture_output=True)
    git(dest, "checkout", "-q", "--detach", sha)
    return dest


def overlay_tests(workspace: Path, fix_sha: str, paths: list[str]) -> None:
    for path in paths:
        content = subprocess.run(["git", "-C", str(workspace), "show", f"{fix_sha}:{path}"], capture_output=True,
                                 check=True).stdout
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def passing_tests(runner: CheckRunner, workspace: Path, paths: list[str], timeout: int = 900) -> set[str]:
    """Test passati nei file indicati (formato `classe::nome` della junit di pytest)."""
    junit = workspace / JUNIT
    junit.unlink(missing_ok=True)
    command = ("ln -sfn /deps/node_modules node_modules && npm run build --silent >/dev/null 2>&1; "
               f"python -m pytest -q -p no:cacheprovider -o junit_family=xunit2 --junitxml={JUNIT} "
               + " ".join(paths))
    runner.run(command, workspace, timeout)
    if not junit.exists():
        return set()
    passed = set()
    for case in ET.parse(junit).getroot().iter("testcase"):
        if not any(child.tag in ("failure", "error", "skipped") for child in case):
            passed.add(f"{case.get('classname')}::{case.get('name')}")
    junit.unlink(missing_ok=True)
    return passed


# --- preparazione ---------------------------------------------------------------------------------
def prepare_case(case: Case, repo_dir: Path, repo: RepoEngineering, runner_for, fetch_issue) -> Case:
    fix = git(repo_dir, "rev-parse", case.fix_sha).strip()
    parents = git(repo_dir, "rev-list", "--parents", "-n", "1", fix).split()[1:]
    if len(parents) != 1:
        return Case(**{**asdict(case), "fix_sha": fix, "usable": False, "note": "non e' uno squash merge"})
    base = parents[0]
    changed = git(repo_dir, "diff", "--name-only", base, fix).split()
    hidden = [p for p in changed if p.startswith("tests/") and p.endswith(".py")]
    title, body = fetch_issue(case.issue)
    result = Case(**{**asdict(case), "fix_sha": fix, "base_sha": base, "hidden_tests": hidden,
                     "issue_title": title, "issue_body": body})
    if not hidden:
        return Case(**{**asdict(result), "usable": False, "note": "nessun test Python nella fix"})
    try:
        base_image, fix_image = ensure_image(repo_dir, base, repo), ensure_image(repo_dir, fix, repo)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return Case(**{**asdict(result), "usable": False, "note": f"immagine non costruibile: {detail[0][:150]}"})
    with tempfile.TemporaryDirectory() as tmp:
        at_base = workspace_at(repo_dir, base, Path(tmp) / "base")
        overlay_tests(at_base, fix, hidden)
        before = passing_tests(runner_for(base_image), at_base, hidden)
        at_fix = workspace_at(repo_dir, fix, Path(tmp) / "fix")
        after = passing_tests(runner_for(fix_image), at_fix, hidden)
    f2p, p2p = sorted(after - before), sorted(after & before)
    note = "" if f2p else "nessun test FAIL_TO_PASS (es. test saltati senza emulatore)"
    return Case(**{**asdict(result), "f2p": f2p, "p2p": p2p, "usable": bool(f2p), "note": note})


# --- esecuzione ----------------------------------------------------------------------------------
@dataclass
class RunResult:
    case: str
    model: str
    repeat: int
    status: str
    resolved: bool = False
    f2p_passed: int = 0
    f2p_total: int = 0
    p2p_broken: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    attempts: int = 0
    files: int = 0
    lines: int = 0
    duration_s: float = 0.0
    error: str = ""


def run_case(case: Case, model_key: str, repeat: int, repo_dir: Path, repo: RepoEngineering,
             gateway: LLMGateway, runner_for, now: datetime, max_attempts: int = 2) -> RunResult:
    task_id = f"eval-{case.id}-{model_key}-r{repeat}-{now.strftime('%Y%m%dT%H%M%S')}"
    task = {"id": task_id, "repo": repo.repo, "issue_number": case.issue, "issue_title": case.issue_title,
            "issue_body": case.issue_body, "base_sha": case.base_sha}
    started = time.monotonic()
    image = ensure_image(repo_dir, case.base_sha, repo)
    with tempfile.TemporaryDirectory() as tmp:
        ws = workspace_at(repo_dir, case.base_sha, Path(tmp) / "ws")
        add_local_excludes(ws, repo.local_excludes + ("/" + JUNIT,))
        work = run_work(task, ws, repo, gateway, runner_for(image), Models(author=model_key, reviewer=None),
                        now, max_attempts=max_attempts)
        result = RunResult(case.id, model_key, repeat, work.status, f2p_total=len(case.f2p),
                           attempts=len(work.attempts), files=len(work.files), lines=work.lines_changed,
                           error=work.error)
        if work.status == "patch_ready":
            reset_workspace(ws)
            patch_file = Path(tmp) / "candidate.diff"
            patch_file.write_text(work.patch, encoding="utf-8", newline="")
            git(ws, "apply", "--whitespace=nowarn", str(patch_file))
            overlay_tests(ws, case.fix_sha, case.hidden_tests)
            passed = passing_tests(runner_for(image), ws, case.hidden_tests)
            result.f2p_passed = len(set(case.f2p) & passed)
            result.p2p_broken = len(set(case.p2p) - passed)
            result.resolved = result.f2p_passed == len(case.f2p) and result.p2p_broken == 0
    calls = gateway.ledger.calls_for_task(task_id)
    result.calls = len(calls)
    result.cost_usd = sum((c.get("actual_micros") or c["reserved_micros"]) for c in calls) / 1_000_000
    result.duration_s = round(time.monotonic() - started, 1)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_model: dict[str, dict[str, Any]] = {}
    for r in results:
        m = by_model.setdefault(r["model"], {"runs": 0, "resolved": 0, "patch_ready": 0, "cost": 0.0,
                                             "duration": 0.0, "lines": 0, "p2p_broken": 0})
        m["runs"] += 1
        m["resolved"] += int(r["resolved"])
        m["patch_ready"] += int(r["status"] == "patch_ready")
        m["cost"] += r["cost_usd"]
        m["duration"] += r["duration_s"]
        m["lines"] += r["lines"]
        m["p2p_broken"] += r["p2p_broken"]
    return by_model


def report(results: list[dict[str, Any]], cases: list[Case]) -> str:
    lines = ["# Evaluation engineering", "",
             "Punteggio deterministico: FAIL_TO_PASS e PASS_TO_PASS dei test della fix reale, applicati dopo la "
             "patch del candidato. Il repository e' pubblico: possibile contaminazione dei modelli.", "",
             "| Modello | Run | Risolti | Patch prodotte | Test gia' verdi rotti | Costo totale USD | Costo medio | "
             "Tempo medio s | Righe medie |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for model, m in sorted(summarize(results).items()):
        runs = m["runs"] or 1
        lines.append(f"| {model} | {m['runs']} | {m['resolved']} ({m['resolved'] / runs:.0%}) | {m['patch_ready']} | "
                     f"{m['p2p_broken']} | {m['cost']:.4f} | {m['cost'] / runs:.4f} | {m['duration'] / runs:.0f} | "
                     f"{m['lines'] / runs:.0f} |")
    models = sorted({r["model"] for r in results})
    lines += ["", "## Per caso", "", "| Caso | FAIL_TO_PASS | " + " | ".join(models) + " |",
              "|---|---:|" + "---|" * len(models)]
    for case in cases:
        cells = []
        for model in models:
            runs = [r for r in results if r["case"] == case.id and r["model"] == model]
            cells.append(" ".join(("✅" if r["resolved"] else ("🟡" if r["status"] == "patch_ready" else "❌"))
                                  for r in runs) or "—")
        lines.append(f"| {case.id} (PR #{case.pr}) | {len(case.f2p)} | " + " | ".join(cells) + " |")
    lines += ["", "✅ risolto · 🟡 patch prodotta ma test nascosti non superati · ❌ nessuna patch valida", ""]
    failures = [r for r in results if r["error"]]
    if failures:
        lines += ["## Errori", ""] + [f"- {r['case']} / {r['model']}: {r['error'][:200]}" for r in failures]
    return "\n".join(lines) + "\n"
