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

## Krav

- Python 3.10+
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

SQLite-fallback (inget PostgreSQL behövs):

```env
DATABASE_URL=sqlite:///diva_cache.db
```

### 3. Konfigurera i MCP-klienten

Exempel för Claude Desktop (`claude_desktop_config.json`):

```json
"diva": {
  "command": "/sökväg/till/python3",
  "args": ["/sökväg/till/mcp_server.py"],
  "cwd": "/sökväg/till/mcp-for-diva"
}
```

### 4. HTTP-transport (hostad driftsättning)

Sätt `MCP_TRANSPORT=http` i `.env` och generera en API-nyckel:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Ange nyckeln i `MCP_API_KEY`. Servern lyssnar på `MCP_HOST:MCP_PORT`.

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

## Licens

AGPLv3 — se [LICENSE](LICENSE).

DiVA-innehållet är öppet tillgängligt; enskilda poster publiceras under respektive upphovsrättsinnehavares villkor.
