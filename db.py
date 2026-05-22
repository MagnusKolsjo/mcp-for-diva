"""
Databashjälpfunktioner för DiVA MCP-server.

Exponerar sex hjälpfunktioner som alla andra moduler använder:
  _ar_postgres()      — True om Postgres-backend är vald
  _hamta_db()         — ny databasanslutning (per-anrops-mönster)
  _ph()               — parameterplatshållare (%s eller ?)
  _prefix(tabell)     — tabellnamn med schema-prefix
  initiera_schema()   — skapar tabeller idempotent, tål att PG är nere
  hamta_cachad_fulltext(diva_id)
  spara_fulltext(...)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── Konfiguration ─────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{_SCRIPT_DIR / 'diva_cache.db'}",
)

_logg = logging.getLogger("diva.db")

# ── Backend-hjälpfunktioner ───────────────────────────────────────────────────


def _ar_postgres() -> bool:
    """Returnerar True om PostgreSQL-backend är konfigurerad."""
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """
    Returnerar en ny databasanslutning.

    Per-anrops-mönster: varje funktion öppnar och stänger sin egen
    anslutning. Inga globala eller delade anslutningsobjekt.
    """
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    else:
        import sqlite3
        db_fil = DATABASE_URL.replace("sqlite:///", "")
        if not Path(db_fil).is_absolute():
            db_fil = str(_SCRIPT_DIR / db_fil)
        return sqlite3.connect(db_fil)


def _ph() -> str:
    """Parameterplatshållare: %s för PostgreSQL, ? för SQLite."""
    return "%s" if _ar_postgres() else "?"


def _prefix(tabell: str) -> str:
    """Tabellnamn med schema-prefix för PostgreSQL, bare tabell för SQLite."""
    return f"diva.{tabell}" if _ar_postgres() else tabell


def _nu() -> str:
    """SQL-uttryck för aktuell tidsstämpel."""
    return "now()" if _ar_postgres() else "datetime('now')"

# ── Schemainitiering ──────────────────────────────────────────────────────────


def initiera_schema() -> None:
    """
    Skapar schema och tabeller om de inte finns (idempotent).

    Tål att Postgres-containern är nere vid uppstart — felet loggas
    som varning och servern fortsätter. Verktygsanrop felar sedan
    tills databasen är tillgänglig, men processen dör inte.
    """
    if not DATABASE_URL:
        _logg.warning("DATABASE_URL är inte satt — databasen används inte")
        return
    try:
        ansl = _hamta_db()
        try:
            markör = ansl.cursor()

            # ── Bas-schema (v1.0.0 — låst) ───────────────────────────────────
            # Ändra aldrig detta block efter first commit.
            # Lägg schemaändringar i migrationsblocket nedan.

            if _ar_postgres():
                markör.execute("CREATE SCHEMA IF NOT EXISTS diva")

            markör.execute(f"""
                CREATE TABLE IF NOT EXISTS {_prefix("fulltext_cache")} (
                    diva_id         TEXT PRIMARY KEY,
                    urn             TEXT,
                    doi             TEXT,
                    titel           TEXT,
                    ar              INTEGER,
                    publikationstyp TEXT,
                    fulltext_url    TEXT,
                    fulltext_md     TEXT,
                    pdf_hamtad      TEXT,
                    skapad          TEXT DEFAULT ({_nu()})
                )
            """)
            markör.execute(f"""
                CREATE TABLE IF NOT EXISTS {_prefix("synk_status")} (
                    nyckel      TEXT PRIMARY KEY,
                    varde       TEXT,
                    uppdaterad  TEXT DEFAULT ({_nu()})
                )
            """)
            ansl.commit()

            # ── Migrationer (tomt vid v1.0.0 baseline) ───────────────────────
            # Lägg nya ALTER TABLE ... IF NOT EXISTS här vid schemaändringar
            # efter att v1.0.0 baselinelåsts.

        finally:
            ansl.close()
    except Exception as exc:
        _logg.warning(
            "Databasinitiering misslyckades: %s — fortsätter utan DB", exc
        )

# ── Cache-funktioner ──────────────────────────────────────────────────────────


def hamta_cachad_fulltext(diva_id: str) -> Optional[str]:
    """Returnerar cachad fulltext-markdown om den finns, annars None."""
    ansl = _hamta_db()
    try:
        markör = ansl.cursor()
        markör.execute(
            f"SELECT fulltext_md FROM {_prefix('fulltext_cache')} WHERE diva_id = {_ph()}",
            (diva_id,),
        )
        rad = markör.fetchone()
        return rad[0] if rad and rad[0] else None
    finally:
        ansl.close()


def spara_fulltext(
    diva_id: str,
    urn: str,
    doi: str,
    titel: str,
    ar: int,
    publikationstyp: str,
    fulltext_url: str,
    fulltext_md: str,
) -> None:
    """Sparar extraherad fulltext i cachen (upsert)."""
    ansl = _hamta_db()
    try:
        markör = ansl.cursor()
        ph = _ph()
        tbl = _prefix("fulltext_cache")
        if _ar_postgres():
            markör.execute(f"""
                INSERT INTO {tbl}
                    (diva_id, urn, doi, titel, ar, publikationstyp,
                     fulltext_url, fulltext_md, pdf_hamtad)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},now())
                ON CONFLICT (diva_id) DO UPDATE SET
                    fulltext_md = EXCLUDED.fulltext_md,
                    pdf_hamtad  = now()
            """, (diva_id, urn, doi, titel, ar, publikationstyp,
                  fulltext_url, fulltext_md))
        else:
            markör.execute(f"""
                INSERT INTO {tbl}
                    (diva_id, urn, doi, titel, ar, publikationstyp,
                     fulltext_url, fulltext_md, pdf_hamtad)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT (diva_id) DO UPDATE SET
                    fulltext_md = excluded.fulltext_md,
                    pdf_hamtad  = datetime('now')
            """, (diva_id, urn, doi, titel, ar, publikationstyp,
                  fulltext_url, fulltext_md))
        ansl.commit()
    finally:
        ansl.close()
