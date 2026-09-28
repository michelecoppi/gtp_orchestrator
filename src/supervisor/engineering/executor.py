"""Executor: l'unico componente con permessi di scrittura su GitHub (branch e draft PR).

Non riceve chiavi AI e non esegue il codice patchato. Prima di scrivere ricontrolla tutto da solo:
- policy `create_branch_or_draft_pr` (serve `human` con approvazione valida, o `auto`);
- stato del task e hash della patch registrato dal worker;
- percorsi e dimensioni della patch (`check_patch`, senza fidarsi del worker);
- approvazione ancora valida (etichetta, approvatore, contenuto della issue invariato).
Poi crea il commit con la Git Data API (nessuna credenziale git su disco), il branch e la PR in bozza.

Idempotenza: branch con nome deterministico e marcatore `gtp-supervisor task=` nel commit e nella PR.
Prima di creare qualcosa si cerca se esiste gia' (per esempio dopo un timeout): se c'e' si riprende,
non si duplica. Nessun merge, mai.
"""
from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from supervisor.collectors.github import API, GitHubApi, GitHubError
from supervisor.collectors.http import HttpClient
from supervisor.core.policy import Policy
from supervisor.core.scrub import scrub, untrusted
from supervisor.engineering import approvals
from supervisor.engineering.config import EngineeringConfig, RepoEngineering
from supervisor.engineering.patch import PatchRejected, check_patch, git, sha256
from supervisor.engineering.tasks import TaskQueue

MARKER = "gtp-supervisor"


class ExecutorBlocked(RuntimeError):
    pass


class GitHubWriter:
    def __init__(self, http: HttpClient, token: str) -> None:
        if not token:
            raise ExecutorBlocked("nessun token di scrittura")
        self.http = http
        self.token = token

    def send(self, method: str, path: str, body: dict) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": f"Bearer {self.token}"}
        try:
            response = self.http.request(method, API + path, headers=headers, json_body=body)
        except Exception as exc:
            raise GitHubError(scrub(f"{method} {path}: {type(exc).__name__}: {exc}")) from exc
        if response.status >= 400:
            detail = response.body.get("message", "") if isinstance(response.body, dict) else ""
            raise GitHubError(scrub(f"{method} {path}: HTTP {response.status} {untrusted(detail, 120)}"))
        return response.body or {}


def marker(task_id: str, patch_sha: str) -> str:
    return f"{MARKER} task={task_id} patch={patch_sha[:16]}"


