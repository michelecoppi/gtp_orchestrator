# ADR 0002 — Notifiche con il bot approvazioni di Promo Studio

**Stato:** accettata per M1 · **Data:** 28/09/2026

## Contesto
Michele preferisce un solo bot Telegram. Promo Studio ha già un bot approvazioni (`PROMO_APPROVAL_BOT_TOKEN`).
Il comando `sync` di quel bot legge `getUpdates` e ne conferma gli offset, così i click su Approva/Rifiuta
vengono consumati da un solo lettore.

## Decisione (M1)
- Il supervisore usa lo stesso token **solo per `sendMessage`** verso `SUP_ADMIN_CHAT_ID`.
- **Niente pulsanti inline e niente `getUpdates`.** Un secondo lettore sposterebbe gli offset e Promo perderebbe
  le approvazioni.
- `doctor` usa `getMe`, che è in sola lettura e non tocca gli update.
- Testo semplice, senza `parse_mode` e senza anteprime dei link: titoli e messaggi di errore sono dati non fidati.
- Ogni messaggio è idempotente per contenuto e giorno (`notifications/{chiave}`).

## Aperto per M2+
Quando il supervisore dovrà raccogliere approvazioni, un solo processo dovrà leggere gli update. Opzioni:
1. il `sync` di Promo inoltra al supervisore i callback con prefisso `sup:` (Promo deve saperlo fare);
2. un bot dedicato al supervisore (isolamento completo, ma due bot);
3. approvazioni del supervisore solo da GitHub (issue o commenti), senza Telegram.

La scelta va fatta con Michele prima di abilitare le approvazioni. Fino ad allora il supervisore non chiede
approvazioni via Telegram.
