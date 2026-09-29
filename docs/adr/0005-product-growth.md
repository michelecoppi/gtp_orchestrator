# ADR 0005 — Prodotto e growth: metriche del gioco, fattibilità calcolata, brief in bozza

**Stato:** accettata · **Data:** 29/09/2026

## Contesto
M4 della specifica chiede un funnel verificato e brief per Promo in bozza. I criteri: metriche riconciliabili,
coorti mature e gestione dei dati mancanti.

Il gioco ha già definizioni, finestre, soglie minime (30 utenti) e una procedura di review settimanale
(`docs/product-analytics.md` §12 e §15b). La baseline del 27/09 registra anche un problema di qualità dei dati:
`bot_started` è assente mentre gli eventi Daily arrivano.

## Decisione
1. **Stesse definizioni del gioco.** Le query HogQL riprendono le insight salvate: filtro
   `environment=production`, `is_new_user = 'true'` come stringa, primo evento di sempre, coorti mature. A
   queste si aggiunge la North Star della specifica: nuovi ingressi che completano una Daily nella Mini App
   entro 7 giorni.
2. **Ogni tasso porta con sé conteggi e stato.** Gli stati sono `ok`, `sotto_soglia` (solo descrittivo),
   `non_disponibile` (nessun denominatore, mai 0%) e `immatura`.
3. **Collector con cache.** PostHog si interroga al massimo ogni 20 ore; nei giri intermedi i fatti vengono dal
   cursore, con la data di raccolta. Se una query fallisce non si aggiorna la cache.
4. **La fattibilità la decide il codice.** Il modello propone metrica e miglioramento minimo; il codice calcola
   la numerosità dal tasso di partenza reale (solo sopra soglia) e dal volume settimanale. Oltre 8 settimane la
   proposta diventa qualitativa. I numeri nel testo che non compaiono nei dati vengono segnalati.
5. **Una proposta a settimana, idempotente.** È salvata in `proposals/{settimana ISO}`.
6. **Brief per Promo in bozza.** Contiene solo fatti verificati (`config/promo_facts.toml`, ognuno con la sua
   fonte), valori da elenchi chiusi (lingua, formato, canale) e `campaign_id` generato dal codice. Il supporto
   nel gioco e in Promo è proposto come issue (`docs/proposals/`), non implementato da qui: ogni repository
   segue il proprio processo.
7. **Qualità dei dati come finding.** Un evento chiave assente è un finding (`analytics_data_quality`), non un
   crollo del funnel.

## Aggiornamento 29/09/2026 — giro chiuso con gioco e Promo
- Gioco [#218](https://github.com/michelecoppi/guess_the_player_from_the_path/issues/218): `bot_started` porta `campaign_id` dai link `src_<fonte>-<campagna>`, con
  campagna `[a-z0-9-]{1,24}`. Il supervisore misura l'attivazione a 24 ore per campagna.
- Promo [#1](https://github.com/michelecoppi/promo_studio/issues/1): `python -m promo brief-import <file.json>`. Il supervisore scrive il file (artifact
  del workflow Growth, `growth briefs --export`) con i soli campi letti da Promo e con valori dei suoi elenchi:
  - canali `tiktok`, `telegram_channel`, `x`;
  - formati senza `solution`.

  Il link con la campagna è attivo solo con `PROMO_CAMPAIGN_LINKS=true` in Promo.
- Differenza da sistemare in Promo: `brief-import` accetta `campaign_id` con maiuscole e `_`, che il gioco non
  attribuisce (l'esempio `2026w40_it_tiktok` perderebbe la campagna). Gli id generati dal supervisore rispettano
  la regola del gioco, verificata da un test.

## Aggiornamento 29/09/2026 — brief pubblicati nello stato ([issue #9](https://github.com/michelecoppi/gtp_orchestrator/issues/9))
Il passaggio manuale (scaricare il file dall'artifact e lanciare `brief-import`) diventa un contratto di sola
lettura fra i due Firestore.
- **Il supervisore pubblica, non consegna.** La review scrive `promo_briefs/{campaign_id}` nel proprio
  Firestore con `status: proposed`, `schema_version: 1`, `expires_at` (+14 giorni) e `brief`, che è esattamente il
  JSON di `brief-import` (`promo_import_payload`). Il formato è fissato da `tests/fixtures/promo_brief_doc.json`,
  copiato nei test di Promo.
- **Scrittura solo se assente** (transazione `create`): rilanciare la review, anche con `force`, non duplica il
  brief e non ne cambia il contenuto già visto da Promo.
- **Lo stato della decisione vive in Promo**, non qui: `promo_brief_decisions/{campaign_id}` nel Firestore del
  gioco (`asked` → `used` | `discarded`, con chi ha deciso e quando). Così il supervisore non riceve permessi di
  scrittura da Promo né Promo dal supervisore. Il supervisore legge le decisioni con il collector Promo (ha già
  `datastore.viewer` sul progetto del gioco) e le mostra nel brief quotidiano.
- **Promo legge con `roles/datastore.viewer`** sul progetto `gtp-orchestrator`, dato a mano da Michele (runbook,
  M4). Il ruolo copre tutto il Firestore del supervisore: non contiene segreti, ma budget, eventi e finding
  diventano leggibili da Promo. Alternativa scartata per semplicità: un secondo database Firestore solo per i
  brief.
- **Conservazione:** `expires_at` (14 giorni) dice a Promo fino a quando proporre il brief; il documento resta 90
  giorni (`RETENTION_DAYS`, `prune`), per rileggere la campagna nelle review successive.
- Il file nell'artifact di *Growth* resta come via manuale.

## Conseguenze
- Con i volumi attuali (pochi utenti al giorno) quasi tutte le proposte saranno qualitative o riguarderanno i
  dati. È il risultato corretto: nessun "vincitore" su numeri piccoli.
- Serve una Personal API Key PostHog in sola lettura (`SUP_POSTHOG_PERSONAL_API_KEY`). Senza chiave la sorgente
  risulta "non configurata".
- Le query sono verificate con fixture; la prima esecuzione reale va controllata sul riepilogo del run.
