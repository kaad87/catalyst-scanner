# Catalyst Scanner

Realtids-katalysatorscanner: overvåger **SEC EDGAR's live 8-K-feed** og
newswire-RSS for partnerskaber, kontrakter og deals — den slags nyheder der
flytter aktier. Nye hits committes til repoet af en GitHub Actions-cron og
vises på et statisk "wire tape"-dashboard (Netlify-venligt).

Hits **scores og tieres (A/B/C)** så ægte katalysatorkandidater skilles fra
Item 1.01-støjen (kreditfaciliteter, leasing, udvandende finansiering):

- **+2** pr. katalysator-keyword ("strategic partnership", "joint venture" …),
  **+3** for kontrakt-keywords ("awarded a contract", "purchase order" …)
- **+3** ved megacap-omtale (Nvidia, Walmart, DoD …), **+1** for Item 1.01
- **−3** pr. finansieringsterm ("credit agreement", "warrant", "at-the-market" …)
- Tier A ≥ 6, B ≥ 2, resten er C (foldet sammen på dashboardet)

Hvert hit beriges desuden med **market cap** (SEC shares outstanding ×
Yahoo-kurs; small < $2B < mid < $10B < large) og **kursændring siden
detektion** — tallet der besvarer "er jeg for sent?". Med en OpenAI-nøgle
får A/B-hits også en **AI-linje på dansk** (hvad aftalen konkret er +
kategori), og en finansiering/udvandings-klassifikation kan nedgradere et
hit til C, mens høj væsentlighed kan løfte B til A.

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
     Kun A/B-tier hits notificeres (A med høj prioritet).
   - `OPENAI_API_KEY` *(valgfri)* — aktiverer AI-opsummeringer af A/B-hits.
     Model kan overstyres med repo-variablen `OPENAI_MODEL`
     (default `gpt-4o-mini`; ved 20–50 hits/dag koster det få øre).
3. Kør workflowet manuelt første gang (*Actions → Catalyst scan → Run
   workflow*) og tjek loggen.

## Lokal kørsel

```bash
python3 catalyst_scanner.py --selftest          # offline check af matchers
SEC_USER_AGENT="MitApp min@email.dk" python3 catalyst_scanner.py
python3 -m http.server 8737                     # åbn http://localhost:8737
```

## Kilder

- **SEC EDGAR 8-K** (Item 1.01 + keyword-scan af primærdokument)
- **GlobeNewswire / PR Newswire** (katalysator-keywords)
- **FDA press releases** (godkendelses-keywords: approves, clearance, recall …)
- **Trump · Truth Social** via trumpstruth.org-arkivets RSS. Poster
  prefiltreres for markedsord/cashtags og AI-gates derefter: kun
  virksomheds-/sektorrelevante poster overlever som A/B (kategorien
  `policy` dækker fx toldudmeldinger). Uden OpenAI-nøgle beholdes kun
  megacap-poster.
- **Musk/X:** intet gratis feed — X-API'et koster $100+/md., og offentlige
  nitter-spejle er bot-blokerede. Finder du et virkende spejl, tilføjes det
  på én linje i `SOCIAL_FEEDS`. Musk-udtalelser fanges indtil da indirekte
  via newswire-dækning.

Nye kilder følger samme mønster: skriv en `scan_*()`-funktion der returnerer
hit-dicts (se `scan_rss`/`scan_social`), og kald den fra `run()`. Dedup,
dashboard og alerts samler den automatisk op.

## Opdater-knappen ("⟳ Scan nu")

Knappen kalder `/api/scan` (Netlify Function), som trigger Actions-workflowet
og dermed en frisk scanning + deploy (~2 min). Den kræver en GitHub-token i
Netlify-miljøet:

1. Opret en **fine-grained PAT** på
   https://github.com/settings/personal-access-tokens/new —
   Repository access: *Only select repositories* → `catalyst-scanner`;
   Permissions: **Actions: Read and write** (intet andet).
2. Sæt den i Netlify (indsæt tokenet når `read` venter — så rammer det
   hverken shell-historik eller skærm):
   ```bash
   read -s T && NETLIFY_SITE_ID=31bc4b87-54b1-441f-bfcc-382be036e784 \
     netlify env:set GITHUB_TOKEN "$T" && unset T
   ```

Uden token svarer funktionen 501, og knappen viser en fejl — resten af
dashboardet er upåvirket.

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
