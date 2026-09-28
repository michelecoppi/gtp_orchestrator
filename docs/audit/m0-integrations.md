# M0 — Audit delle integrazioni (sola lettura)

**Data:** 28 settembre 2026 · **Base:** `guess_the_player_from_the_path@71a04ca` (main), `promo_studio@8f5e2c2`
**Metodo:** lettura del codice nei due repository e chiamate GitHub REST in sola lettura. Non sono stati aperti
`.env` né chiavi di servizio. Il riferimento di partenza è la specifica `GTP_Supervisor_Analisi_V1.md`.

## Tabella delle integrazioni

| Integrazione | Riferimento | Contratto disponibile | Permessi necessari al supervisore | Stato |
|---|---|---|---|---|
| CI del gioco | `.github/workflows/ci.yml` | Job `test`: ruff, mypy, typecheck/build frontend, dataset, pytest con coverage ≥70%, audit dipendenze, detect-secrets, emulatore Firestore | `actions:read` | Osservata in M1 |
| Deploy del gioco | `.github/workflows/deploy.yml` | `workflow_run` dopo una CI verde su main; deploy su Cloud Run solo della testa di main con CI verde sullo stesso SHA | `actions:read` | Osservata in M1 |
| Backup e restore del gioco | `backup.yml` (lunedì), `restore-verification.yml` (martedì) | Export Firestore e prova di ripristino sull'emulatore | `actions:read` | Osservate in M1 |
| Issue, PR, Project | `AGENTS.md`, `docs/agent-protocol.md`, `docs/github-workflow.md` | Project #2 è la fonte di verità; WIP 2; branch `<type>/<issue>-<slug>`; `Closes`/`Refs #N`; CI verde sullo SHA di testa; nessun merge senza Michele | `issues:read`, `pull_requests:read` | Osservati in M1 (Project: da M3) |
| CI e cron di Promo | `promo_studio/.github/workflows/{ci,promo}.yml` | ruff + pytest; cron per bozze, approvazione, pubblicazione, report | `actions:read` | Osservati in M1 |
| Coda contenuti | `promo_studio/promo/{models,queue,store}.py` | Collection `promo_posts`: `id`, `status` (draft/approved/rejected/published/failed), `created_at`, `scheduled_for`, `published_at`, `attempts`, `error`, `history` | `datastore.viewer` sul progetto del gioco | Osservata in M1 (se configurata) |
| Analytics prodotto | `services/product_analytics.py`, `services/product_analytics_query.py`, `docs/product-analytics.md` | Eventi PostHog lato server in allow-list; HogQL in sola lettura con `CORE_METRICS` | `POSTHOG_PERSONAL_API_KEY` in sola lettura | Da M4 |
| Attribuzione | `handlers/start_handler.py`, `CAMPAIGN_SOURCES` | `/start` → `acquisition_channel` (`src_<fonte>` solo per le fonti in allow-list, `ref_`, `duel_`, `lega_`, `direct`, `other`) | — | Da M4 |
| Feature flag ed esperimenti | `services/feature_flags.py`, `services/experiments.py` | `admin_settings/*`; assegnazione deterministica; evento `experiment_assigned` | Nessuno in M1 (le modifiche restano umane) | Fuori da M1 |
| Osservabilità runtime | `services/observability.py`, `docs/observability.md` | Sentry opzionale (`SENTRY_DSN`), log strutturati, `/app/api/perf`, `/app/api/client-error` | Da definire | Fuori da M1 |
| Notifiche | `promo_studio/promo/approvals.py` | Bot approvazioni (`PROMO_APPROVAL_BOT_TOKEN`), chat admin `PROMO_ADMIN_CHAT_ID` | Stesso token, solo `sendMessage` | M1 (ADR 0002) |

## Differenze rispetto alla specifica

1. **Nessun `campaign_id`, `content_id` né `variant` sugli eventi di ingresso.** L'attribuzione è per canale
   (`src_<fonte>`); aggiungere una campagna richiede una modifica al codice. Il funnel identificabile della sez. 3
   si può misurare per canale, non per singolo contenuto. → Proposta per M4: `src_<fonte>_<campagna>` con allow-list
   e proprietà `campaign_id`.
2. **Promo Studio non usa LLM e non ha brief.** Le caption vengono da template versionati e i video da un renderer
   deterministico. Il "brief strutturato" della sez. 11 richiede un nuovo punto d'ingresso in Promo (`brief_id`),
   da progettare in M4 insieme a Michele.
3. **Il costo della promozione si inserisce a mano** in `costs.json`, con euro per settimana e per canale. Non c'è
   contabilità AI da riusare: la prenotazione del budget (M2) è tutta da costruire.
4. **Il gioco impone regole precise agli agenti.** Il supervisore non può creare task "Ready" né spostare elementi
   del Project. In M3 potrà solo proporre issue in Backlog con l'etichetta `supervisor` e aprire draft PR che
   rispettino branch, template e `Closes`/`Refs`.
5. **Il gioco esiste già come pipeline di esperimenti** (flag e registry), ma senza test di significatività. La
   sez. 11 della specifica resta valida: con pochi utenti niente "vincitori".
6. **Promo Studio legge il gioco importando codice** (`GAME_REPO_PATH`). Il supervisore non lo fa: legge solo la coda
   `promo_posts` e le API GitHub, così non dipende dalle versioni interne dei due repository.
7. Il clone locale di `promo_studio` ha `origin/HEAD` su un branch `claude/...`; su GitHub il branch di default è
   `main` (verificato con l'API). La regola `default_branch_unexpected` lo sorveglia.

## Decisioni aperte (input delle tranche successive)

- Progetto GCP dedicato: nome, billing, regione Firestore (proposta: `gtp-supervisor`, `europe-west1`).
- GitHub App: creazione e installazione sui due repository (permessi in `docs/runbook.md`).
- Protocollo delle approvazioni sul bot condiviso (M2+, ADR 0002).
- Formato di `campaign_id` nel gioco e di `brief_id` in Promo (M4).
- Budget AI approvato e chiavi dedicate per OpenAI, Anthropic e Gemini (M2).
