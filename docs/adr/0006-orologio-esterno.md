# ADR 0006 — Orologio esterno e segnale di vita

**Stato:** accettata · **Data:** 29/09/2026

## Contesto
Il 28–29/09/2026 nessun cron del repository è mai partito: né Observe, né Engineer, né Watchdog. Tutti i run
fino ad allora erano stati avviati a mano. GitHub non aveva incidenti aperti e non ha mandato alcun avviso.
Disattivare e riattivare i workflow ha registrato di nuovo gli orari.

I cron di GitHub Actions possono ritardare, saltare nelle ore di punta o non registrarsi affatto. Nei repository
pubblici si sospendono dopo 60 giorni senza commit. Il watchdog gira sullo stesso orologio, quindi non può
accorgersene.

L'esecuzione su Actions resta la scelta giusta: lavori brevi a lotti, Docker per le verifiche del worker, costo
zero su un repository pubblico, Workload Identity senza chiavi. Il punto debole è solo l'avvio.

## Decisione
1. **Un solo orologio**: `tick.yml` gira ogni ora e decide con l'ora UTC e di Roma che cosa avviare (Observe,
   brief, Engineer, Watchdog, Growth). Li avvia con `workflow_dispatch` e `scheduled=true`. Gli altri workflow
   non hanno più cron propri. Gli orari stanno tutti in un file.
2. **Avvio esterno**: cron-job.org chiama l'API `workflow_dispatch` di `tick.yml` ogni ora al minuto 5. Usa un
   token fine-grained limitato a questo repository con il solo permesso *Actions: read and write*.
3. **Riserva**: `tick.yml` mantiene anche un cron di GitHub. Se nella stessa ora arrivano entrambi, il secondo
   tick vede quello già riuscito e non fa nulla. I workflow avviati sono comunque idempotenti: eventi
   deduplicati, notifiche prenotate, un solo task attivo, una proposta growth per settimana. Il brief
   pianificato usa una chiave di notifica per giorno (`--once-per-day`), così due giri non mandano due brief.
4. **Segnale di vita esterno**: ogni giro pianificato riuscito di Observe fa un ping a healthchecks.io
   (secret `SUP_HEALTHCHECK_URL`). Se il ping non arriva nell'orario atteso, healthchecks.io avvisa Michele da
   fuori GitHub. Questo copre anche il caso in cui si fermi tutto, watchdog compreso.

## Conseguenze
- Il token su cron-job.org può avviare o annullare i workflow di questo repository. Non può leggere né scrivere
  codice né secret. L'avvio a mano di Evaluate resta limitato dal budget di valutazione.
- Il token scade: la data va segnata. Se cron-job.org riceve un errore, manda una mail. Se non arrivano ping,
  avvisa healthchecks.io.
- Un'esecuzione in più al giorno per ora pianificata (il tick, circa 10 secondi), senza costi su un repository
  pubblico.
- Se in futuro servissero eventi in tempo reale (pulsanti Telegram, webhook), servirà un piccolo servizio sempre
  in ascolto. I lavori pesanti possono restare su Actions.
