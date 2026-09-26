# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/lang/sv/).

## [Unreleased]

## [2.0.0] — 2026-09-26

### Rättat

- OCR-språket för sidor utan textlager var engelska (pymupdf4llms
  standardvärde), eftersom ingen kod angav `ocr_language`. Svenska, franska
  och tyska tecken i skannade sidor blev därför fel. `DIVA_OCR_SPRAK`
  (standard `swe+eng+fra+deu`) styr nu språket explicit.

### Tillagt

- Minnes- och tidsvakt kring PDF-extraktionen (`pdftext_skydd.py`):
  extraktionen körs i en egen process per sidblock, och ett block som
  passerar minnes- eller tidsgränsen läses om med ren textutvinning i
  stället för att fälla processen.
- OCR-kö (`ocr_ko/ko.jsonl` + `ocr_ko/filer/`) för dokument där minst en
  sida saknade textlager eller där ett block föll tillbaka på ren
  textutvinning, så att de kan köras genom en bättre OCR senare.
- Ett svar från export.jsf som inte är den väntade CSV-exporten — HTML i
  stället för CSV, tomt svar, HTTP 404/410 eller en CSV utan kolumnerna `PID`
  och `Title` — ger ett fel som säger att källan kan ha bytt plattform, i
  stället för tomma träffar eller ett tolkningsfel.
- `diva_sok` redovisar termer vars anrop misslyckades i `misslyckade_termer`.

### Borttaget

- `ocrmypdf`-reserven i `_ocr_pdf()`. Den har aldrig kunnat köras — `ocrmypdf`
  är inte installerat — och pymupdf4llms egen sidvisa OCR (nu med rätt
  språk) täcker samma fall.
- SSE-transporten och den egna Starlette-appen för http-läget.
- De oanvända beroendena `requests` och `beautifulsoup4` ur `requirements.txt`.
- Oanvända hjälpfunktioner för relevansfiltrering och söktyp.

### Ändrat

- Frågeexpansion på serversidan har inget förvalt modellnamn. `QUERY_EXPANSION_MODEL` anges alltid i `.env` (platshållare `<modellnamn>` i `config.example.env`); saknas det hoppas expansionen över.
- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen: `mcp-for-diva/2.0`.
- **Brytande:** servern kräver `mcp>=2.0,<3` och är skriven med `MCPServer`
  och `@mcp.tool()`. Verktygsnamn, parametrar, obligatoriska fält och
  beskrivningar är oförändrade.
- **Brytande:** förväntade fel returneras som felsvar (`isError`) med ett
  meddelande på svenska, i stället för som ett lyckat svar med fältet `fel`.
  Det gäller bland annat okänt `diva_id`, okänt lärosäte, saknad öppen
  fulltext och att DiVA inte svarar.
- **Brytande:** http-läget kräver `MCP_API_KEY` och avbryter uppstarten med
  exitkod 2 utan den. Anrop utan `Authorization`-header ger 401, fel nyckel 403.
- **Brytande:** http-läget kör Streamable HTTP på `/mcp`. SSE-transporten
  (`/sse`) är borttagen.
- Svaren är typade: varje verktyg har `outputSchema`, och klienten får
  `structuredContent` utöver JSON-texten. Textfälten i en post är alltid
  strängar (tomma när DiVA:s CSV saknar värdet); bara `ar` kan vara `null`.
- Alla verktyg har titel och annotationer (läsande, öppen värld).
- `diva_sok` söker kommaseparerade termer parallellt, högst fyra åt gången.
- README och konfigurationsmallen beskriver PostgreSQL och SQLite som likvärdiga
  val i stället för att rekommendera PostgreSQL.
- `diva_hamta_fulltext` returnerar den extraherade texten även när den inte
  kunde sparas i cachen; felet loggas och nästa anrop hämtar PDF:en på nytt.

### Fixat

- `diva_sok` med standardvärdet 200 träffar kunde ge ett svar på över 1,3 MB
  (JSON-text plus `structuredContent`), över MCP-klienternas gräns på ungefär
  1 MB. I träfflistorna från `diva_sok` och `diva_relaterade` kortas nu
  abstracts till cirka 600 tecken, markerat med `abstract_kapad` och
  `abstract_tecken_totalt`. Listan kapas när posterna når ett bytetak som håller
  svaret under cirka 800 KB; ett kapat svar bär `trunkerad`,
  `utelamnade_poster` och ett `meddelande` om hur sökningen kan snävas in.
  De lägst rankade posterna utelämnas först. `diva_hamta_post` ger alltid hela
  posten. `max_traffar` betyder som förut högsta antal träffar.
- `diva_hamta_fulltext` med `max_tecken=0` gav hela texten, för den största
  avhandlingen omkring 2,4 MB med `structuredContent`. Ett svar rymmer nu högst
  300 000 tecken (cirka 650 KB); `max_tecken=0` eller ett högre värde ger
  största tillåtna utdrag, och ett `meddelande` säger att resten läses i delar.
  Ett kapat svar bär `las_vidare`, det fullständiga anropet för nästa del med
  `fran_tecken` på utdragets faktiska slut, så att delarna tillsammans blir
  exakt hela texten. `DIVA_MAX_TECKEN` begränsas till samma tak.
- En sökning där anropen för alla termer misslyckades redovisades som noll
  träffar. Den ger nu ett felsvar med orsaken.
- Samtidiga fulltextanrop kunde skriva över varandras tillfälliga PDF-fil och
  återställa processens fil 1 och 2 i fel ordning.

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
