# ADR 0004 — Worker engineering: approvazione con etichetta, worker isolato, executor separato

**Stato:** accettata · **Data:** 28/09/2026

## Contesto
M3 chiede un worker che corregga problemi piccoli e apra draft PR (specifica, sez. 10), con:
- controlli sullo SHA finale;
- nessun merge automatico;
- credenziali separate tra chi esegue codice e chi scrive su GitHub (sez. 8).

Il protocollo del gioco (`AGENTS.md`, `docs/agent-protocol.md`) aggiunge vincoli propri:
- i compiti li decide Michele (Project #2);
- una issue corrisponde a una PR, con `Closes`/`Refs`;
- la CI va verificata sullo SHA di testa esatto;
- nessun merge da parte degli agenti.

Resta da risolvere anche la questione delle approvazioni sul bot Telegram condiviso (ADR 0002).

## Decisione

### 1. Approvazione su GitHub
Michele aggiunge l'etichetta `supervisor:fix` a una issue aperta. L'approvazione vale solo se:
- l'evento `labeled` più recente è di un approvatore (`config/engineering.toml`);
- titolo e corpo della issue non sono cambiati dall'approvazione (hash registrato).

Togliere l'etichetta o chiudere la issue revoca l'approvazione; rimetterla crea un task nuovo. Il supervisore
non crea issue né sceglie compiti: così resta coerente con il Project #2. Telegram resta di sola notifica, e non
serve più leggere gli update del bot di Promo.

### 2. Coda con lease
I task stanno nello stato del supervisore (`tasks`, `eng_locks`):
- un solo task attivo per repository;
- lease di 60 minuti;
- al massimo `max_fix_attempts` riprese.

Gli stati sono quelli della specifica; la fase indica il punto del lavoro.

### 3. Tre job con permessi diversi (`engineer.yml`)

| Job | Token GitHub | Chiavi AI | Esegue codice patchato |
|---|---|---|---|
| `claim` | lettura | no | no |
| `work` | lettura | sì | sì, ma solo in Docker `--network none`, `--cap-drop ALL`, senza variabili d'ambiente né credenziali |
| `open-pr` | scrittura (contents + pull requests, solo sul gioco) | no | no |

Le dipendenze si installano nell'immagine dallo SHA di partenza, prima che la patch esista.

### 4. Il modello propone, il codice applica
- **Modifiche:** il modello propone sostituzioni esatte (`search` presente una sola volta).
- **Limiti:** percorsi vietati (workflow, segreti, dipendenze, regole, istruzioni), niente file binari, niente
  rinomine o cancellazioni, al massimo 5 file e 200 righe.
- **Controlli:** i controlli rapidi seguono l'ordine della CI del gioco: ruff, mypy, build del frontend,
  pytest.
- **Tentativi:** al secondo tentativo il modello riceve l'esito dei controlli come dato non fidato.
- **Review:** la fa un modello di un altro provider (Sonnet 5 per una patch di GPT-6 Sol). Se non è
  disponibile, la PR lo dichiara e la review resta umana.

### 5. Controlli indipendenti dell'executor
Prima di scrivere, l'executor ricontrolla da solo:
- la policy;
- l'hash della patch registrato dal worker;
- percorsi e dimensioni della patch;
- la validità dell'approvazione.

Crea poi il commit con la Git Data API (nessuna credenziale git su disco) su un branch deterministico,
`fix/<issue>-supervisor-<slug>`, e apre la PR in bozza con il template del gioco, usando sempre `Refs #N`.
`Closes` lo decide Michele. Un marcatore nel commit e nella PR rende l'operazione ripetibile dopo un
timeout: se branch o PR esistono già, li riprende.

### 6. Il gate è la CI sullo SHA di testa
`engineer verify` (dal workflow Observe) segue la PR:
- CI verde sullo SHA di testa → notifica "pronta per la tua review";
- nuovo push → la CI precedente non vale più;
- merge o chiusura → task chiuso e repository libero.

## Limiti noti
- Il supervisore non può spostare gli elementi del Project #2: una GitHub App non accede ai Project di un
  utente. La PR lo segnala nella checklist.
- Il worker vede al massimo 8 file (60 KB ciascuno): è pensato per fix piccoli, non per evolutive.
- I controlli nel container non sostituiscono la CI completa: mancano l'emulatore Firestore, gli audit e la
  coverage.
- Il contratto dell'immagine Docker è stato verificato localmente solo con gli stessi comandi fuori da
  Docker. Il primo run in Actions va seguito.
