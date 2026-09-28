# Runbook

## Setup iniziale (una volta, a cura di Michele)

1. **Progetto GCP dedicato** (es. `gtp-supervisor`): abilitare Firestore in modalità Native, regione `europe-west1`.
2. **Service account** `supervisor@gtp-supervisor.iam.gserviceaccount.com`:
   - `roles/datastore.user` sul progetto `gtp-supervisor`;
   - `roles/datastore.viewer` sul progetto del gioco (`guess-the-player-from-path-bot`), per leggere `promo_posts`.
     Nessun ruolo di scrittura sul gioco.
3. **Workload Identity Federation**: un provider che accetti solo `repository == michelecoppi/gtp_supervisor`,
   con binding `roles/iam.workloadIdentityUser` sul service account.
4. **GitHub App** "gtp-supervisor" (privata), installata solo su `guess_the_player_from_the_path` e
   `promo_studio`, con permessi in sola lettura: *Actions*, *Contents*, *Issues*, *Pull requests*, *Metadata*.
   Nessun permesso di scrittura in M1.
5. **Secret e variabili** del repository `gtp_supervisor`:

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

## Costi attesi M1
- Actions (repository privato): circa 7 giri al giorno × 1–2 minuti ≈ 200–400 minuti al mese.
- Firestore: poche centinaia di scritture al giorno, entro la quota gratuita (da verificare sul billing).
- Nessuna chiamata AI.
