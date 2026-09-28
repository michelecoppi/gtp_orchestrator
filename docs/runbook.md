# Runbook

## Stato del setup (28/09/2026)

| Passo | Stato |
|---|---|
| Repository `michelecoppi/gtp_orchestrator` (pubblico), CI verde | fatto |
| Progetto GCP `gtp-orchestrator` (n. 752943707933), Firestore Native `europe-west1`, senza fatturazione | fatto |
| Service account `supervisor@gtp-orchestrator.iam.gserviceaccount.com`: `datastore.user` sul proprio progetto, `datastore.viewer` su `guess-the-player-from-path-bot` | fatto |
| Workload Identity: pool `github`, provider `gtp-orchestrator`, condizione `assertion.repository=='michelecoppi/gtp_orchestrator'` | fatto |
| Secret `SUP_WIF_PROVIDER`, `SUP_WIF_SERVICE_ACCOUNT`; variabili progetti, chat admin, interruttori a `false` | fatto |
| Etichetta `supervisor:fix` sul repository del gioco | fatto |
| Budget AI approvato (1 USD/giorno, 15 USD/mese) | fatto |
| GitHub App `gtp-orchestrator` (ID 5113175), variabile `SUP_GITHUB_APP_ID` | fatto |
| Secret `SUP_GITHUB_APP_KEY`, `PROMO_APPROVAL_BOT_TOKEN`, `SUP_OPENROUTER_API_KEY` | **Michele** |
| `llm smoke` e `access_verified = true` nel catalogo, poi interruttori a `true` | dopo i secret |
| Secret `SUP_POSTHOG_PERSONAL_API_KEY` (PostHog, sola lettura delle query) per M4 | **Michele** |

## Setup iniziale (una volta, a cura di Michele)

1. **Progetto GCP dedicato** (es. `gtp-supervisor`): abilitare Firestore in modalità Native, regione `europe-west1`.
2. **Service account** `supervisor@gtp-supervisor.iam.gserviceaccount.com`:
   - `roles/datastore.user` sul progetto `gtp-supervisor`;
   - `roles/datastore.viewer` sul progetto del gioco (`guess-the-player-from-path-bot`), per leggere `promo_posts`.
     Nessun ruolo di scrittura sul gioco.
3. **Workload Identity Federation**: un provider che accetti solo `repository == michelecoppi/gtp_orchestrator`,
   con binding `roles/iam.workloadIdentityUser` sul service account.
4. **GitHub App** "gtp-supervisor" (privata), installata solo su `guess_the_player_from_the_path` e
   `promo_studio`, con permessi in sola lettura: *Actions*, *Contents*, *Issues*, *Pull requests*, *Metadata*.
   Da M3 servono anche *Contents* e *Pull requests* in scrittura, ma solo sul gioco: i workflow chiedono
   token ridotti per ogni job e la scrittura esiste solo nel job `open-pr` di `engineer.yml`.
5. **Secret e variabili** del repository `gtp_orchestrator`:

   | Tipo | Nome | Valore |
   |---|---|---|
   | variable | `SUP_ENABLED` | `false` all'inizio, `true` dopo il primo dry-run riuscito |
   | variable | `SUP_FIRESTORE_PROJECT` | `gtp-supervisor` |
   | variable | `SUP_GAME_FIRESTORE_PROJECT` | `guess-the-player-from-path-bot` (vuoto = Promo non osservato) |
   | variable | `SUP_ADMIN_CHAT_ID` | stessa chat di `PROMO_ADMIN_CHAT_ID` |
   | variable | `SUP_GITHUB_APP_ID` | id della GitHub App |
   | secret | `SUP_GITHUB_APP_KEY` | chiave privata della GitHub App |
   | secret | `SUP_WIF_PROVIDER` | risorsa del provider WIF |
   | secret | `SUP_WIF_SERVICE_ACCOUNT` | email del service account |
   | secret | `PROMO_APPROVAL_BOT_TOKEN` | token del bot approvazioni di Promo (ADR 0002) |
   | variable | `SUP_AI_ENABLED` | `false` finché non si completano i passi di "Attivare l'AI" |
   | secret | `SUP_OPENROUTER_API_KEY` | chiave OpenRouter dedicata al supervisore, con limite di spesa sulla chiave (provider attivo); `SUP_OPENAI_API_KEY` / `SUP_ANTHROPIC_API_KEY` solo per un eventuale passaggio alle API dirette |
   | variable | `SUP_ENGINEER_ENABLED` | `false` finché non si completano i passi di "Attivare il worker engineering" |

