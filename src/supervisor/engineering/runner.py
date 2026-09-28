"""Esecuzione dei controlli sul codice modificato dal modello.

Il codice patchato e' codice non fidato: in CI gira in un container Docker senza rete, senza segreti e
senza le credenziali del job (`DockerRunner`). Le dipendenze si installano prima, in un'immagine
costruita dallo SHA di partenza, quando la patch non esiste ancora (`dockerfile()`).
`LocalRunner` serve per sviluppo e test: ambiente ripulito, ma nessun isolamento vero.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from supervisor.core.scrub import scrub
from supervisor.engineering.config import RepoEngineering

OUTPUT_TAIL = 4000
SAFE_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL")


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    output_tail: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class CheckRunner(Protocol):
    def run(self, command: str, workspace: Path, timeout: int) -> CheckResult: ...


def _tail(text: str) -> str:
    return scrub(text[-OUTPUT_TAIL:])


class LocalRunner:
    def run(self, command: str, workspace: Path, timeout: int) -> CheckResult:
        env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        env["HOME"] = str(workspace)
        try:
            result = subprocess.run(command, shell=True, cwd=workspace, env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return CheckResult(command, -1, _tail(output), timed_out=True)
        return CheckResult(command, result.returncode, _tail(result.stdout + result.stderr))


class DockerRunner:
    def __init__(self, image: str, memory: str = "4g", cpus: str = "2") -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus

    def command_line(self, command: str, workspace: Path) -> list[str]:
        return [
            "docker", "run", "--rm", "--network", "none", "--memory", self.memory, "--cpus", self.cpus,
            "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{getattr(os, 'getuid', lambda: 1000)()}:{getattr(os, 'getgid', lambda: 1000)()}",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{workspace.resolve()}:/work", "-w", "/work", self.image, "sh", "-c", command,
        ]

    def run(self, command: str, workspace: Path, timeout: int) -> CheckResult:
        try:
            result = subprocess.run(self.command_line(command, workspace), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return CheckResult(command, -1, "timeout", timed_out=True)
        return CheckResult(command, result.returncode, _tail(result.stdout + result.stderr))


def dockerfile(repo: RepoEngineering) -> str:
    """Immagine di controllo: dipendenze dello SHA di partenza, installate prima della patch."""
    lines = []
    if repo.node_image:
        lines.append(f"FROM {repo.node_image} AS node")
    lines += [f"FROM {repo.python_image}", "RUN apt-get update && apt-get install -y --no-install-recommends git "
              "&& rm -rf /var/lib/apt/lists/*"]
    if repo.node_image:
        # Solo node e npm: copiare tutto /usr/local sovrascriverebbe Python.
        lines += ["COPY --from=node /usr/local/bin/node /usr/local/bin/node",
                  "COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm",
                  "RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm"]
    # Solo i file delle dipendenze (se configurati): commit con le stesse dipendenze riusano la cache di Docker.
    copy = f"COPY {' '.join(repo.dependency_files)} ./" if repo.dependency_files else "COPY . /deps"
    lines += ["WORKDIR /deps", copy]
    lines += [f"RUN {command}" for command in repo.install]
    # I controlli girano con l'utente del runner: le dipendenze devono essere leggibili e le cache scrivibili.
    lines += ["RUN chmod -R a+rwX /deps", "WORKDIR /work"]
    return "\n".join(lines) + "\n"


def run_checks(runner: CheckRunner, workspace: Path, repo: RepoEngineering) -> list[CheckResult]:
    results = []
    for command in repo.checks:
        result = runner.run(command, workspace, repo.check_timeout_seconds)
        results.append(result)
        if not result.ok:
            break  # il primo controllo fallito basta: gli altri si rifanno al tentativo successivo
    return results
