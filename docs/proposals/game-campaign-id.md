# Proposta di issue — gioco: `campaign_id` nei link di campagna

**Repository:** `michelecoppi/guess_the_player_from_the_path` · **Tipo:** feature · **Stato:** bozza, non aperta

---

**Titolo:** feat(analytics): campaign_id nei link `/start` di campagna

## Problema
`acquisition_channel` distingue il canale (`src_tiktok`, `src_telegram_channel`, ...) ma non il singolo
contenuto o la campagna. Il supervisore (gtp_orchestrator, M4) e Promo Studio non possono dire quale video o
post ha portato nuovi giocatori attivati: si confrontano solo canali interi, con volumi spesso sotto la soglia
di 30 utenti. La specifica del supervisore (sez. 3) chiede proprietà come `campaign_id` e `content_id`.

## Proposta
- Formato del parametro: `src_<fonte>-<campagna>`, con `<fonte>` in `CAMPAIGN_SOURCES` (invariato) e
  `<campagna>` = `[a-z0-9-]{1,24}`. Esempio: `src_tiktok-2026w40-whois`. Il totale resta entro i 64 caratteri
  ammessi da Telegram per `start`.
- `handlers/start_handler.py::acquisition_channel`: `src_tiktok-2026w40-whois` produce `acquisition_channel =
  tiktok` (come oggi) e una nuova proprietà `campaign_id = 2026w40-whois` su `bot_started`.
- La campagna si valida con la regex e non è mai testo libero. Un valore non valido dà `campaign_id` assente e
  canale invariato, mai `other` per colpa della sola campagna.
- Senza suffisso tutto resta com'è: i link esistenti non cambiano.
- `docs/product-analytics.md` §6 (tabella di `bot_started`) e §15b: aggiungere `campaign_id` come raggruppamento
  opzionale dell'attivazione.

## Fuori scopo
- `content_id` separato: per ora la campagna identifica il contenuto (un brief corrisponde a una campagna).
- Salvare la campagna nel profilo utente.

## Criteri di completamento
- [ ] `src_<fonte>-<campagna>` valido → `acquisition_channel=<fonte>` e `campaign_id=<campagna>` su `bot_started`.
- [ ] Campagna non valida o troppo lunga → nessun `campaign_id`, canale corretto; nessun testo libero inviato.
- [ ] Link senza suffisso invariati (test di regressione su `ACQUISITION_CHANNELS`).
- [ ] Allow-list delle proprietà in `services/product_analytics.py` aggiornata; documentazione §6 e §15b.
