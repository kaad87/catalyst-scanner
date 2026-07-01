# Catalyst Scanner

Realtids-katalysatorscanner: overvåger **SEC EDGAR's live 8-K-feed** og
newswire-RSS for partnerskaber, kontrakter og deals — den slags nyheder der
flytter aktier. Nye hits committes til repoet af en GitHub Actions-cron og
vises på et statisk "wire tape"-dashboard (Netlify-venligt).

## Hvordan det virker

```
SEC EDGAR 8-K feed ─┐
GlobeNewswire RSS  ─┼─> catalyst_scanner.py ─> data/hits.json ─> index.html
PR Newswire RSS    ─┘        (cron 15 min)         (commit)       (dashboard)
                                  └────────> ntfy.sh push (valgfri)
```

- **`catalyst_scanner.py`** — tailer EDGAR's `getcurrent` 8-K-atom-feed.
  Feedets summary indeholder allerede item-listen, så **Item 1.01**
  (Material Definitive Agreement — hvor underskrevne partnerskaber/kontrakter
  lander) flagges uden ekstra requests. Derudover deep-fetches primær-
  dokumentet og scannes for keywords ("strategic partnership", "awarded a
  contract", "joint venture" …) samt megacap-partnere (Nvidia, Microsoft,
  DoD …). Generisk RSS-handler dækker pressebureauerne.
- **`.github/workflows/scan.yml`** — kører hvert 15. min på hverdage
  (12–23 UTC ≈ US pre-market til after-hours) og committer nye hits, hvilket
  trigger Netlify-rebuild.
- **`index.html`** — statisk tape, nyeste først. Hits under 30 min gløder
  signalgult og køler af med alderen (30 min → 3 t → 24 t → stale).

## Opsætning

1. Push repoet til GitHub og kobl det på Netlify (ingen build-kommando,
   publish = roden).
2. Tilføj repo-secrets under *Settings → Secrets and variables → Actions*:
   - `SEC_USER_AGENT` **(påkrævet)** — fx `CatalystScanner din@email.com`.
     SEC blokerer requests uden reel kontakt-User-Agent. Max 10 req/s
     (scanneren holder sig langt under).
   - `NTFY_TOPIC` *(valgfri)* — et selvvalgt topic-navn; abonnér i
     [ntfy-appen](https://ntfy.sh) på samme topic for push på telefonen.
3. Kør workflowet manuelt første gang (*Actions → Catalyst scan → Run
   workflow*) og tjek loggen.

## Lokal kørsel

```bash
python3 catalyst_scanner.py --selftest          # offline check af matchers
SEC_USER_AGENT="MitApp min@email.dk" python3 catalyst_scanner.py
python3 -m http.server 8737                     # åbn http://localhost:8737
```

## Udvidelse

Nye kilder følger samme mønster: skriv en `scan_*()`-funktion der returnerer
hit-dicts (se `scan_rss`), og kald den fra `run()`. Dedup, dashboard og
alerts samler den automatisk op. Oplagte næste: DoD daily contract awards,
FDA-godkendelser.

## Ærlige begrænsninger

- Fanger katalysatorer **idet de bliver offentlige** — hurtigere end
  aggregatorer, men ikke hurtigere end markedet. Edgen ligger i den lange
  hale: small/mid-caps hvis 8-K ingen kigger på i realtid.
- Business Wire har nedlagt sine offentlige RSS-feeds; tilføj selv et
  fungerende feed i `RSS_FEEDS` hvis du finder ét.
- 8-K'er dukker op i feedet 1–3 min efter accept — men cron'en kører kun
  hvert 15. min (GitHub Actions' reelle minimum).

**Dette er research, ikke investeringsrådgivning.** Partnerskabsnyheder er
berygtede for "buy the rumor, sell the news".
