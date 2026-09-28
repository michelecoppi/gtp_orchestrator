"""Modifiche proposte dal modello, applicate e controllate dal codice.

Il modello non scrive file liberamente: propone sostituzioni `{path, search, replace}`.
- `search` deve comparire esattamente una volta nel file esistente;
- `search` vuoto crea un file nuovo, che non deve esistere;
- percorsi vietati, assoluti o con `..` sono rifiutati (config/engineering.toml);
- la patch finale e' `git diff` del workspace, con limiti su file e righe toccate.
Lo stesso controllo di percorsi e dimensioni lo ripete l'executor sul diff, senza fidarsi del worker.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from supervisor.engineering.config import RepoEngineering


class PatchRejected(ValueError):
    pass


@dataclass(frozen=True)
class Edit:
    path: str
    search: str
    replace: str


@dataclass(frozen=True)
class PatchStats:
    files: tuple[str, ...]
    added: int
    deleted: int

    @property
    def lines(self) -> int:
        return self.added + self.deleted


def git(workspace: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    if check and result.returncode != 0:
        raise PatchRejected(f"git {args[0]} fallito: {result.stderr.strip()[:300]}")
    return result.stdout


def add_local_excludes(workspace: Path, patterns: tuple[str, ...]) -> None:
    """Esclusioni solo locali (.git/info/exclude): file prodotti dai controlli che non devono entrare nella patch."""
    if not patterns:
        return
    exclude = Path(git(workspace, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else workspace / exclude
    current = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [p for p in patterns if p not in current]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(current + missing) + "\n", encoding="utf-8")


def reset_workspace(workspace: Path) -> None:
    git(workspace, "reset", "-q", "--hard")
    git(workspace, "clean", "-fdq")


def apply_edits(workspace: Path, edits: list[Edit], repo: RepoEngineering) -> None:
    if not edits:
        raise PatchRejected("nessuna modifica proposta")
    root = workspace.resolve()
    for edit in edits:
        problem = repo.path_problem(edit.path)
        if problem:
            raise PatchRejected(problem)
        target = (root / edit.path).resolve()
        if root not in target.parents:
            raise PatchRejected(f"percorso fuori dal repository: {edit.path}")
        if edit.search == "":
            if target.exists():
                raise PatchRejected(f"{edit.path} esiste gia': usare una sostituzione")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.replace, encoding="utf-8", newline="")
            continue
        if not target.is_file():
            raise PatchRejected(f"{edit.path} non esiste")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PatchRejected(f"{edit.path} non e' un file di testo UTF-8") from exc
        count = text.count(edit.search)
        if count != 1:
            raise PatchRejected(f"{edit.path}: il testo da sostituire compare {count} volte (serve esattamente 1)"
                                + _where(text, edit.search, count))
        target.write_text(text.replace(edit.search, edit.replace, 1), encoding="utf-8", newline="")


def _where(text: str, search: str, count: int) -> str:
    """Aiuto per il tentativo successivo: righe delle occorrenze, o la riga piu' simile se non ce n'e' nessuna."""
    if count > 1:
        lines: list[str] = []
        index = text.find(search)
        while index != -1 and len(lines) < 10:
            lines.append(str(text.count("\n", 0, index) + 1))
            index = text.find(search, index + 1)
        return f"; occorrenze alle righe {', '.join(lines)}: includere piu' contesto per renderlo unico"
    first = next((line.strip() for line in search.splitlines() if line.strip()), "")
    for number, line in enumerate(text.splitlines(), 1):
        if first and first in line:
            return f"; la prima riga cercata compare alla riga {number}: copiare il testo esatto (spazi inclusi)"
    return "; nessuna riga simile: rileggere il file"


def diff(workspace: Path) -> str:
    git(workspace, "add", "-A", "-N")  # i file nuovi entrano nel diff senza essere indicizzati
    return git(workspace, "diff", "--no-color", "--no-ext-diff", "--binary", "HEAD")


_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def stats_from_diff(patch: str) -> PatchStats:
    files: list[str] = []
    added = deleted = 0
    for line in patch.splitlines():
        match = _DIFF_FILE.match(line)
        if match:
            for path in (match.group(1), match.group(2)):
                if path not in files:
                    files.append(path)
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return PatchStats(tuple(files), added, deleted)


def check_patch(patch: str, repo: RepoEngineering) -> PatchStats:
    """Controlli indipendenti dal worker: percorsi, file binari, dimensioni."""
    if not patch.strip():
        raise PatchRejected("patch vuota")
    if "GIT binary patch" in patch or "\nBinary files " in patch:
        raise PatchRejected("la patch contiene file binari")
    if re.search(r"^(old|new) mode |^deleted file mode |^rename from |^similarity index ", patch, re.MULTILINE):
        raise PatchRejected("la patch cambia permessi, rinomina o cancella file")
    stats = stats_from_diff(patch)
    for path in stats.files:
        problem = repo.path_problem(path)
        if problem:
            raise PatchRejected(problem)
    if len(stats.files) > repo.max_files_changed:
        raise PatchRejected(f"{len(stats.files)} file toccati, massimo {repo.max_files_changed}")
    if stats.lines > repo.max_lines_changed:
        raise PatchRejected(f"{stats.lines} righe cambiate, massimo {repo.max_lines_changed}")
    return stats


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
