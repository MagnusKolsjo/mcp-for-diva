#!/usr/bin/env python3
"""
MCP-server för DiVA (Digitala Vetenskapliga Arkivet).

Exponerar fyra verktyg:
  diva_sok            — Sök bland ~1,5 miljoner poster
  diva_hamta_post     — Hämta fullständig metadata för en post via ID
  diva_hamta_fulltext — Hämta och casha PDF-fulltext on-demand
  diva_relaterade     — Hitta relaterade publikationer

Transport: stdio (standard) eller HTTP (MCP_TRANSPORT=http).
Databas:   PostgreSQL (standard) eller SQLite (DATABASE_URL=sqlite:///...).
           Hanteras av db.py — se den modulen för anslutningsdetaljer.
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
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

import db

# ── Konfiguration ─────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT", "8015"))
MCP_API_KEY   = os.getenv("MCP_API_KEY", "")

_PDF_CACHE = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
if not _PDF_CACHE.is_absolute():
    _PDF_CACHE = _SCRIPT_DIR / _PDF_CACHE
_PDF_CACHE.mkdir(parents=True, exist_ok=True)

PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "7"))

QUERY_EXPANSION_ENABLED  = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_API_URL  = os.getenv("QUERY_EXPANSION_API_URL", "")
QUERY_EXPANSION_API_KEY  = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL    = os.getenv("QUERY_EXPANSION_MODEL", "claude-haiku-4-5-20251001")

# Epistemisk status — grundpoäng per publikationstyp (konfigurerbart via .env)
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
    # Examensarbeten: avancerad vs grund avgörs via ThesisLevel-kolumnen
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

# Standardtak för fulltext i diva_hamta_fulltext. Avhandlingar kan vara över en
# miljon tecken och överskrida MCP-protokollets storleksgräns, vilket får anropet
# att misslyckas helt. Anroparen kan höja taket eller sätta 0 för hela texten.
DIVA_MAX_TECKEN = int(os.getenv("DIVA_MAX_TECKEN", "60000"))

# ── FD1-skydd (MCP-stdio-hygien) ──────────────────────────────────────────────

@contextlib.contextmanager
def _tysta_fd1():
    """Omdirigerar FD 1+2 till loggfil under anrop som kan skriva till stdout."""
    loggfil = _LOGG_DIR / "subprocess.log"
    spar_ut  = os.dup(1)
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

# ── DiVA API-konfiguration ────────────────────────────────────────────────────

_DIVA_EXPORT_URL = "https://www.diva-portal.org/smash/export.jsf"
_DIVA_HUVUDEN = {
    "User-Agent": "mcp-for-diva/1.0 (+https://github.com/MagnusKolsjo/mcp-for-diva)",
    "Accept":     "text/csv,text/plain,*/*",
    "Referer":    "https://www.diva-portal.org/",
}

# ── Lärosätesfiltrering (Bugg 2) ──────────────────────────────────────────────
#
# DiVA:s export-API kräver ett numeriskt organisations-ID i aq-strukturen
# för lärosätesfiltrering. URL-parametern "organisation=<klartext>" ignoreras
# tyst. Denna dict mappar lärosätesnamn → DiVA-organisations-ID.
#
# Lägg till fler via DiVA-portalen: sök efter organisationen, inspektera
# URL-parametern "organisationId" i sökfrågan.
_LAEROSATE_ORG_ID: dict[str, str] = {
    # Uppsala universitet — verifierat 2026-05-18 (audit Bugg 2)
    "uppsala":                     "4853",
    "uppsala university":          "4853",
    "uppsala universitet":         "4853",
    # Lägg till fler efter verifiering mot DiVA-portalen
}

# ── Kolumnmappning: internt fältnamn → möjliga CSV-rubriker ──────────────────
#
# DiVA export.jsf (csvall2) skickar engelska CamelCase-kolumner.
# Algoritmen tar den *första* matchande rubriken per fältnamn.
_KOLUMN_ALIAS: dict[str, list[str]] = {
    "diva_id":           ["pid", "postid", "post id"],
    "forfattare":        ["name", "författare", "author", "authors"],
    "titel":             ["title", "titel"],
    "publikationstyp":   ["publicationtype", "publikationstyp", "publication type",
                          "type", "typ"],
    "sprak":             ["language", "språk", "sprak"],
    "ar":                ["year", "år"],
    "abstract":          ["abstract", "sammanfattning"],
    "doi":               ["doi"],
    "urn":               ["nbn", "urn:nbn", "urn", "uri"],
    "nyckelord":         ["keywords", "nyckelord"],
    # amne: Categories är primär (Bg2) — ResearchSubjects är ofta tom
    "amne":              ["categories", "researchsubjects",
                          "nationell ämneskategori", "subject", "subjects"],
    # laerosate: extraheras ur Name-fältet (Bugg 3) — ingen direkt CSV-kolumn
    "laerosate":         ["organisation", "university", "institution"],
    "tidskrift":         ["journal", "tidskrift"],
    # issn: DiVA-CSV:n har JournalISSN/JournalEISSN/SeriesISSN — aldrig bara ISSN (Bg1)
    "issn":              ["journalissn", "journaleissn", "seriesissn", "serieseissn"],
    "fulltext_url":      ["fulltextlink", "länk till fulltext",
                          "fulltext url", "link to fulltext"],
    "granskad":          ["reviewed", "granskad", "peer reviewed", "refereed"],
    "fri_fulltext":      ["freefulltext", "fri fulltext", "free fulltext", "open access"],
    "disputationsdatum": ["defencedate", "disputationsdatum", "defense date"],
    "handledare":        ["supervisors", "handledare", "supervisor"],
    "examinator":        ["examiners", "examinator", "examiner"],
    "isbn":              ["isbn"],
    "foerlag":           ["publisher", "förlag"],
    "underkategori":     ["publicationsubtype", "underkategori", "subcategory"],
    # Citationsfält för artiklar och böcker (Bg3)
    "volym":             ["volume"],
    "nummer":            ["issue"],
    "startpage":         ["startpage", "start page"],
    "slutpage":          ["endpage", "end page"],
    "sidor":             ["pages"],
    "hostpublication":   ["hostpublication", "host publication"],
    # Examensarbetesnivå för kalibrerad epistemisk poäng (Bg4)
    "thesislevel":       ["thesislevel", "thesis level"],
}

# DiVA returnerar svenska displaysträngar i PublicationType-kolumnen.
# Mappa till interna koder som _berakna_epistemisk_status förstår.
_DIVA_TYP_TILL_INTERN: dict[str, str] = {
    "doktorsavhandling, monografi":        "monographDoctoralThesis",
    "doktorsavhandling, sammanläggning":   "comprehensiveDoctoralThesis",
    "doktorsavhandling":                   "doctoralThesis",
    "licentiatavhandling, monografi":      "monographLicentiateThesis",
    "licentiatavhandling, sammanläggning": "comprehensiveLicentiateThesis",
    "licentiatavhandling":                 "licentiateThesis",
    "artikel i tidskrift":                 "article",
    "artikel, forskningsöversikt":         "review",
    "artikel":                             "article",
    "recension":                           "bookReview",
    "bok":                                 "book",
    "samlingsverk":                        "collection",
    "samlingsverk (redaktörskap)":         "collection",
    "kapitel i bok, del av antologi":      "chapter",
    "kapitel i bok":                       "chapter",
    "konferensbidrag":                     "conferencePaper",
    "proceedings (redaktörskap)":          "conferenceProceedings",
    "rapport":                             "report",
    "studentuppsats (examensarbete)":      "studentThesis",
    "patent":                              "patent",
    "övrigt":                              "other",
    "konstnärlig output":                  "other",
    "manuskript (preprint)":               "manuscript",
}

# Föräldratyp-mappning: detaljerade koder → den kod användaren anger vid filtrering
_TYP_FOREALDRATYP: dict[str, str] = {
    "monographDoctoralThesis":       "doctoralThesis",
    "comprehensiveDoctoralThesis":   "doctoralThesis",
    "monographLicentiateThesis":     "licentiateThesis",
    "comprehensiveLicentiateThesis": "licentiateThesis",
    "collection":                    "book",
    "conferenceProceedings":         "conferencePaper",
}

# Blocklista: DiVA-poster som returneras som fallback vid noll verkliga träffar.
# Värden verifierade 2026-05-18 (audit Bg6): BTH-konferenspapper med CELEX-liknande ID.
# Utöka via DIVA_FILLER_IDS-miljövariabel (kommaseparerad) vid behov.
_extra_filler = {
    s.strip()
    for s in os.getenv("DIVA_FILLER_IDS", "").split(",")
    if s.strip()
}
_KANDA_FILLER_IDS: frozenset[str] = frozenset(
    {"diva2:833794", "diva2:837011"} | _extra_filler
)

# Stoppord som inte bidrar till relevansbedömning
_STOPPORD: frozenset[str] = frozenset({
    "i", "och", "av", "för", "till", "med", "på", "den", "det", "en", "ett",
    "de", "om", "är", "som", "att", "men", "har", "inte", "vi", "du", "han",
    "hon", "ni", "dem", "sig", "sin", "sitt", "sina", "inom", "under", "samt",
    "vid", "från", "kan", "mot", "över", "efter", "ut", "upp", "ner", "när",
    "of", "the", "a", "an", "and", "in", "to", "for", "on", "at", "by",
    "with", "from", "or", "not", "it", "is", "be", "as", "are", "its",
    "this", "that", "which", "have", "has", "had", "was", "were",
})

# ── Hjälpfunktioner för kolumnhantering ──────────────────────────────────────

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


def _extrahera_laerosate(name_field: str) -> str:
    """
    Extraherar lärosätets namn ur DiVA:s Name-fält.

    DiVA-format: "Efternamn, Förnamn [orcid] (Lärosäte [id], Fakultet [id]);..."
    Regex plockar det första segmentet inuti den första parentesen, före
    komma eller hakparentes — det är lärosätets namn.
    """
    if not name_field:
        return ""
    m = re.search(r'\(([^,\[\]]+)', name_field)
    if m:
        return m.group(1).strip()
    return ""


def _formatera_enkel_sokterm(term: str) -> str:
    """
    Formaterar en enskild sökterm för DiVA:s aq/freeText-fält (SOLR-baserat).

    Citerade termer bevaras med sina citattecken (frasmatchning).
    Ociterade termer skickas as-is (implicit AND — alla ord måste finnas).
    """
    return term.strip()


def _ar_relevant_for_term(post: dict, sokterm: str) -> bool:
    """
    Kontrollerar att posten innehåller minst ett signifikant ord från söktermen.
    Filtrerar bort DiVA:s filler-poster vid noll verkliga träffar.
    """
    sookord = [
        o.lower()
        for o in sokterm.split()
        if len(o) > 2 and o.lower() not in _STOPPORD
    ]
    if not sookord:
        return True

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
    har_postgraduate  = any(p.lower() not in undergraduate_typer for p in publikationstyper)
    if har_undergraduate and not har_postgraduate:
        return "undergraduate"
    if har_postgraduate and not har_undergraduate:
        return "postgraduate"
    return "all"

# ── Epistemisk status ─────────────────────────────────────────────────────────

def _berakna_epistemisk_status(
    publikationstyp: str,
    har_doi: bool,
    open_access: bool,
    granskad: str,
    examensarbete_niva: str = "",
) -> dict:
    """
    Beräknar epistemisk status (poäng 1–7) och returnerar poäng + motivering.

    examensarbete_niva: "grund" ger EPISTEMISK_EXAMENSARBETE_GRUND (default 1),
                        "" eller "avancerad" ger EPISTEMISK_EXAMENSARBETE_AVANCERAD
                        (default 2). Hämtas ur ThesisLevel-kolumnen (Bg4).
    """
    if publikationstyp == "studentThesis" and examensarbete_niva == "grund":
        grundpong = int(os.getenv("EPISTEMISK_EXAMENSARBETE_GRUND", "1"))
    else:
        grundpong = _EPISTEMISK_GRUNDPONG.get(publikationstyp, 1)

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
    typtext   = typbeskrivningar.get(publikationstyp, publikationstyp or "Okänd typ")
    motivering = typtext
    if bonus_delar:
        motivering += " + " + ", ".join(bonus_delar)

    stjarnor = "⭐" * min(totalt, 5)
    return {
        "pong":      totalt,
        "typtext":   typtext,
        "motivering": motivering,
        "display":   f"{stjarnor} ({totalt}/7) — {motivering}",
    }

# ── CSV-normalisering ─────────────────────────────────────────────────────────

def _normalisera_rad(rad: dict, rubrikindex: dict) -> dict | None:
    """Normaliserar en CSV-rad till ett standardiserat postdikt."""
    diva_id = _val(rad, rubrikindex, "diva_id")
    if not diva_id:
        return None

    # Konstruera diva2:NNNNN-format
    m = re.search(r'diva2:\d+', diva_id)
    if m:
        diva_id = m.group(0)
    elif diva_id.isdigit():
        diva_id = f"diva2:{diva_id}"
    elif "/" in diva_id:
        diva_id = diva_id.rstrip("/").split("/")[-1]

    # Filtrera bort kända filler-poster (modulnivå-konstant, Bg6)
    if diva_id in _KANDA_FILLER_IDS:
        return None

    # Normalisera publikationstyp: DiVA returnerar svenska displaysträngar
    publikationstyp_raa = _val(rad, rubrikindex, "publikationstyp")
    publikationstyp     = _DIVA_TYP_TILL_INTERN.get(
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

    # Filtrera bort poster med orimligt lågt årtal (Naturvårdsverket-rapporter
    # med korrupt årsdata: år 901, 905, etc.)
    if ar is not None and ar < 1000:
        return None

    # Examensarbetesnivå — avgör om grundnivå eller avancerad (Bg4)
    thesislevel        = _val(rad, rubrikindex, "thesislevel").lower()
    examensarbete_niva = ""
    if publikationstyp == "studentThesis":
        if "grundnivå" in thesislevel or "grund" in thesislevel:
            examensarbete_niva = "grund"
        elif "avancerad" in thesislevel:
            examensarbete_niva = "avancerad"

    epistemisk = _berakna_epistemisk_status(
        publikationstyp=publikationstyp,
        har_doi=har_doi,
        open_access=open_access,
        granskad=granskad_raw,
        examensarbete_niva=examensarbete_niva,
    )

    # Lärosäte: extraheras ur Name-fältet eftersom DiVA:s CSV saknar
    # en separat organisations-kolumn (Bugg 3)
    forfattare_raw = _val(rad, rubrikindex, "forfattare")
    laerosate = _extrahera_laerosate(forfattare_raw)

    return {
        "diva_id":           diva_id,
        "titel":             _val(rad, rubrikindex, "titel"),
        "forfattare":        forfattare_raw,
        "ar":                ar,
        "publikationstyp":   publikationstyp,
        "sprak":             _val(rad, rubrikindex, "sprak"),
        "abstract":          _val(rad, rubrikindex, "abstract"),
        "nyckelord":         _val(rad, rubrikindex, "nyckelord"),
        "amne":              _val(rad, rubrikindex, "amne"),
        "laerosate":         laerosate,
        "tidskrift":         _val(rad, rubrikindex, "tidskrift"),
        "issn":              _val(rad, rubrikindex, "issn"),
        "doi":               doi,
        "urn":               urn,
        "isbn":              _val(rad, rubrikindex, "isbn"),
        "foerlag":           _val(rad, rubrikindex, "foerlag"),
        # Citationsfält (Bg3)
        "volym":             _val(rad, rubrikindex, "volym"),
        "nummer":            _val(rad, rubrikindex, "nummer"),
        "sidor":             _val(rad, rubrikindex, "sidor"),
        "startpage":         _val(rad, rubrikindex, "startpage"),
        "slutpage":          _val(rad, rubrikindex, "slutpage"),
        "hostpublication":   _val(rad, rubrikindex, "hostpublication"),
        "fulltext_url":      fulltext_url,
        "open_access":       open_access,
        "granskad":          granskad_raw,
        "handledare":        _val(rad, rubrikindex, "handledare"),
        "examinator":        _val(rad, rubrikindex, "examinator"),
        "disputationsdatum": _val(rad, rubrikindex, "disputationsdatum"),
        "epistemisk_status": epistemisk,
    }

# ── DiVA export-API ───────────────────────────────────────────────────────────

def _hamta_diva_export(params: dict) -> list[dict]:
    """
    Anropar DiVA export.jsf med format=csvall2 och returnerar normaliserade poster.

    Interna parameternamn som hanteras av denna funktion:
      freetext  → konverteras till aq=[[{"freeText": ...}]]
      rows      → noOfRows
      sort      → sortOrder (år → dateIssued_sort_desc)
    """
    import json as _json

    params = dict(params)

    # Konvertera 'freetext' → 'aq' med korrekt JSON-format.
    # Årsfiltrering läggs till i AND-gruppen med freeText om ar_fran/ar_till finns.
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
            params["aq"]  = _json.dumps([and_villkor])
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

    text = svar.content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []

    forsta_rad = text.split("\n")[0] if "\n" in text else text[:500]
    separator  = ";" if forsta_rad.count(";") > forsta_rad.count(",") else ","

    lasare   = csv.DictReader(io.StringIO(text), delimiter=separator)
    rubriker = list(lasare.fieldnames or [])
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
                "pymupdf4llm är inte installerat. Kör: pip install pymupdf4llm"
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
                str(pdf_fil), str(ocr_fil),
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
                "Authorization":     f"Bearer {QUERY_EXPANSION_API_KEY}",
                "Content-Type":      "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":    QUERY_EXPANSION_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15.0,
        )
        svar.raise_for_status()
        data      = svar.json()
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
                "Söker i DiVA (Digitala Vetenskapliga Arkivet) — ~1,5 miljoner "
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
                            "poster som innehåller alla tre orden."
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
                        "description": (
                            "Begränsa till ett lärosäte (klartext, t.ex. 'Uppsala universitet'). "
                            "Kräver att lärosätets DiVA-ID finns i serverns lookup-tabell. "
                            "Vid okänt lärosäte returneras ett felmeddelande med instruktion."
                        ),
                    },
                    "open_access": {
                        "type": "boolean",
                        "description": "Om true: returnera bara poster med öppen fulltext.",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "description": "Antal träffar (1–250). Standard: 200.",
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
                    "max_tecken": {
                        "type": "integer",
                        "description": (
                            "Teckentak för fulltexten (standard 60 000, 0 = hela texten). "
                            "Avhandlingar kan vara över en miljon tecken; utan tak "
                            "misslyckas anropet mot svarsgränsen."
                        ),
                        "default": 60000,
                    },
                    "fran_tecken": {
                        "type": "integer",
                        "description": "Börja texten vid denna teckenposition — för att läsa vidare.",
                        "default": 0,
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
                            "organisation: publikationer från samma lärosäte "
                            "(kräver att lärosätets DiVA-ID finns i serverns lookup-tabell)."
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

    sokterm_expanderad = _expandera_sokterm(sokterm_ra)

    alla_termer = [t.strip() for t in sokterm_expanderad.split(",") if t.strip()]
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

    max_traffar = int(args.get("max_traffar", 200))
    fran_ar     = args.get("fran_ar")
    till_ar     = args.get("till_ar")
    laerosate   = args.get("laerosate", "").strip()
    oa_filter   = args.get("open_access", False)

    # Lärosätesfiltrering — slå upp DiVA-organisations-ID (Bugg 2)
    org_id: Optional[str] = None
    if laerosate:
        org_id = _LAEROSATE_ORG_ID.get(laerosate.lower())
        if not org_id:
            kanda = ", ".join(
                sorted({v for v in _LAEROSATE_ORG_ID.keys()
                        if not v.replace(" ", "").isdigit()})
            )
            return {
                "fel": (
                    f"Organisationsfiltrering kräver ett DiVA-organisations-ID. "
                    f"Ingen mappning hittades för '{laerosate}'. "
                    f"Lärosäten med känd mappning: {kanda}. "
                    "Lägg till fler via DiVA-portalen: sök på organisationens namn "
                    "och inspektera URL-parametern 'organisationId' i sökfrågan."
                )
            }

    hamta_rader = 300 if publikationstyper else 200

    def _bygg_params(term: str) -> dict:
        """Bygger DiVA-sökparametrar för en enskild term med korrekt aq-format."""
        import json as _json
        p: dict[str, Any] = {"searchtype": "all", "rows": hamta_rader}

        # Bygg AND-grupp i aq med freeText, eventuell årsfiltrering och org-filter
        and_villkor: list[dict] = [{"freeText": _formatera_enkel_sokterm(term)}]
        if fran_ar and till_ar:
            and_villkor.append(
                {"dateIssued": {"from": str(int(fran_ar)), "to": str(int(till_ar))}}
            )
        if org_id:
            and_villkor.append({"organisationId": org_id, "organisationId-Xtra": False})

        p["aq"]  = _json.dumps([and_villkor])
        p["aqe"] = "[]"
        p["af"]  = "[]"
        p["aq2"] = "[[]]"

        if oa_filter:
            p["onlyFullText"] = "true"
        return p

    async def _sok_en_term(term: str) -> tuple[str, list[dict]]:
        poster = await asyncio.to_thread(_hamta_diva_export, _bygg_params(term))
        return term, poster

    _logg.info(
        "diva_sok: %d termer parallellt: %s",
        len(termer),
        ", ".join(f'"{t}"' for t in termer),
    )
    resultat = await asyncio.gather(
        *[_sok_en_term(t) for t in termer],
        return_exceptions=True,
    )

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

    if publikationstyper:
        typer_set = {p.lower() for p in publikationstyper}
        alla_poster = [
            p for p in alla_poster
            if _typ_for_filter(p.get("publikationstyp", "")).lower() in typer_set
            or p.get("publikationstyp", "").lower() in typer_set
        ]

    # Sortera: epistemisk poäng desc, sedan år desc (Bugg 1: använd "pong" inte "total")
    alla_poster.sort(
        key=lambda p: (
            p.get("epistemisk_status", {}).get("pong", 0),
            p.get("ar") or 0,
        ),
        reverse=True,
    )

    alla_poster = alla_poster[:max_traffar]

    termer_med_traffar = sorted({
        p["_sokterm"] for p in alla_poster if p.get("_sokterm")
    })

    if not alla_poster:
        return {
            "sokterm_original":    sokterm_ra,
            "antal_termer_sokta":  len(termer),
            "antal_returnerade":   0,
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
        # Bg5: antal_returnerade (inte antal_traffar) — tydliggör att det
        # är antalet returnerade poster, inte DiVA-databasens totalantal
        "antal_returnerade":  len(alla_poster),
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
        poster = _hamta_diva_export({
            "searchtype": "all",
            "aq":         _json.dumps([[{"pid": diva_id}]]),
            "noOfRows":   1,
        })
    elif urn:
        poster = _hamta_diva_export({
            "searchtype": "all",
            "freetext":   urn,
            "noOfRows":   3,
        })
        poster = [p for p in poster if p.get("urn", "").lower() == urn.lower()]
    else:
        poster = _hamta_diva_export({
            "searchtype": "all",
            "freetext":   doi,
            "noOfRows":   3,
        })
        poster = [p for p in poster if p.get("doi", "").lower() == doi.lower()]

    if not poster:
        return {"fel": f"Ingen post hittades för: {diva_id or urn or doi}"}

    return poster[0]


def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if max_tecken and max_tecken > 0 and len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag    = utdrag.rstrip()
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
    }


async def _verktyg_diva_hamta_fulltext(args: dict) -> dict:
    diva_id = args.get("diva_id", "").strip()
    if not diva_id:
        return {"fel": "diva_id krävs"}

    max_tecken  = int(args.get("max_tecken", DIVA_MAX_TECKEN) or 0)
    fran_tecken = int(args.get("fran_tecken", 0) or 0)

    cachad = db.hamta_cachad_fulltext(diva_id)
    if cachad:
        utdrag = _skar_ut(cachad, max_tecken, fran_tecken)
        return {
            "diva_id":     diva_id,
            "kalla":       "cache",
            "fulltext_md": utdrag["text"],
            "tecken_totalt":        utdrag["tecken_totalt"],
            "tecken_visade":        utdrag["tecken_visade"],
            "trunkerad":            utdrag["trunkerad"],
            "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
        }

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

    db.spara_fulltext(
        diva_id=diva_id,
        urn=urn,
        doi=post.get("doi", ""),
        titel=post.get("titel", ""),
        ar=post.get("ar") or 0,
        publikationstyp=post.get("publikationstyp", ""),
        fulltext_url=fulltext_url,
        fulltext_md=fulltext_md,
    )

    # Databasen har alltid hela texten — trunkeringen gäller bara svaret.
    utdrag = _skar_ut(fulltext_md, max_tecken, fran_tecken)
    return {
        "diva_id":     diva_id,
        "kalla":       "diva",
        "fulltext_md": utdrag["text"],
        "tecken_totalt":        utdrag["tecken_totalt"],
        "tecken_visade":        utdrag["tecken_visade"],
        "trunkerad":            utdrag["trunkerad"],
        "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
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
        "noOfRows":   max_traffar + 1,
        "sort":       "year desc",
    }
    beskrivning = ""

    if relationstyp == "forfattare":
        forfattare_rad = post.get("forfattare", "")
        if not forfattare_rad:
            return {"fel": "Ingen författarinformation tillgänglig för denna post"}
        import re as _re
        forste_full = forfattare_rad.split(";")[0].strip()
        forste      = _re.sub(r'\s*[\[\(].*', '', forste_full).strip()
        if not forste:
            return {"fel": "Kunde inte tolka författarnamn"}
        import json as _json
        diva_params["aq"] = _json.dumps([[{"name": forste}]])
        beskrivning = f"Andra verk av {forste}"

    elif relationstyp == "amne":
        amne      = post.get("amne", "")
        nyckelord = post.get("nyckelord", "")
        kandidat  = amne or nyckelord or post.get("titel", "")
        sokterm_amne = kandidat.split(";")[0].split(",")[0].strip()[:80]
        if not sokterm_amne:
            return {"fel": "Ingen ämneskategori eller nyckelord tillgängliga för denna post"}
        diva_params["freetext"] = sokterm_amne
        beskrivning = f"Publikationer inom ämnet: {sokterm_amne}"

    elif relationstyp == "organisation":
        laerosate_post = post.get("laerosate", "")
        if not laerosate_post:
            return {"fel": "Ingen organisationsinformation tillgänglig för denna post"}
        org_id = _LAEROSATE_ORG_ID.get(laerosate_post.lower())
        if not org_id:
            return {
                "fel": (
                    f"Organisationsfiltrering kräver DiVA-organisations-ID. "
                    f"Ingen mappning för '{laerosate_post}'. "
                    "Utöka _LAEROSATE_ORG_ID-tabellen med verifierade ID:n."
                )
            }
        import json as _json
        diva_params["aq"] = _json.dumps(
            [[{"organisationId": org_id, "organisationId-Xtra": False}]]
        )
        diva_params["searchtype"] = "postgraduate"
        beskrivning = f"Publikationer från {laerosate_post}"

    else:
        return {
            "fel": (
                f"Okänd relationstyp: {relationstyp}. "
                "Tillåtna: forfattare, amne, organisation"
            )
        }

    poster = _hamta_diva_export(diva_params)
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
    db.initiera_schema()  # try/except hanteras inuti funktionen
    _logg.info("DiVA MCP-server startar (transport=%s)", MCP_TRANSPORT)

    if MCP_TRANSPORT == "http":
        _starta_http()
    else:
        asyncio.run(_starta_stdio())


async def _starta_stdio() -> None:
    async with stdio_server() as (las, skriv):
        await _server.run(las, skriv, _server.create_initialization_options())


def _starta_http() -> None:
    """
    HTTP-transport med Bearer-token-autentisering (Starlette + uvicorn).

    Använder SseServerTransport (äldre mcp-stil) — migrering till
    streamable_http_app() är planerad men ej brådskande för stdio-användare.
    Se audit 2026-05-18, Konvention 12.
    """
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
