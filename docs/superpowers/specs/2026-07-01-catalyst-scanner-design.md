# Catalyst Scanner — design

*2026-07-01. Genbygning 1:1 af design godkendt i tidligere chat-session;
godkendt igen af bruger i denne session.*

## Formål

Flagge helt friske aktiekatalysatorer (partnerskaber, kontrakter, deals —
gerne med store virksomheder) fra primærkilder, hurtigere end aggregatorer
som GuruFocus/Investing.com.

## Arkitektur

Tre løst koblede dele, samme mønster som brugerens øvrige
GitHub Actions → Netlify-dashboards:

1. **Scanner** (`catalyst_scanner.py`, ren stdlib, Python ≥3.9)
   - `scan_sec_8k()`: EDGAR `getcurrent`-atom-feed (100 seneste 8-K).
     Item-numre parses direkte fra feed-summary; **Item 1.01 = hit**.
     Primærdokument findes via mappens `index.json` (største .htm der ikke
     er index/XBRL) og keyword-/megacap-scannes. Deep-fetch cappes pr. run
     og rate-limites (0,15 s mellem SEC-requests, grænsen er 10/s).
   - `scan_rss(source, url)`: generisk RSS 2.0-handler; keyword-match på
     titel+beskrivelse. GlobeNewswire + PR Newswire (Business Wire har
     nedlagt offentlig RSS).
   - Ticker-mapping: SEC `company_tickers.json`, cachet 7 dage, første
     entry pr. CIK (primær notering).
   - Tilstand i `data/`: `hits.json` (max 500, nyeste først), `seen.json`
     (dedup, prunes efter 7 dage), `tickers.json` (cache).
   - Alerts: POST til `ntfy.sh/<NTFY_TOPIC>` pr. nyt hit (max 10 pr. run).
   - `--selftest`: offline checks af item-parsing, html→tekst, keyword- og
     megacap-matching. Fejl i én kilde vælter aldrig de andre.

2. **Cron** (`.github/workflows/scan.yml`): hvert 15. min, hverdage
   12–23 UTC. Committer `data/` når der er nye hits → Netlify-rebuild.
   Secrets: `SEC_USER_AGENT` (påkrævet), `NTFY_TOPIC` (valgfri).

3. **Dashboard** (`index.html`, statisk, ingen dependencies): wire-tape,
   nyeste først, heat-styling efter alder (<30 min gløder signalgult,
   <3 t varm, <24 t dæmpet, ældre stale), klientside-filter,
   auto-refresh 60 s.

## Fejlhåndtering

Hver kilde fanger egne exceptions og logger dem som warnings; manglende
ticker-map degraderer til tomme tickers; ntfy-fejl ignoreres. Kun total
mangel på `SEC_USER_AGENT` stopper en live-kørsel med klar besked.

## Verifikation

Selftest (10 checks) + live-kørsel mod EDGAR 2026-07-01: 22 hits, heriblandt
Surf Air Mobilitys faktiske Item 1.01-filing fra dagens Palantir-aftale.