6. **Primo avvio:** *Actions → Observe → Run workflow* con `dry_run = true`. Controllare il log e l'artifact
   `brief-*`. Poi impostare `SUP_ENABLED=true`.

## Uso locale

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt -e .
export SUP_GITHUB_TOKEN=$(gh auth token) SUP_STORE=sqlite SUP_ENABLED=true
python -m supervisor doctor
python -m supervisor observe
python -m supervisor report
python -m supervisor status
python -m supervisor replay --fixtures tests/fixtures/github_replay.json --promo-fixture tests/fixtures/promo_posts.json
```

## Situazioni tipiche

| Sintomo | Che cosa fare |
|---|---|
| `saltato: un altro giro di observe e' in corso` | Normale se due giri si sovrappongono. Se si ripete, `status` mostra owner e scadenza del lock; il lease scade da solo dopo 15 minuti. |
| Sorgente `incompleta` o `non disponibile` nel brief | Leggere l'errore nella sezione "Completezza" (token scaduto, permessi, rate limit). I cursori di quella sorgente non sono avanzati: al giro successivo non si perde nulla. |
| `status` mostra notifiche "in sospeso" | Un invio è stato interrotto a metà. Controllare su Telegram se il messaggio è arrivato. Non viene ripetuto in automatico. |
| Finding che non si chiude | Le regole di stato si chiudono solo con dati completi della sorgente; quelle di workflow con una run verde successiva sul branch di default. |
| Emergenza | Impostare `SUP_ENABLED=false`: resteranno attivi solo `doctor` e i dry-run. |
| Triage `blocked` | Il motivo è scritto accanto: budget non approvato, modello non verificato, tetto raggiunto, `SUP_AI_ENABLED` spento. Nessuna chiamata è partita. |
| `budget` mostra "da riconciliare" | Una chiamata è finita in timeout o in crash. Controllare sulla dashboard del provider se è stata addebitata, poi `budget --reconcile <call_id> --actual <USD>` oppure `--release`. Fino ad allora quel task non riparte. |
| Emergenza AI | Impostare `SUP_AI_ENABLED=false` (variabile del repository): osservazione e report continuano. |

## Attivare l'AI (M2)

Da fare in quest'ordine. Ogni passo è una decisione di Michele.

1. **Chiavi dedicate** al supervisore, con un limite di spesa impostato anche sul provider quando possibile.
   Secret: `SUP_OPENAI_API_KEY` e, quando serviranno, `SUP_ANTHROPIC_API_KEY` e `SUP_GEMINI_API_KEY`.
2. **Approvare il budget** in `config/budget.toml`: `approved = true`, `approved_by`, `approved_on`, tramite commit
   con review.
3. **Verificare l'accesso** al modello del triage, in locale o con un dispatch:
   ```bash
   SUP_ENABLED=true SUP_AI_ENABLED=true OPENROUTER_API_KEY=... python -m supervisor llm smoke gpt-6-luna@openrouter
   ```
   Controllare che il modello restituito e il costo siano quelli attesi, poi impostare `access_verified = true`
   per quel modello in `config/models.toml` (commit con review). Verificare anche l'id `litellm_model`.
4. **Provare in dry-run**: `python -m supervisor triage --dry-run` mostra, per ogni finding, la stima di costo e gli
   eventuali blocchi.
5. Impostare la variabile `SUP_AI_ENABLED=true`. Da quel momento il workflow Observe tria i finding nuovi e il
   brief mostra proposte e budget.

Prima di cambiare un prezzo o un modello: aggiornare `config/models.toml` (nuova `version`, fonte e data di
verifica). Il router non cambia provider né prezzi da solo.

## Attivare il worker engineering (M3)

Prerequisiti: AI attiva (sezione precedente) e accesso verificato a `gpt-6-sol` (autore) e, se possibile, a
`claude-sonnet-5` (reviewer): `llm smoke gpt-6-sol@openrouter` e `llm smoke claude-sonnet-5@openrouter`.

