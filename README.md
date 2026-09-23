# MCP-server för DiVA

MCP-server (Model Context Protocol) för sökning i DiVA — Digitala Vetenskapliga Arkivet. Ger MCP-kompatibla AI-verktyg tillgång till ~1,5 miljoner vetenskapliga publikationer från ~50 svenska lärosäten och myndigheter.

## Innehåll

Täcker doktorsavhandlingar, licentiatavhandlingar, vetenskapliga artiklar, böcker, rapporter, konferensbidrag och examensarbeten publicerade vid svenska lärosäten och myndigheter.

Varje sökträff inkluderar metadata, abstract och ett **epistemisk status**-fält (poäng 1–7) som värderar källans tillförlitlighet baserat på publikationstyp, peer review-status och open access.

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `diva_sok` | Sökning med fritexter, typfilter, årsintervall, lärosäte, open access |
| `diva_hamta_post` | Fullständig metadata för en post via diva_id, URN:NBN eller DOI |
| `diva_hamta_fulltext` | On-demand PDF-extraktion med lokal cache |
| `diva_relaterade` | Relaterade poster via författare, ämne eller organisation |

Alla verktyg är läsande (`readOnlyHint`) och har titel. Svaren är typade:
klienten får både JSON-text och `structuredContent` enligt verktygets
`outputSchema`. Förväntade fel — okänt id, okänt lärosäte, ingen öppen
fulltext, DiVA svarar inte — kommer som felsvar (`isError`) med ett
meddelande på svenska.

## Källa: DiVA:s export.jsf

Servern hämtar all metadata ur DiVA:s CSV-export,
`https://www.diva-portal.org/smash/export.jsf` i formatet `csvall2`. Det finns
inget annat stöd för sökning eller uppslag. Svarar export.jsf med något annat
än den väntade CSV-exporten — en HTML-sida, ett tomt svar, HTTP 404/410 eller
en CSV utan kolumnerna `PID` och `Title` — ger verktygen ett fel som säger att
källan kan ha bytt plattform, i stället för tomma träffar. Servern behöver då
anpassas till DiVA:s nya gränssnitt.

## Krav

- Python 3.10+
- `mcp` 2.x (`mcp>=2.0,<3`, se `requirements.txt`)
- `pymupdf4llm` för fulltextextraktion (valfritt)
- PostgreSQL med pgvector eller SQLite (se konfiguration)
- Tesseract för OCR-fallback (valfritt): `brew install tesseract tesseract-lang`

## Installation

### 1. Klona och installera beroenden

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-diva.git
cd mcp-for-diva
pip install -r requirements.txt
```

### 2. Konfigurera miljövariabler

```bash
cp config.example.env .env
```

Redigera `.env` och ange databasanslutning. PostgreSQL rekommenderas:

```env
DATABASE_URL=postgresql://anvandare:losenord@localhost:5432/databas
```

SQLite-backend (välj vid installation):

```env
DATABASE_URL=sqlite:///diva_cache.db
```

### 3. Konfigurera i MCP-klienten

Exempel för Claude Desktop (`claude_desktop_config.json`):

```json
"diva": {
  "command": "<SOKVAG_TILL_PYTHON3>",
  "args": ["<SOKVAG_TILL_MCP_SERVER>"],
  "cwd": "<SOKVAG_TILL_PROJEKTMAPPEN>"
}
```

### 4. HTTP-transport (delad drift)

stdio passar en lokal MCP-klient; http passar delad drift bakom en reverse
proxy. Sätt `MCP_TRANSPORT=http` i `.env` och generera en API-nyckel:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Ange nyckeln i `MCP_API_KEY`. Nyckeln är obligatorisk: utan den avbryts
uppstarten i http-läget med exitkod 2. Servern kör Streamable HTTP på
`http://MCP_HOST:MCP_PORT/mcp` (standard `127.0.0.1:8015`), och varje anrop
måste bära `Authorization: Bearer <nyckel>` — utan header svarar servern 401,
med fel nyckel 403. SSE-transporten finns inte längre.

## Flerspråkig sökning

Kommaseparerade söktermer tolkas som OR-logik och skickas direkt till DiVA:s sök-API. Valfri AI-stödd termexpansion aktiveras med `QUERY_EXPANSION_ENABLED=true`.

```
sokterm: "rättssäkerhet,legal certainty,Rechtssicherheit"
```

## Epistemisk status

Varje träff i `diva_sok` innehåller `epistemisk_status` med ett poängvärde (1–7) och en motivering:

```json
"epistemisk_status": {
  "pong": 6,
  "typtext": "Doktorsavhandling",
  "motivering": "Doktorsavhandling + DOI, öppen fulltext",
  "display": "⭐⭐⭐⭐⭐ (6/7) — Doktorsavhandling + DOI, öppen fulltext"
}
```

Skalan och vikterna är konfigurerbara via `.env`.


## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Den största cachade posten är en avhandling på **1 214 096 tecken** — över gränsen.
`diva_hamta_fulltext` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken, högst 300 000; `0` ger största tillåtna utdrag. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett svar rymmer högst 300 000 tecken fulltext, eftersom svaret skickas både som
JSON-text och som `structuredContent` och måste hålla sig under klienternas
gräns på ungefär 1 MB. Längre texter läses i delar.

Ett kapat svar säger alltid ifrån med fälten `trunkerad`, `tecken_totalt`,
`tecken_visade` och `fortsatt_fran_tecken`, samt `las_vidare` med det
fullständiga anropet för nästa del. Kapningen sker på ordgräns, aldrig mitt i
ett ord, och `fortsatt_fran_tecken` pekar på utdragets faktiska slut — delarna
blir tillsammans exakt hela texten.

I träfflistorna från `diva_sok` och `diva_relaterade` kortas abstracts till
cirka 600 tecken (`abstract_kapad`), och en lång lista kapas med `trunkerad`
och `utelamnade_poster`. `diva_hamta_post` ger alltid hela posten.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `DIVA_MAX_TECKEN`.

## Licens

AGPLv3 — se [LICENSE](LICENSE).

DiVA-innehållet är öppet tillgängligt; enskilda poster publiceras under respektive upphovsrättsinnehavares villkor.
