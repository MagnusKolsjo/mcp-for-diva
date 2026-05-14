# Ändringslogg

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/).
Versionshanteringen följer [Semantic Versioning](https://semver.org/lang/sv/).

## [Unreleased]

### Tillagt
- `diva_sok` — sökning med kommaseparerade OR-termer, epistemisk_status per träff
- `diva_hamta_post` — fullständig metadata via diva_id, urn eller doi
- `diva_hamta_fulltext` — on-demand PDF-extraktion med lokal cache
- `diva_relaterade` — relaterade poster via författare, ämne eller organisation
- PostgreSQL-schema `diva_portal` med SQLite-fallback
- stdio- och HTTP-transport med Bearer-token-autentisering
- Valfri flerspråkig begreppsexpansion via AI-endpoint (QUERY_EXPANSION_ENABLED)
- Epistemisk status-poängsättning (1–7) konfigurerbar via .env
- FD1-skydd mot C-bindningars stdout-läckage
- `_SCRIPT_DIR`-ankrade cache-sökvägar

### Fixat
- Årsfiltrering (`fran_ar`/`till_ar`) implementerad server-side via DiVA:s `aq`-format med `{"dateIssued": {"from": "YYYY", "to": "YYYY"}}` — tidigare ignorerades parametrarna tyst av export.jsf
- Frasmatchning: SOLR-citattecken bevaras nu korrekt i `freeText`-värdet (t.ex. `"öppna data"` ger exakt frasträff i stället för AND-träff)
- Standardvärde för `max_traffar` höjt från 20 till 200
