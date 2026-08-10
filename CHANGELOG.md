# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/lang/sv/).

## [1.2.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `diva_hamta_fulltext`**, med standardtaket
  `DIVA_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). Den största cachade
  posten är **1 214 096 tecken** — en avhandling — vilket gav ett svar på 1 222 478
  tecken och därmed överskred MCP-protokollets storleksgräns. Anropet misslyckades
  alltid för den posten. Med standardtaket blir samma anrop 60 718 tecken.
  Kapade svar bär `trunkerad`, `tecken_totalt`, `tecken_visade` och
  `fortsatt_fran_tecken`; kapningen sker på ordgräns.
- Taket tillämpas på både cacheträff och nyhämtad text, så svarsstrukturen är
  densamma oavsett kodväg.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Cachen och databasen lagrar fortfarande hela
texten — trunkeringen gäller bara svaret till anroparen, så sökning och indexering
påverkas inte.

---

## [1.1.0] — 2026-05-22

Publicerad 2026-05-22 (commit `0c58d2f`, tagg `v1.1.0`). Posten skrevs in i
efterhand 2026-08-10 — arbetet låg under `[Unreleased]` när versionen taggades
och rubriken döptes aldrig om.

### Fixat

- Sortering använde nyckel `epistemisk_status.total` (ej existerande) — sorterade alltid på noll; ändrat till `pong`
- `laerosate`-filter byggde aldrig `aq`-query — skickar nu `{"organisationId": "<id>", "organisationId-Xtra": false}` i `aq`-JSON i stället för ignorerad URL-parameter `organisation=`
- `laerosate`-fältet i sökresultat var alltid tomt — extraheras nu ur `Name`-kolumnen i DiVA:s CSV-svar via regex
- `ThesisLevel`-kolumn lästes inte — `EPISTEMISK_EXAMENSARBETE_GRUND`-variabeln i `.env` användes aldrig; nu används `ThesisLevel` för att skilja grundnivå- från avancerad examensarbete vid poängsättning
- `issn`-alias listade enbart `journalISSN` och `journaleISSN` — utökad med `seriesISSN` och `serieseISSN`
- `amne`-alias hade `researchsubjects` som primär nyckel — `categories` är nu primär nyckel för DiVA:s ämnesklassificering
- Resultfältet hette `antal_traffar` (ej exporterat) — bytt till `antal_returnerade`
- `_KANDA_FILLER_IDS` definierades inuti funktionsanrop — flyttad till modulnivå
- `initiera_schema()` saknade try/except — servern kan nu starta utan databas och utan att processen dör
- UA-strängen var kvar som webbläsar-UA — uppdaterad till `mcp-for-diva/1.0 (+https://github.com/MagnusKolsjo/mcp-for-diva)`

### Ändrat

- DB-kod extraherad till `db.py` med projektstandard-hjälpfunktioner (`_ar_postgres`, `_hamta_db`, `_ph`, `_prefix`, `initiera_schema`)
- Citationsfälten `volym`, `nummer`, `startpage`, `slutpage`, `sidor` och `hostpublication` tillagda i kolumnalias
- `pymupdf4llm`-felmeddelande hänvisar nu till `pip install pymupdf4llm` utan hårdkodad sökvägsprefiks
- Verktygets `max_traffar`-beskrivning uppdaterad till standardvärde 200

## [1.0.0] - 2026-05-15

### Tillagt

- `diva_sok` — sökning med kommaseparerade OR-termer, epistemisk_status per träff
- `diva_hamta_post` — fullständig metadata via diva_id, urn eller doi
- `diva_hamta_fulltext` — on-demand PDF-extraktion med lokal cache
- `diva_relaterade` — relaterade poster via författare, ämne eller organisation
- PostgreSQL-schema `diva` med SQLite-backend
- stdio- och HTTP-transport med Bearer-token-autentisering
- Valfri flerspråkig begreppsexpansion via AI-endpoint (QUERY_EXPANSION_ENABLED)
- Epistemisk status-poängsättning (1–7) konfigurerbar via .env
- FD1-skydd mot C-bindningars stdout-läckage
- `_SCRIPT_DIR`-ankrade cache-sökvägar

### Fixat

- Årsfiltrering (`fran_ar`/`till_ar`) implementerad server-side via DiVA:s `aq`-format med `{"dateIssued": {"from": "YYYY", "to": "YYYY"}}` — tidigare ignorerades parametrarna tyst av export.jsf
- Frasmatchning: SOLR-citattecken bevaras nu korrekt i `freeText`-värdet (t.ex. `"öppna data"` ger exakt frasträff i stället för AND-träff)
- Standardvärde för `max_traffar` höjt från 20 till 200