1. Concedere alla GitHub App *Contents: write* e *Pull requests: write*, e accettare i nuovi permessi
   sull'installazione.
2. Creare nel repository del gioco l'etichetta `supervisor:fix`.
3. Impostare la variabile `SUP_ENGINEER_ENABLED=true`. Il workflow Engineer gira ogni 2 ore di giorno, oppure a
   mano da *Actions → Engineer*.
4. Per affidare un fix: scrivere la issue con criteri di accettazione chiari e aggiungere l'etichetta. Il
   supervisore:
   - reclama la issue;
   - prepara la patch e la verifica nel container;
   - apre una **draft PR** `fix/<n>-supervisor-...` con `Refs #n`;
   - avvisa su Telegram quando la CI sullo SHA di testa è verde.
5. Review e merge restano tuoi. Se tutti i criteri sono soddisfatti, sostituisci `Refs` con `Closes`. Sposta a
   mano l'elemento del Project #2: il supervisore non può farlo.

| Situazione | Che cosa fare |
|---|---|
| Vuoi fermare un task | Togli l'etichetta: approvazione revocata, il task si blocca prima di scrivere. |
| Hai modificato la issue dopo l'etichetta | L'approvazione non vale più. Togli e rimetti l'etichetta per un task nuovo. |
| `engineer list` mostra `blocked` o `failed` | Il motivo è accanto: patch fuori dai limiti, controlli falliti dopo 2 tentativi, review bloccante, budget. L'artifact `engineer-<task>` del workflow contiene `result.json` e l'eventuale patch. |
| Il repository resta "occupato" | Un solo task attivo per repository, finché la PR è aperta. Unisci o chiudi la PR per liberarlo. |
| Emergenza | `SUP_ENGINEER_ENABLED=false` ferma il workflow. La policy `create_branch_or_draft_pr = "deny"` blocca anche l'executor. |

## Prodotto e growth (M4)

- **Metriche**: con `SUP_POSTHOG_PERSONAL_API_KEY` impostata, Observe aggiunge al brief la sezione "Prodotto".
  Contiene volumi, attivazione per canale, completamento Daily, ritorno a 7 giorni, North Star e referral, ognuno
  con conteggi e stato della lettura. PostHog si interroga al massimo ogni 20 ore.
- **Review settimanale**: il workflow *Growth* gira il lunedì alle 08:30, oppure a mano con `dry_run` per la
  stima del costo. Produce una proposta con i campi della specifica (sez. 11) e la fattibilità calcolata dal
  codice. Se riguarda la promozione, aggiunge una bozza di brief per Promo con `campaign_id`. Per vedere le
  bozze: `python -m supervisor growth briefs`.
- **La chiave PostHog** va creata in PostHog (*Settings → Personal API keys*) con il solo permesso di lettura
  delle query, sul progetto 275711.
- **Dati di qualità**: se compare il finding `analytics_data_quality` (per esempio `bot_started` assente), si
  sistema la raccolta prima di leggere il funnel.
- **Proposte per gli altri repository**: `docs/proposals/game-campaign-id.md` e
  `docs/proposals/promo-brief-intake.md`, aperte il 29/09/2026 come [gioco #218](https://github.com/michelecoppi/guess_the_player_from_the_path/issues/218) e
  [promo_studio #1](https://github.com/michelecoppi/promo_studio/issues/1). Nuove proposte si aprono solo dopo averle approvate.

## Costi attesi
- Actions (repository privato): circa 7 giri al giorno × 1–2 minuti ≈ 200–400 minuti al mese.
- Firestore: poche centinaia di scritture al giorno, entro la quota gratuita (da verificare sul billing).
- Engineering: circa 4 chiamate per task (piano, patch, eventuale secondo tentativo, review); con GPT-6 Sol la stima
  pessimista è di pochi centesimi per task. Il tetto giornaliero di 1 USD ferma i task che non ci stanno.
- AI: triage con GPT-6 Luna a circa 0,0004 USD per finding (stima pessimista con 600 token di output). Il tetto
  resta 15 USD al mese e 1 USD al giorno.