def branch_name(repo: RepoEngineering, issue: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", untrusted(title, 80).lower()).strip("-")[:40].strip("-") or "fix"
    return f"{repo.branch_prefix}/{issue}-supervisor-{slug}"


def safe_markdown(text: str) -> str:
    """Testo del modello nel corpo della PR: niente menzioni, HTML o commenti che imitino il marcatore."""
    return text.replace("@", "@​").replace("<", "&lt;").replace(">", "&gt;")


def pr_body(task: dict, meta: dict, base_sha: str) -> str:
    issue = task["issue_number"]
    checks = []
    for attempt in meta.get("attempts") or []:
        for check in attempt.get("checks") or []:
            state = "ok" if check["exit_code"] == 0 and not check.get("timed_out") else "fallito"
            checks.append(f"- tentativo {attempt['attempt']}: `{check['command']}` — {state}")
        if attempt.get("rejected"):
            checks.append(f"- tentativo {attempt['attempt']}: patch rifiutata dai controlli del supervisore")
    criteria = "\n".join(f"- {safe_markdown(untrusted(c, 200))}" for c in meta.get("plan", {}).get("acceptance_criteria", []))
    if meta.get("completes_issue"):
        issue_note = (f"Secondo il worker questa PR soddisfa tutti i criteri della issue. Se lo confermi, sostituisci "
                      f"con `Closes #{issue}`.")
    else:
        issue_note = f"Resta da fare: {safe_markdown(meta.get('remaining') or 'da valutare')}"
    review = meta.get("review") or {}
    notes = "\n".join(f"- {safe_markdown(n)}" for n in review.get("notes") or []) or "- (nessuna nota)"
    return f"""## Summary

{safe_markdown(meta.get('summary', ''))}

Preparata dal supervisore GTP su approvazione di {task['approval']['approver']} ({task['approval']['approved_at']}).
Worker: `{meta.get('author_model', '?')}`. Review automatica: `{review.get('verdict', 'n/d')}`.

**Approccio:** {safe_markdown(untrusted(meta.get('plan', {}).get('approach', ''), 800))}

**Criteri di accettazione proposti:**
{criteria or '- (nessuno)'}

## Issue

Refs #{issue}

{issue_note}

## Test plan

Controlli rapidi eseguiti dal worker in un container senza rete, sullo SHA di partenza `{base_sha[:7]}` con la patch:
{chr(10).join(checks) or '- (nessuno)'}

Piano del worker: {safe_markdown(meta.get('test_plan', ''))}

Il gate che conta e' la CI del repository sullo SHA di testa di questa PR: il supervisore la segue e lo segnala.

## Checklist

- [ ] Relevant tests added or updated and passing (da confermare con la CI sullo SHA di testa)
- [ ] Project item moved to `Review` (il supervisore non ha accesso al Project #2: da fare a mano)
- [ ] Docs updated if behavior, workflow, or architecture changed

<details><summary>Note della review automatica</summary>

{notes}

</details>

<!-- {marker(task['id'], meta['patch_sha256'])} -->
"""


@dataclass
class OpenedPR:
    number: int
    url: str
    head_sha: str
    branch: str
    reused: bool


def _changed_files(workspace: Path) -> list[tuple[str, str]]:
    git(workspace, "add", "-A")
    out = git(workspace, "diff", "--cached", "--name-status", "--no-renames", "HEAD")
    return [(line.split("\t", 1)[0], line.split("\t", 1)[1]) for line in out.splitlines() if "\t" in line]


def _file_mode(workspace: Path, path: str) -> str:
    out = git(workspace, "ls-files", "-s", "--", path, check=False).split()
    return out[0] if out and out[0] in ("100644", "100755") else "100644"


def open_pr(*, task: dict, queue: TaskQueue, api: GitHubApi, writer: GitHubWriter, config: EngineeringConfig,
            repo: RepoEngineering, policy: Policy, workspace: Path, patch_text: str, meta: dict,
            now: datetime) -> OpenedPR:
    task_id, issue, repo_name = task["id"], task["issue_number"], task["repo"]

    def block(reason: str) -> ExecutorBlocked:
        queue.transition(task_id, expect_states=("running",), now=now, note=reason, state="blocked", error=reason)
        return ExecutorBlocked(reason)

    decision = policy.decide("create_branch_or_draft_pr")
    if decision not in ("human", "auto"):
        raise block(f"policy: create_branch_or_draft_pr = {decision}")
    if task["state"] != "running" or task.get("phase") != "patch_ready":
        raise ExecutorBlocked(f"task in stato {task['state']}/{task.get('phase')}, atteso running/patch_ready")
    patch_sha = sha256(patch_text)
    if patch_sha != task.get("patch_sha256") or patch_sha != meta.get("patch_sha256"):
        raise block("la patch non corrisponde a quella registrata dal worker")
    try:
        check_patch(patch_text, repo)
    except PatchRejected as exc:
        raise block(f"patch rifiutata dall'executor: {exc}")
    reason = approvals.verify(api, config, task)
    if reason:
        raise block(reason)

    branch = branch_name(repo, issue, task["issue_title"])
    tag = marker(task_id, patch_sha)
    owner = repo_name.split("/")[0]
    existing = api.get(f"/repos/{repo_name}/pulls", {"head": f"{owner}:{branch}", "state": "all", "per_page": 10}).body
    ours = [pr for pr in existing or [] if tag in (pr.get("body") or "")]
    if ours:  # PR gia' aperta da un tentativo precedente (per esempio dopo un timeout): si riprende
        pr = ours[0]
        return _record(queue, task_id, now, OpenedPR(pr["number"], pr["html_url"], pr["head"]["sha"], branch, True))
    if existing:
        raise block(f"esiste gia' una PR sul branch {branch} non creata per questo task")

    try:
        head_sha = _ensure_branch(api, writer, workspace, repo_name, task, branch, patch_text, meta, tag)
    except (ExecutorBlocked, PatchRejected) as exc:
        raise block(str(exc))
    title = meta["commit_message"].split("\n", 1)[0]
    pr = writer.send("POST", f"/repos/{repo_name}/pulls", {
        "title": title, "head": branch, "base": repo.base_branch, "draft": True,
        "body": pr_body(task, meta, task["base_sha"]), "maintainer_can_modify": True,
    })
    return _record(queue, task_id, now, OpenedPR(pr["number"], pr["html_url"], head_sha, branch, False))


def _ensure_branch(api: GitHubApi, writer: GitHubWriter, workspace: Path, repo_name: str, task: dict,
                   branch: str, patch_text: str, meta: dict, tag: str) -> str:
    ref = _get_or_none(api, f"/repos/{repo_name}/git/ref/heads/{branch}")
    if ref is not None:
        sha = ref["object"]["sha"]
        commit = api.get(f"/repos/{repo_name}/git/commits/{sha}").body or {}
        if tag in (commit.get("message") or ""):
            return sha  # branch gia' creato da un tentativo precedente interrotto
        raise ExecutorBlocked(f"il branch {branch} esiste gia' e non e' del supervisore")

    if git(workspace, "rev-parse", "HEAD").strip() != task["base_sha"]:
        raise ExecutorBlocked("il workspace dell'executor non e' allo SHA di partenza")
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8", newline="") as fh:
        fh.write(patch_text)
        patch_file = fh.name
    try:
        git(workspace, "apply", "--check", "--whitespace=nowarn", patch_file)
        git(workspace, "apply", "--whitespace=nowarn", patch_file)
    finally:
        Path(patch_file).unlink(missing_ok=True)

    tree_items = []
    for status, path in _changed_files(workspace):
        if status not in ("A", "M"):
            raise ExecutorBlocked(f"modifica non ammessa ({status}) su {path}")
        content = base64.b64encode((workspace / path).read_bytes()).decode("ascii")
        blob = writer.send("POST", f"/repos/{repo_name}/git/blobs", {"content": content, "encoding": "base64"})
        tree_items.append({"path": path, "mode": _file_mode(workspace, path), "type": "blob", "sha": blob["sha"]})
    base = api.get(f"/repos/{repo_name}/git/commits/{task['base_sha']}").body
    tree = writer.send("POST", f"/repos/{repo_name}/git/trees", {"base_tree": base["tree"]["sha"], "tree": tree_items})
    commit = writer.send("POST", f"/repos/{repo_name}/git/commits", {
        "message": f"{meta['commit_message']}\n\n{tag}", "tree": tree["sha"], "parents": [task["base_sha"]],
    })
    writer.send("POST", f"/repos/{repo_name}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    return commit["sha"]


def _get_or_none(api: GitHubApi, path: str) -> Optional[dict[str, Any]]:
    try:
        return api.get(path).body
    except GitHubError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def _record(queue: TaskQueue, task_id: str, now: datetime, pr: OpenedPR) -> OpenedPR:
    queue.transition(task_id, expect_states=("running",), expect_phase="patch_ready", now=now,
                     note=f"draft PR #{pr.number} {'ritrovata' if pr.reused else 'aperta'}",
                     state="awaiting_approval", phase="ci_pending", pr_number=pr.number, pr_url=pr.url,
                     head_sha=pr.head_sha, branch=pr.branch)
    return pr

