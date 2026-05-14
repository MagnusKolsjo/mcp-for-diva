#!/usr/bin/env python3
"""
MCP-server för DiVA-Portal (Digitala Vetenskapliga Arkivet).

Exponerar fyra verktyg:
  diva_sok            — Sök bland ~1,5 miljoner poster
  diva_hamta_post     — Hämta fullständig metadata för en post via ID
  diva_hamta_fulltext — Hämta och casha PDF-fulltext on-demand
  diva_relaterade     — Hitta relaterade publikationer

Transport: stdio (standard) eller HTTP (MCP_TRANSPORT=http).
Databas:   PostgreSQL (standard) eller SQLite (DATABASE_URL=sqlite:///...).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# ── Konfiguration ─────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_SCRIPT_DIR / 'diva_cache.db'}")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8015"))
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

_PDF_CACHE = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
if not _PDF_CACHE.is_absolute():
    _PDF_CACHE = _SCRIPT_DIR / _PDF_CACHE
_PDF_CACHE.mkdir(parents=True, exist_ok=True)

PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "7"))

QUERY_EXPANSION_ENABLED = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_API_URL = os.getenv("QUERY_EXPANSION_API_URL", "")
QUERY_EXPANSION_API_KEY = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL = os.getenv("QUERY_EXPANSION_MODEL", "claude-haiku-4-5-20251001")

# Epistemisk status — grundpoäng per publikationstyp
_EPISTEMISK_GRUNDPONG: dict[str, int] = {
    "doctoralThesis":                int(os.getenv("EPISTEMISK_DOKTORSAVHANDLING", "5")),
    "monographDoctoralThesis":       int(os.getenv("EPISTEMISK_DOKTORSAVHANDLING", "5")),
    "comprehensiveDoctoralThesis":   int(os.getenv("EPISTEMISK_DOKTORSAVHANDLING", "5")),
    "licentiateThesis":              int(os.getenv("EPISTEMISK_LICENTIATAVHANDLING", "4")),
    "monographLicentiateThesis":     int(os.getenv("EPISTEMISK_LICENTIATAVHANDLING", "4")),
    "comprehensiveLicentiateThesis": int(os.getenv("EPISTEMISK_LICENTIATAVHANDLING", "4")),
    "article":                       int(os.getenv("EPISTEMISK_ARTIKEL_GRANSKAD", "4")),
    "review":                        int(os.getenv("EPISTEMISK_ARTIKEL_GRANSKAD", "4")),
    "bookReview":                    2,
    "book":                          int(os.getenv("EPISTEMISK_BOK", "3")),
    "collection":                    int(os.getenv("EPISTEMISK_BOK", "3")),
    "chapter":                       int(os.getenv("EPISTEMISK_BOK", "3")),
    "conferencePaper":               int(os.getenv("EPISTEMISK_KONFERENSBIDRAG_GRANSKAD", "3")),
    "conferenceProceedings":         2,
    "report":                        int(os.getenv("EPISTEMISK_RAPPORT", "2")),
    "manuscript":                    2,
    "patent":                        2,
    "studentThesis":                 int(os.getenv("EPISTEMISK_EXAMENSARBETE_AVANCERAD", "2")),
    "other":                         int(os.getenv("EPISTEMISK_OVRIG", "1")),
}

# ── Loggning ──────────────────────────────────────────────────────────────────

_LOGG_DIR = _SCRIPT_DIR / "logs"
_LOGG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_LOGG_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_logg = logging.getLogger("diva")

# ── FD1-skydd (MCP-stdio-hygien) ──────────────────────────────────────────────

@contextlib.contextmanager
def _tysta_fd1():
    """Omdirigerar FD 1+2 till loggfil under anrop som kan skriva till stdout."""
    loggfil = _LOGG_DIR / "subprocess.log"
    spar_ut = os.dup(1)
    spar_fel = os.dup(2)
    fd = os.open(str(loggfil), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        yield
    finally:
        os.dup2(spar_ut, 1)
        os.dup2(spar_fel, 2)
        os.close(spar_ut)
        os.close(spar_fel)
        os.close(fd)

# ── Databas ───────────────────────────────────────────────────────────────────

_ANVANDER_POSTGRES = DATABASE_URL.startswith("postgresql")

# Tabellprefix: schema.tabell för PG, bara tabell för SQLite
_TBL_CACHE = "diva.fulltext_cache" if _ANVANDER_POSTGRES else "fulltext_cache"
_TBL_SYNK  = "diva.synk_status"    if _ANVANDER_POSTGRES else "synk_status"
_PH        = "%s" if _ANVANDER_POSTGRES else "?"
_NU        = "now()" if _ANVANDER_POSTGRES else "datetime('now')"


def _db_anslutning():
    if _ANVANDER_POSTGRES:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    else:
        import sqlite3
        db_fil = DATABASE_URL.replace("sqlite:///", "")
        if not Path(db_fil).is_absolute():
            db_fil = str(_SCRIPT_DIR / db_fil)
        return sqlite3.connect(db_fil)


def _db_init():
    """Skapar schema och tabeller om de inte finns."""
    ansl = _db_anslutning()
    try:
        markör = ansl.cursor()
        if _ANVANDER_POSTGRES:
            markör.execute("CREATE SCHEMA IF NOT EXISTS diva")
        markör.execute(f"""
            CREATE TABLE IF NOT EXISTS {_TBL_CACHE} (
                diva_id         TEXT PRIMARY KEY,
                urn             TEXT,
                doi             TEXT,
                titel           TEXT,
                ar              INTEGER,
                publikationstyp TEXT,
                fulltext_url    TEXT,
                fulltext_md     TEXT,
                pdf_hamtad      TEXT,
                skapad          TEXT DEFAULT ({_NU})
            )
        """)
        markör.execute(f"""
            CREATE TABLE IF NOT EXISTS {_TBL_SYNK} (
                nyckel      TEXT PRIMARY KEY,
                varde       TEXT,
                uppdaterad  TEXT DEFAULT ({_NU})
            )
        """)
        ansl.commit()
    finally:
        ansl.close()


def _hamta_cachad_fulltext(diva_id: str) -> str | None:
    """Returnerar cachad fulltext-markdown om den finns."""
    ansl = _db_anslutning()
    try:
        markör = ansl.cursor()
        markör.execute(
            f"SELECT fulltext_md FROM {_TBL_CACHE} WHERE diva_id = {_PH}",
            (diva_id,),
        )
        rad = markör.fetchone()
        return rad[0] if rad and rad[0] else None
    finally:
        ansl.close()


def _spara_fulltext(
    diva_id: str, urn: str, doi: str, titel: str, ar: int,
    publikationstyp: str, fulltext_url: str, fulltext_md: str,
) -> None:
    """Sparar extraherad fulltext i cachen (upsert)."""
    ansl = _db_anslutning()
    try:
        markör = ansl.cursor()
        ph = _PH
        if _ANVANDER_POSTGRES:
            markör.execute(f"""
                INSERT INTO {_TBL_CACHE}
                    (diva_id, urn, doi, titel, ar, publikationstyp, fulltext_url, fulltext_md, pdf_hamtad)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},now())
                ON CONFLICT (diva_id) DO UPDATE SET
                    fulltext_md = EXCLUDED.fulltext_md,
                    pdf_hamtad  = now()
            """, (diva_id, urn, doi, titel, ar, publikationstyp, fulltext_url, fulltext_md))
        else:
            markör.execute(f"""
                INSERT INTO {_TBL_CACHE}
                    (diva_id, urn, doi, titel, ar, publikationstyp, fulltext_url, fulltext_md, pdf_hamtad)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT (diva_id) DO UPDATE SET
                    fulltext_md = excluded.fulltext_md,
                    pdf_hamtad  = datetime('now')
            """, (diva_id, urn, doi, titel, ar, publikationstyp, fulltext_url, fulltext_md))
        ansl.commit()
    finally:
        ansl.close()

# ── DiVA API-hjälpfunktioner ──────────────────────────────────────────────────

_DIVA_EXPORT_URL = "https://www.diva-portal.org/smash/export.jsf"
_DIVA_HUVUDEN = {
    "User-Agent": "Mozilla/5.0 (compatible; riksdags-rattsinformations-ai/1.0)",
    "Accept":     "text/csv,text/plain,*/*",
    "Referer":    "https://www.diva-portal.org/",
}

# Kolumnmappning: internt fältnamn → möjliga CSV-rubriker (gemener, case-insensitiv)
# DiVA export.jsf (csvall2) skickar engelska CamelCase-kolumner.
_KOLUMN_ALIAS: dict[str, list[str]] = {
    "diva_id":           ["pid", "postid", "post id"],
    "forfattare":        ["name", "författare", "author", "authors"],
    "titel":             ["title", "titel"],
    "publikationstyp":   ["publicationtype", "publikationstyp", "publication type", "type", "typ"],
    "sprak":             ["language", "språk", "sprak"],
    "ar":                ["year", "år"],
    "abstract":          ["abstract", "sammanfattning"],
    "doi":               ["doi"],
    "urn":               ["nbn", "urn:nbn", "urn", "uri"],
    "nyckelord":         ["keywords", "nyckelord"],
    "amne":              ["researchsubjects", "categories", "nationell ämneskategori", "subject", "subjects"],
    "laerosate":         ["organisation", "university", "institution"],
    "tidskrift":         ["journal", "tidskrift"],
    "fulltext_url":      ["fulltextlink", "länk till fulltext", "fulltext url", "link to fulltext"],
    "granskad":          ["reviewed", "granskad", "peer reviewed", "refereed"],
    "fri_fulltext":      ["freefulltext", "fri fulltext", "free fulltext", "open access"],
    "disputationsdatum": ["defencedate", "disputationsdatum", "defense date"],
    "handledare":        ["supervisors", "handledare", "supervisor"],
    "examinator":        ["examiners", "examinator", "examiner"],
    "isbn":              ["isbn"],
    "issn":              ["issn"],
    "foerlag":           ["publisher", "förlag"],
    "underkategori":     ["publicationsubtype", "underkategori", "subcategory"],
}

# DiVA returnerar svenska displaysträngar i PublicationType-kolumnen.
# Mappa dessa till interna koder som _berakna_epistemisk_status förstår.
_DIVA_TYP_TILL_INTERN: dict[str, str] = {
    "doktorsavhandling, monografi":                   "monographDoctoralThesis",
    "doktorsavhandling, sammanläggning":              "comprehensiveDoctoralThesis",
    "doktorsavhandling":                              "doctoralThesis",
    "licentiatavhandling, monografi":                 "monographLicentiateThesis",
    "licentiatavhandling, sammanläggning":            "comprehensiveLicentiateThesis",
    "licentiatavhandling":                            "licentiateThesis",
    "artikel i tidskrift":                            "article",
    "artikel, forskningsöversikt":                    "review",
    "artikel":                                        "article",
    "recension":                                      "bookReview",
    "bok":                                            "book",
    "samlingsverk":                                   "collection",
    "samlingsverk (redaktörskap)":                    "collection",
    "kapitel i bok, del av antologi":                 "chapter",
    "kapitel i bok":                                  "chapter",
    "konferensbidrag":                                "conferencePaper",
    "proceedings (redaktörskap)":                     "conferenceProceedings",
    "rapport":                                        "report",
    "studentuppsats (examensarbete)":                 "studentThesis",
    "patent":                                         "patent",
    "övrigt":                                         "other",
    "konstnärlig output":                             "other",
    "manuskript (preprint)":                          "manuscript",
}

# Föräldratyp-mappning: detaljerade koder → den kod användaren anger vid filtrering.
# T.ex. 'monographDoctoralThesis' och 'comprehensiveDoctoralThesis' är
# undertyper av 'doctoralThesis' — ska matcha om användaren söker 'doctoralThesis'.
_TYP_FOREALDRATYP: dict[str, str] = {
    "monographDoctoralThesis":       "doctoralThesis",
    "comprehensiveDoctoralThesis":   "doctoralThesis",
    "monographLicentiateThesis":     "licentiateThesis",
    "comprehensiveLicentiateThesis": "licentiateThesis",
    "collection":                    "book",
    "conferenceProceedings":         "conferencePaper",
}


def _typ_for_filter(intern_typ: str) -> str:
    """Normaliserar intern typkod till föräldratyp för jämförelse vid filtrering."""
    return _TYP_FOREALDRATYP.get(intern_typ, intern_typ)


def _bygg_rubrikindex(rubriker: list[str]) -> dict[str, str]:
    """Bygger ett index: internt fältnamn → faktisk CSV-rubrik."""
    index: dict[str, str] = {}
    rubriker_gem = {r.lower(): r for r in rubriker}
    for internt, alias_lista in _KOLUMN_ALIAS.items():
        for alias in alias_lista:
            if alias in rubriker_gem:
                index[internt] = rubriker_gem[alias]
                break
    return index


def _val(rad: dict, rubrikindex: dict, falt: str, default: str = "") -> str:
    """Hämtar värdet för ett internt fältnamn ur en CSV-rad."""
    rubrik = rubrikindex.get(falt)
    if not rubrik:
        return default
    return (rad.get(rubrik) or "").strip()


def _formatera_enkel_sokterm(term: str) -> str:
    """
    Formaterar en enskild sökterm för DiVA:s aq/freeText-fält (SOLR-baserat).

    DiVA:s SOLR-motor stödjer två matchningslägen:
    - Implicit AND:   'öppna data'   → båda orden måste finnas i posten, men
                                       inte nödvändigtvis adjacenta.
    - Exakt fras:     '"öppna data"' → orden måste förekomma som exakt fras
                                       (citattecknen bevaras in i JSON-värdet).

    Användaren väljer läge via citat:
    - Ingen citat  → AND-sökning (bred täckning, kan ge falskt positiva).
    - Med citat    → frasmatchning (precision, kan ge färre träffar).

    Regler:
    - Citerad term:    bevara citaten så SOLR tolkar dem som frasmatchning.
    - Ociterad term:   skicka som är (implicit AND).
    - Tom term:        returnera tom sträng.
    """
    term = term.strip()
    if not term:
        return term
    # Bevara yttre citat — de skickas in i freeText-JSON för SOLR-frasmatchning
    return term


# Stoppord som inte bidrar till relevansbedömning
_STOPPORD: frozenset[str] = frozenset({
    # Svenska
    "i", "och", "av", "för", "till", "med", "på", "den", "det", "en", "ett",
    "de", "om", "är", "som", "att", "men", "har", "inte", "vi", "du", "han",
    "hon", "ni", "dem", "sig", "sin", "sitt", "sina", "inom", "under", "samt",
    "vid", "från", "kan", "mot", "över", "efter", "ut", "upp", "ner", "när",
    # Engelska
    "of", "the", "a", "an", "and", "in", "to", "for", "on", "at", "by",
    "with", "from", "or", "not", "it", "is", "be", "as", "are", "its",
    "this", "that", "which", "have", "has", "had", "was", "were",
})


def _ar_relevant_for_term(post: dict, sokterm: str) -> bool:
    """
    Kontrollerar att en DiVA-post faktiskt innehåller minst ett signifikant
    ord från söktermen i titel, abstract, nyckelord eller ämnesområde.

    Filtrerar bort DiVA:s filler-poster som returneras när en sökning ger
    noll verkliga träffar — dessa saknar all tematisk koppling till söktermen.
    """
    # Extrahera signifikanta sökord (längre än 2 tecken, ej stoppord)
    sookord = [
        o.lower()
        for o in sokterm.split()
        if len(o) > 2 and o.lower() not in _STOPPORD
    ]
    if not sookord:
        return True  # kan inte avgöra — behåll posten

    # Sök i alla textfält
    text = " ".join(filter(None, [
        post.get("titel", ""),
        post.get("abstract", ""),
        post.get("nyckelord", ""),
        post.get("amne", ""),
    ])).lower()

    return any(ord_ in text for ord_ in sookord)


def _bestam_soktyp(publikationstyper: list[str]) -> str:
    """Bestämmer DiVA searchtype baserat på begärda publikationstyper."""
    if not publikationstyper:
        return "all"
    undergraduate_typer = {"studentthesis", "examensarbete", "undergraduate"}
    har_undergraduate = any(p.lower() in undergraduate_typer for p in publikationstyper)
    har_postgraduate = any(p.lower() not in undergraduate_typer for p in publikationstyper)
    if har_undergraduate and not har_postgraduate:
        return "undergraduate"
    if har_postgraduate and not har_undergraduate:
        return "postgraduate"
    # Blandat — all stöder inte publicationtype-filter men ger bredast täckning
    return "all"


def _berakna_epistemisk_status(
    publikationstyp: str,
    har_doi: bool,
    open_access: bool,
    granskad: str,
) -> dict:
    """Beräknar epistemisk status (poäng 1–7) och returnerar poäng + motivering."""
    grundpong = _EPISTEMISK_GRUNDPONG.get(publikationstyp, 1)

    # Artikel utan bekräftad granskning sänks ett steg
    if publikationstyp in ("article", "review"):
        if granskad.lower() not in ("yes", "ja", "true", "1", "x"):
            grundpong = max(1, grundpong - 1)

    bonus = 0
    bonus_delar: list[str] = []
    if har_doi:
        bonus += 1
        bonus_delar.append("DOI")
    if open_access:
        bonus += 1
        bonus_delar.append("öppen fulltext")

    totalt = min(7, grundpong + bonus)

    typbeskrivningar = {
        "doctoralThesis":                "Doktorsavhandling",
        "monographDoctoralThesis":       "Doktorsavhandling (monografi)",
        "comprehensiveDoctoralThesis":   "Doktorsavhandling (sammanläggning)",
        "licentiateThesis":              "Licentiatavhandling",
        "monographLicentiateThesis":     "Licentiatavhandling (monografi)",
        "comprehensiveLicentiateThesis": "Licentiatavhandling (sammanläggning)",
        "article":                       "Artikel i tidskrift",
        "review":                        "Forskningsöversikt",
        "bookReview":                    "Recension",
        "book":                          "Bok",
        "collection":                    "Samlingsverk",
        "chapter":                       "Bokkapitel",
        "conferencePaper":               "Konferensbidrag",
        "conferenceProceedings":         "Konferensproceedings",
        "report":                        "Rapport",
        "manuscript":                    "Manuskript (preprint)",
        "studentThesis":                 "Examensarbete",
        "patent":                        "Patent",
        "other":                         "Övrigt",
    }
    typtext = typbeskrivningar.get(publikationstyp, publikationstyp or "Okänd typ")

    motivering = typtext
    if bonus_delar:
        motivering += " + " + ", ".join(bonus_delar)

    # Max 5 stjärnor i visuell display
    stjarnor = "⭐" * min(totalt, 5)

    return {
        "pong":      totalt,
        "typtext":   typtext,
        "motivering": motivering,
        "display":   f"{stjarnor} ({totalt}/7) — {motivering}",
    }


def _normalisera_rad(rad: dict, rubrikindex: dict) -> dict | None:
    """Normaliserar en CSV-rad till ett standardiserat postdikt."""
    diva_id = _val(rad, rubrikindex, "diva_id")
    if not diva_id:
        return None

    # Konstruera diva2:NNNNN-format
    # DiVA's CSV har PID som bara siffror (t.ex. "356766") — lägg till prefix.
    # URL-format "https://.../record.jsf?pid=diva2:789" och "diva2:789" hanteras med regex.
    m = re.search(r'diva2:\d+', diva_id)
    if m:
        diva_id = m.group(0)
    elif diva_id.isdigit():
        diva_id = f"diva2:{diva_id}"
    elif "/" in diva_id:
        diva_id = diva_id.rstrip("/").split("/")[-1]

    # Normalisera publikationstyp: DiVA returnerar svenska displaysträngar,
    # mappa till interna koder för epistemisk_status-beräkning.
    publikationstyp_raa = _val(rad, rubrikindex, "publikationstyp")
    publikationstyp = _DIVA_TYP_TILL_INTERN.get(
        publikationstyp_raa.lower(), publikationstyp_raa or "other"
    )
    doi              = _val(rad, rubrikindex, "doi")
    urn              = _val(rad, rubrikindex, "urn")
    fulltext_url     = _val(rad, rubrikindex, "fulltext_url")
    fri_fulltext_raw = _val(rad, rubrikindex, "fri_fulltext").lower()
    granskad_raw     = _val(rad, rubrikindex, "granskad")

    open_access = fri_fulltext_raw in ("yes", "ja", "true", "1", "x", "✓") or bool(fulltext_url)
    har_doi     = bool(doi)

    ar_str = _val(rad, rubrikindex, "ar")
    try:
        ar: int | None = int(ar_str) if ar_str and ar_str.isdigit() else None
    except (ValueError, TypeError):
        ar = None

    # Filtrera bort DiVA:s fallback-poster (två kända kategorier):
    # 1. Naturvårdsverket-rapporter med korrupt årsdata: år 901, 905, 909 etc.
    #    Avvisa poster med år < 1000 som orimliga för akademiska publikationer.
    # 2. Kända BTH-konferenspapper (diva2:833794, diva2:837011) som DiVA returnerar
    #    som fallback vid noll verkliga träffar. Blocklista utökas vid behov.
    if ar is not None and ar < 1000:
        return None
    _KANDA_FILLER_IDS = {"diva2:833794", "diva2:837011"}
    if diva_id in _KANDA_FILLER_IDS:
        return None

    epistemisk = _berakna_epistemisk_status(
        publikationstyp=publikationstyp,
        har_doi=har_doi,
        open_access=open_access,
        granskad=granskad_raw,
    )

    return {
        "diva_id":           diva_id,
        "titel":             _val(rad, rubrikindex, "titel"),
        "forfattare":        _val(rad, rubrikindex, "forfattare"),
        "ar":                ar,
        "publikationstyp":   publikationstyp,
        "sprak":             _val(rad, rubrikindex, "sprak"),
        "abstract":          _val(rad, rubrikindex, "abstract"),
        "nyckelord":         _val(rad, rubrikindex, "nyckelord"),
        "amne":              _val(rad, rubrikindex, "amne"),
        "laerosate":         _val(rad, rubrikindex, "laerosate"),
        "tidskrift":         _val(rad, rubrikindex, "tidskrift"),
        "doi":               doi,
        "urn":               urn,
        "isbn":              _val(rad, rubrikindex, "isbn"),
        "fulltext_url":      fulltext_url,
        "open_access":       open_access,
        "granskad":          granskad_raw,
        "handledare":        _val(rad, rubrikindex, "handledare"),
        "examinator":        _val(rad, rubrikindex, "examinator"),
        "disputationsdatum": _val(rad, rubrikindex, "disputationsdatum"),
        "epistemisk_status": epistemisk,
    }


def _hamta_diva_export(params: dict) -> list[dict]:
    """
    Anropar DiVA export.jsf med format=csvall2 och returnerar normaliserade poster.

    DiVA:s export-API ignorerar parametern 'freetext' — den är ett icke-implementerat
    alias. Korrekt sökparameter är 'aq' (advanced query) med JSON-format:
      aq=[[{"freeText": "söktermen"}]]
    Det yttre arrayet är OR-grupper, det inre arrayet är AND-villkor.

    Interna parameternamn som hanteras av denna funktion:
      freetext  → konverteras till aq=[[{"freeText": ...}]]
      rows      → noOfRows
      sort      → sortOrder (år → dateIssued_sort_desc)
    """
    import json as _json

    params = dict(params)

    # Konvertera 'freetext' → 'aq' med korrekt JSON-format.
    # Årsfiltrering (ar_fran/ar_till) läggs till som {"dateIssued": {...}}
    # i samma AND-grupp som freeText — DiVA:s dokumenterade format är
    # {"dateIssued": {"from": "YYYY", "to": "YYYY"}}.
    ar_fran = params.pop("ar_fran", None)
    ar_till = params.pop("ar_till", None)

    if "freetext" in params:
        fritextterm = params.pop("freetext")
        if fritextterm and "aq" not in params:
            and_villkor: list[dict] = [{"freeText": fritextterm}]
            if ar_fran and ar_till:
                and_villkor.append(
                    {"dateIssued": {"from": str(ar_fran), "to": str(ar_till)}}
                )
            params["aq"] = _json.dumps([and_villkor])
            params.setdefault("aqe", "[]")
            params.setdefault("af",  "[]")
            params.setdefault("aq2", "[[]]")

    if "rows" in params:
        params["noOfRows"] = params.pop("rows")
    if "sort" in params:
        sort_val = params.pop("sort")
        if "year" in sort_val or "date" in sort_val:
            params["sortOrder"] = "dateIssued_sort_desc"

    fragestallning = {"format": "csvall2", **params}

    try:
        svar = httpx.get(
            _DIVA_EXPORT_URL,
            params=fragestallning,
            headers=_DIVA_HUVUDEN,
            timeout=30.0,
            follow_redirects=True,
        )
        svar.raise_for_status()
    except httpx.HTTPError as e:
        _logg.error("DiVA API-fel: %s", e)
        raise RuntimeError(f"DiVA API svarade inte: {e}") from e

    # Tolka svarskropp med BOM-stöd
    text = svar.content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []

    # Detektera CSV-separator (semikolon vanligare i europeiska exporter)
    forsta_rad = text.split("\n")[0] if "\n" in text else text[:500]
    separator = ";" if forsta_rad.count(";") > forsta_rad.count(",") else ","

    lasare = csv.DictReader(io.StringIO(text), delimiter=separator)
    rubriker: list[str] = list(lasare.fieldnames or [])
    rubrikindex = _bygg_rubrikindex(rubriker)

    if not rubrikindex:
        _logg.warning(
            "Inga kända kolumnnamn hittades i DiVA-svaret. "
            "Faktiska rubriker: %s", rubriker[:10]
        )

    poster: list[dict] = []
    for rad in lasare:
        post = _normalisera_rad(rad, rubrikindex)
        if post:
            poster.append(post)

    return poster

# ── Fulltextextraktion ────────────────────────────────────────────────────────

def _hamta_pdf_fulltext(diva_id: str, fulltext_url: str, urn: str = "") -> str:
    """
    Hämtar PDF och extraherar text med pymupdf4llm.
    OCR-fallback via Tesseract om textlagret är tomt.
    Raderar PDF direkt efter lyckad extraktion (per TTL-principen).
    """
    if not fulltext_url and urn:
        # Försök via URN:NBN-resolver
        fulltext_url = f"https://urn.kb.se/{urn}" if urn.startswith("urn:") else ""

    if not fulltext_url:
        raise ValueError(f"Ingen fulltextlänk tillgänglig för {diva_id}")

    pdf_fil = _PDF_CACHE / f"{re.sub(r'[:/]', '_', diva_id)}.pdf"

    try:
        with httpx.stream(
            "GET", fulltext_url, headers=_DIVA_HUVUDEN,
            timeout=60.0, follow_redirects=True,
        ) as svar:
            svar.raise_for_status()
            with open(pdf_fil, "wb") as f:
                for del_ in svar.iter_bytes(chunk_size=65536):
                    f.write(del_)
    except httpx.HTTPError as e:
        _logg.error("PDF-nedladdning misslyckades för %s: %s", diva_id, e)
        raise RuntimeError(f"PDF-nedladdning misslyckades: {e}") from e

    try:
        try:
            import pymupdf4llm
        except ImportError as imp_err:
            raise RuntimeError(
                "pymupdf4llm är inte installerat. "
                "Kör: $HOME/MCP-Servers/.venv/bin/pip install pymupdf4llm"
            ) from imp_err

        with _tysta_fd1():
            text = pymupdf4llm.to_markdown(str(pdf_fil))

        if not text or len(text.strip()) < 100:
            _logg.info("Tomt textlager för %s — provar OCR", diva_id)
            text = _ocr_pdf(pdf_fil)

    except RuntimeError:
        raise
    except Exception as e:
        _logg.error("Textextraktion misslyckades för %s: %s", diva_id, e)
        raise RuntimeError(f"Textextraktion misslyckades: {e}") from e
    finally:
        pdf_fil.unlink(missing_ok=True)

    return text or ""


def _ocr_pdf(pdf_fil: Path) -> str:
    """OCR-fallback via ocrmypdf + pymupdf4llm."""
    import pymupdf4llm

    ocr_fil = pdf_fil.with_suffix(".ocr.pdf")
    try:
        try:
            import ocrmypdf
        except ImportError:
            _logg.warning("ocrmypdf inte installerat — hoppar OCR-fallback")
            return ""

        with _tysta_fd1():
            ocrmypdf.ocr(
                str(pdf_fil),
                str(ocr_fil),
                language="swe+eng+fra+deu",
                skip_text=True,
                progress_bar=False,
            )
        with _tysta_fd1():
            text = pymupdf4llm.to_markdown(str(ocr_fil))
        return text or ""
    except Exception as e:
        _logg.error("OCR misslyckades: %s", e)
        return ""
    finally:
        ocr_fil.unlink(missing_ok=True)

# ── Flerspråkig begreppsexpansion ─────────────────────────────────────────────

def _expandera_sokterm(sokterm: str) -> str:
    """
    Expanderar sökterm till flerspråkiga ekvivalenter via AI-endpoint.
    Returnerar kommaseparerad söksträng (samma format som indata).
    Expansion sker bara om QUERY_EXPANSION_ENABLED=true i .env.
    """
    if not QUERY_EXPANSION_ENABLED or not QUERY_EXPANSION_API_URL:
        return sokterm

    prompt_fil = _SCRIPT_DIR / "prompts" / "expansion_prompt.txt"
    if not prompt_fil.exists():
        _logg.warning("Promptfil saknas: %s", prompt_fil)
        return sokterm

    prompt = prompt_fil.read_text(encoding="utf-8").replace("{sokterm}", sokterm)

    try:
        svar = httpx.post(
            QUERY_EXPANSION_API_URL,
            headers={
                "Authorization":      f"Bearer {QUERY_EXPANSION_API_KEY}",
                "Content-Type":       "application/json",
                "anthropic-version":  "2023-06-01",
            },
            json={
                "model":      QUERY_EXPANSION_MODEL,
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15.0,
        )
        svar.raise_for_status()
        data = svar.json()
        expanderad = data["content"][0]["text"].strip()
        _logg.info("Begreppsexpansion: '%s' → '%s'", sokterm, expanderad)
        return expanderad
    except Exception as e:
        _logg.warning("Begreppsexpansion misslyckades — använder originalterm: %s", e)
        return sokterm

# ── MCP-server ────────────────────────────────────────────────────────────────

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_server = Server("diva")


@_server.list_tools()
async def lista_verktyg() -> list[Tool]:
    return [
        Tool(
            name="diva_sok",
            description=(
                "Söker i DiVA-Portal (Digitala Vetenskapliga Arkivet) — ~1,5 miljoner "
                "vetenskapliga publikationer från ~50 svenska lärosäten och myndigheter. "
                "Täcker doktorsavhandlingar, licentiatavhandlingar, vetenskapliga artiklar, "
                "böcker, rapporter, konferensbidrag och examensarbeten. "
                "Varje träff innehåller metadata, abstract och epistemisk_status "
                "(poäng 1–7 för källans tillförlitlighet). "
                "Flerspråkig sökning: kommaseparerade termer ger separata parallella anrop "
                "till DiVA — ett anrop per term — som sedan mergas och dedupliceras. "
                "Flerordstermer bevaras som fraser (implicit AND). "
                "Exempel: 'sokterm': 'rättssäkerhet,rule of law,Rechtssicherheit'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": (
                            "Sökterm eller kommaseparerade termer. Varje kommasegment "
                            "blir ett eget DiVA-anrop. Flerordstermer fungerar som "
                            "frasmatchning (implicit AND): 'rule of law' söker efter "
                            "poster som innehåller alla tre orden. Tvåordstermer citeras "
                            "automatiskt för precis frasmatchning."
                        ),
                    },
                    "publikationstyp": {
                        "type": "string",
                        "description": (
                            "Filtrera på publikationstyp. Möjliga värden: "
                            "doctoralThesis, licentiateThesis, article, review, "
                            "book, chapter, conferencePaper, report, studentThesis, other. "
                            "Kommaseparera för flera typer."
                        ),
                    },
                    "fran_ar": {
                        "type": "integer",
                        "description": "Publicerat från och med detta år. Kräver till_ar.",
                    },
                    "till_ar": {
                        "type": "integer",
                        "description": "Publicerat till och med detta år. Kräver fran_ar.",
                    },
                    "laerosate": {
                        "type": "string",
                        "description": "Begränsa till ett lärosäte (klartext).",
                    },
                    "open_access": {
                        "type": "boolean",
                        "description": "Om true: returnera bara poster med öppen fulltext.",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "description": "Antal träffar (1–250). Standard: 20.",
                    },
                },
                "required": ["sokterm"],
            },
        ),
        Tool(
            name="diva_hamta_post",
            description=(
                "Hämtar fullständig metadata för en enskild DiVA-post. "
                "Ange diva_id (t.ex. 'diva2:123456'), urn (URN:NBN) eller doi. "
                "Minst ett av dessa fält krävs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "diva_id": {
                        "type": "string",
                        "description": "DiVA-id, t.ex. 'diva2:123456'.",
                    },
                    "urn": {
                        "type": "string",
                        "description": "URN:NBN, t.ex. 'urn:nbn:se:uu:diva-12345'.",
                    },
                    "doi": {
                        "type": "string",
                        "description": "DOI, t.ex. '10.1234/example'.",
                    },
                },
            },
        ),
        Tool(
            name="diva_hamta_fulltext",
            description=(
                "Hämtar och cachar fulltext-PDF on-demand för en DiVA-post. "
                "Returnerar extraherad text som markdown. "
                "Kräver att posten har öppen fulltext (open_access=true i diva_sok-svaret). "
                "Återanvänder cache vid upprepade anrop."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "diva_id": {
                        "type": "string",
                        "description": "DiVA-id, t.ex. 'diva2:123456'.",
                    },
                },
                "required": ["diva_id"],
            },
        ),
        Tool(
            name="diva_relaterade",
            description=(
                "Hittar publikationer relaterade till en given DiVA-post. "
                "Söker baserat på samma författare, samma ämnesområde eller samma organisation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "diva_id": {
                        "type": "string",
                        "description": "DiVA-id för utgångspunkten.",
                    },
                    "relationstyp": {
                        "type": "string",
                        "enum": ["forfattare", "amne", "organisation"],
                        "description": (
                            "forfattare: andra verk av samma författare. "
                            "amne: publikationer inom samma ämnesområde (standard). "
                            "organisation: publikationer från samma lärosäte."
                        ),
                    },
                    "max_traffar": {
                        "type": "integer",
                        "description": "Max antal relaterade poster (1–50). Standard: 10.",
                    },
                },
                "required": ["diva_id"],
            },
        ),
    ]


@_server.call_tool()
async def anropa_verktyg(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "diva_sok":
            resultat = await _verktyg_diva_sok(arguments)
        elif name == "diva_hamta_post":
            resultat = await _verktyg_diva_hamta_post(arguments)
        elif name == "diva_hamta_fulltext":
            resultat = await _verktyg_diva_hamta_fulltext(arguments)
        elif name == "diva_relaterade":
            resultat = await _verktyg_diva_relaterade(arguments)
        else:
            resultat = {"fel": f"Okänt verktyg: {name}"}
    except Exception as e:
        _logg.exception("Oväntat fel i verktyg %s: %s", name, e)
        resultat = {"fel": str(e)}

    return [TextContent(type="text", text=json.dumps(resultat, ensure_ascii=False, indent=2))]

# ── Verktygsimplementationer ───────────────────────────────────────────────────

async def _verktyg_diva_sok(args: dict) -> dict:
    sokterm_ra = args.get("sokterm", "").strip()
    if not sokterm_ra:
        return {"fel": "sokterm krävs"}

    # Valfri flerspråkig expansion
    sokterm_expanderad = _expandera_sokterm(sokterm_ra)

    # Dela upp i enskilda söktermer (kommaseparerade)
    alla_termer = [t.strip() for t in sokterm_expanderad.split(",") if t.strip()]
    # Deduplicera (case-insensitivt), bevara ordning
    sett_termer: set[str] = set()
    termer: list[str] = []
    for t in alla_termer:
        if t.lower() not in sett_termer:
            sett_termer.add(t.lower())
            termer.append(t)

    publikationstyper_ra = args.get("publikationstyp", "")
    publikationstyper = (
        [p.strip() for p in publikationstyper_ra.split(",") if p.strip()]
        if publikationstyper_ra else []
    )

    # max_traffar: mjuk övre gräns för totalt returnerade poster.
    # Default 200 — vitsen med AI-stöd är att hantera stora datamängder,
    # inte att efterlikna en googles förstasida.
    max_traffar  = int(args.get("max_traffar", 200))
    fran_ar      = args.get("fran_ar")
    till_ar      = args.get("till_ar")
    laerosate    = args.get("laerosate", "").strip()
    oa_filter    = args.get("open_access", False)

    # Per term: hämta 200 rader från DiVA (mer vid typfiltrering).
    # Utan tillräcklig volym per term täcker poolen bara de senaste åren.
    hamta_rader = 300 if publikationstyper else 200

    def _bygg_params(term: str) -> dict:
        """Bygger DiVA-sökparametrar för en enskild term."""
        p: dict[str, Any] = {
            "searchtype": "all",
            "freetext":   _formatera_enkel_sokterm(term),
            "rows":       hamta_rader,
            # Ingen year-sortering — DiVA sorterar på relevans,
            # vi sorterar klient-sidan på epistemisk_status + år.
        }
        # Årsfiltrering via DiVA:s aq-format: {"dateIssued": {"from":..., "to":...}}
        # Hanteras av _hamta_diva_export som bäddar in det i AND-gruppen med freeText.
        if fran_ar and till_ar:
            p["ar_fran"] = str(int(fran_ar))
            p["ar_till"] = str(int(till_ar))
        if laerosate:
            p["organisation"] = laerosate
        if oa_filter:
            p["fulltext"] = "true"
        return p

    async def _sok_en_term(term: str) -> tuple[str, list[dict]]:
        """Kör ett DiVA-anrop för en enskild term i en trådpool."""
        poster = await asyncio.to_thread(_hamta_diva_export, _bygg_params(term))
        return term, poster

    # Kör alla termer parallellt
    _logg.info(
        "diva_sok: %d termer parallellt: %s",
        len(termer),
        ", ".join(f'"{t}"' for t in termer),
    )
    resultat = await asyncio.gather(
        *[_sok_en_term(t) for t in termer],
        return_exceptions=True,
    )

    # Merge: deduplicera på diva_id, notera träffande sökterm
    sett_ids: set[str] = set()
    alla_poster: list[dict] = []
    for res in resultat:
        if isinstance(res, Exception):
            _logg.warning("Sökanrop misslyckades: %s", res)
            continue
        term, poster = res
        for post in poster:
            pid = post.get("diva_id", "")
            if pid and pid not in sett_ids:
                sett_ids.add(pid)
                post["_sokterm"] = term
                alla_poster.append(post)

    # Client-side filtrering på publikationstyp
    if publikationstyper:
        typer_set = {p.lower() for p in publikationstyper}
        alla_poster = [
            p for p in alla_poster
            if _typ_for_filter(p.get("publikationstyp", "")).lower() in typer_set
            or p.get("publikationstyp", "").lower() in typer_set
        ]

    # Sortera: epistemisk poäng desc, sedan år desc
    alla_poster.sort(
        key=lambda p: (
            p.get("epistemisk_status", {}).get("total", 0),
            p.get("ar") or 0,
        ),
        reverse=True,
    )

    # Begränsa till max_traffar
    alla_poster = alla_poster[:max_traffar]

    # Vilka termer gav faktiska träffar
    termer_med_traffar = sorted({
        p["_sokterm"] for p in alla_poster if p.get("_sokterm")
    })

    if not alla_poster:
        return {
            "sokterm_original":    sokterm_ra,
            "antal_termer_sokta":  len(termer),
            "antal_traffar":       0,
            "poster":              [],
            "meddelande": (
                "Inga träffar hittades i DiVA. "
                "Söktes som separata anrop per term: "
                + ", ".join(f'"{t}"' for t in termer)
            ),
        }

    return {
        "sokterm_original":   sokterm_ra,
        "antal_termer_sokta": len(termer),
        "termer_med_traffar": termer_med_traffar,
        "antal_traffar":      len(alla_poster),
        "poster":             alla_poster,
    }


async def _verktyg_diva_hamta_post(args: dict) -> dict:
    diva_id = args.get("diva_id", "").strip()
    urn     = args.get("urn", "").strip()
    doi     = args.get("doi", "").strip()

    if not any([diva_id, urn, doi]):
        return {"fel": "Ange minst ett av: diva_id, urn, doi"}

    import json as _json

    if diva_id:
        # Direktuppslagning via aq-filter på PID — returnerar exakt den begärda posten
        poster = _hamta_diva_export({
            "searchtype": "all",
            "aq":         _json.dumps([[{"pid": diva_id}]]),
            "noOfRows":   1,
        })
    elif urn:
        # URN-sökning via freetext (DiVA indexerar URN:NBN i fritextsökning)
        poster = _hamta_diva_export({
            "searchtype": "all",
            "freetext":   urn,
            "noOfRows":   3,
        })
        # Filtrera på exakt URN-match
        poster = [p for p in poster if p.get("urn", "").lower() == urn.lower()]
    else:
        # DOI-sökning
        poster = _hamta_diva_export({
            "searchtype": "all",
            "freetext":   doi,
            "noOfRows":   3,
        })
        poster = [p for p in poster if p.get("doi", "").lower() == doi.lower()]

    if not poster:
        return {"fel": f"Ingen post hittades för: {diva_id or urn or doi}"}

    return poster[0]


async def _verktyg_diva_hamta_fulltext(args: dict) -> dict:
    diva_id = args.get("diva_id", "").strip()
    if not diva_id:
        return {"fel": "diva_id krävs"}

    # Kolla cache
    cachad = _hamta_cachad_fulltext(diva_id)
    if cachad:
        return {
            "diva_id":      diva_id,
            "kalla":        "cache",
            "fulltext_md":  cachad,
            "tecken":       len(cachad),
        }

    # Hämta metadata för att få fulltext_url
    post = await _verktyg_diva_hamta_post({"diva_id": diva_id})
    if "fel" in post:
        return post

    fulltext_url = post.get("fulltext_url", "")
    urn          = post.get("urn", "")

    if not fulltext_url and not urn:
        return {
            "fel":         f"Ingen öppen fulltext tillgänglig för {diva_id}",
            "open_access": post.get("open_access", False),
            "tips":        "Kontrollera om posten har open_access=true i diva_sok-svaret.",
        }

    fulltext_md = _hamta_pdf_fulltext(diva_id, fulltext_url, urn)

    if not fulltext_md:
        return {"fel": f"Kunde inte extrahera text från {diva_id}"}

    _spara_fulltext(
        diva_id=diva_id,
        urn=urn,
        doi=post.get("doi", ""),
        titel=post.get("titel", ""),
        ar=post.get("ar") or 0,
        publikationstyp=post.get("publikationstyp", ""),
        fulltext_url=fulltext_url,
        fulltext_md=fulltext_md,
    )

    return {
        "diva_id":     diva_id,
        "kalla":       "diva",
        "fulltext_md": fulltext_md,
        "tecken":      len(fulltext_md),
    }


async def _verktyg_diva_relaterade(args: dict) -> dict:
    diva_id      = args.get("diva_id", "").strip()
    relationstyp = args.get("relationstyp", "amne")
    max_traffar  = min(int(args.get("max_traffar", 10)), 50)

    if not diva_id:
        return {"fel": "diva_id krävs"}

    post = await _verktyg_diva_hamta_post({"diva_id": diva_id})
    if "fel" in post:
        return post

    diva_params: dict[str, Any] = {
        "searchtype": "all",
        "noOfRows":   max_traffar + 1,  # +1 för att filtrera bort ursprungsposten
        "sort":       "year desc",
    }
    beskrivning = ""

    if relationstyp == "forfattare":
        forfattare_rad = post.get("forfattare", "")
        if not forfattare_rad:
            return {"fel": "Ingen författarinformation tillgänglig för denna post"}
        # Ta förste författarens namn (DiVA-format: "Efternamn, Förnamn [id] (institution);...")
        # Extrahera bara "Efternamn, Förnamn" — klipp bort [id] och (institution)
        import re as _re
        forste_full = forfattare_rad.split(";")[0].strip()
        # Ta bort [...] och (...) suffix
        forste = _re.sub(r'\s*[\[\(].*', '', forste_full).strip()
        if not forste:
            return {"fel": "Kunde inte tolka författarnamn"}
        # Använd aq-sökning på name-fältet för exakt namnsökning
        import json as _json
        diva_params["aq"] = _json.dumps([[{"name": forste}]])
        beskrivning = f"Andra verk av {forste}"

    elif relationstyp == "amne":
        amne      = post.get("amne", "")
        nyckelord = post.get("nyckelord", "")
        # Välj primärt ämne: nationell ämneskategori > nyckelord > titel
        kandidat = amne or nyckelord or post.get("titel", "")
        sokterm_amne = kandidat.split(";")[0].split(",")[0].strip()[:80]
        if not sokterm_amne:
            return {"fel": "Ingen ämneskategori eller nyckelord tillgängliga för denna post"}
        diva_params["freetext"] = sokterm_amne
        beskrivning = f"Publikationer inom ämnet: {sokterm_amne}"

    elif relationstyp == "organisation":
        laerosate = post.get("laerosate", "")
        if not laerosate:
            return {"fel": "Ingen organisationsinformation tillgänglig för denna post"}
        diva_params["organisation"] = laerosate
        diva_params["searchtype"]   = "postgraduate"
        beskrivning = f"Publikationer från {laerosate}"

    else:
        return {"fel": f"Okänd relationstyp: {relationstyp}. Tillåtna: forfattare, amne, organisation"}

    poster = _hamta_diva_export(diva_params)
    # Filtrera bort ursprungsposten
    poster = [p for p in poster if p.get("diva_id") != diva_id][:max_traffar]

    return {
        "ursprung_diva_id": diva_id,
        "relationstyp":     relationstyp,
        "beskrivning":      beskrivning,
        "antal":            len(poster),
        "poster":           poster,
    }

# ── Serverstart ───────────────────────────────────────────────────────────────

def _starta_server() -> None:
    _db_init()
    _logg.info("DiVA-Portal MCP-server startar (transport=%s)", MCP_TRANSPORT)

    if MCP_TRANSPORT == "http":
        _starta_http()
    else:
        asyncio.run(_starta_stdio())


async def _starta_stdio() -> None:
    async with stdio_server() as (las, skriv):
        await _server.run(las, skriv, _server.create_initialization_options())


def _starta_http() -> None:
    """HTTP-transport med Bearer-token-autentisering (Starlette + uvicorn)."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
    import uvicorn

    try:
        from mcp.server.sse import SseServerTransport
    except ImportError as e:
        _logg.error("HTTP-transport kräver mcp[sse]: %s", e)
        raise

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            if MCP_API_KEY:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {MCP_API_KEY}":
                    return Response("Obehörig åtkomst", status_code=401)
            return await call_next(request)

    sse = SseServerTransport("/sse")

    async def hantera_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (las, skriv):
            await _server.run(las, skriv, _server.create_initialization_options())

    app = Starlette(
        middleware=[Middleware(BearerAuth)],
        routes=list(sse.router.routes),
    )

    _logg.info("HTTP-server lyssnar på %s:%s", MCP_HOST, MCP_PORT)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    _starta_server()
