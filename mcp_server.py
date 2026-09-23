#!/usr/bin/env python3
"""
MCP-server för DiVA (Digitala Vetenskapliga Arkivet).

Exponerar fyra verktyg:
  diva_sok            — Sök bland ~1,5 miljoner poster
  diva_hamta_post     — Hämta fullständig metadata för en post via ID
  diva_hamta_fulltext — Hämta och casha PDF-fulltext on-demand
  diva_relaterade     — Hitta relaterade publikationer

Källa:     DiVA:s export.jsf i formatet csvall2. Ett svar som inte är den
           väntade CSV-exporten ger ett fel som säger att källan kan ha bytt
           plattform, i stället för tomma träffar.
Transport: stdio eller http (MCP_TRANSPORT), se mcp_transport.py.
Databas:   PostgreSQL eller SQLite (DATABASE_URL), se db.py.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from typing_extensions import NotRequired, TypedDict

import db
from mcp_annotationer import CACHE_HINTAR, LASNING_EXTERN
from mcp_transport import starta

# ── Konfiguration ─────────────────────────────────────────────────────────────

# Senaste släppta version enligt CHANGELOG.md.
SERVER_VERSION = "1.2.0"

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

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
# att misslyckas helt. Anroparen kan höja taket upp till DIVA_MAX_TECKEN_TAK.
#
# Övre taket gäller alltid, även för max_tecken=0: svaret skickas både som
# JSON-text och som structuredContent, så 300 000 tecken blir omkring 650 KB —
# under klienternas gräns på ungefär 1 MB. Längre texter läses i delar med
# fran_tecken.
DIVA_MAX_TECKEN_TAK = 300_000
DIVA_MAX_TECKEN = min(int(os.getenv("DIVA_MAX_TECKEN", "60000")), DIVA_MAX_TECKEN_TAK)

# ── Utdata från C-bibliotek ───────────────────────────────────────────────────
#
# MuPDF och Tesseract skriver varningar direkt till fil 1 och 2, förbi Pythons
# sys.stdout. MCP-transporten skyddar själv sin kanal i stdio-läget, men
# utskrifterna hör hemma i loggen och inte i klientens stderr.
#
# Omdirigeringen gäller hela processen. Verktygen körs på arbetstrådar, så två
# samtidiga extraktioner skulle annars kunna återställa varandras
# fildeskriptorer i fel ordning. Låset gör omdirigeringen till en i taget.
_FD_LAS = threading.Lock()


@contextlib.contextmanager
def _tysta_fd1():
    """Omdirigerar FD 1+2 till loggfil under anrop som kan skriva till stdout."""
    with _FD_LAS, _omdirigera_fd1_och_fd2():
        yield


@contextlib.contextmanager
def _omdirigera_fd1_och_fd2():
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

# ── Lärosätesfiltrering ───────────────────────────────────────────────────────
#
# DiVA:s export-API kräver ett numeriskt organisations-ID i aq-strukturen
# för lärosätesfiltrering. URL-parametern "organisation=<klartext>" ignoreras
# tyst. Denna dict mappar lärosätesnamn → DiVA-organisations-ID.
#
# Lägg till fler via DiVA-portalen: sök efter organisationen, inspektera
# URL-parametern "organisationId" i sökfrågan.
_LAEROSATE_ORG_ID: dict[str, str] = {
    # Uppsala universitet — verifierat mot DiVA-portalen
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
    # amne: Categories är primär — ResearchSubjects är ofta tom
    "amne":              ["categories", "researchsubjects",
                          "nationell ämneskategori", "subject", "subjects"],
    # laerosate: extraheras ur Name-fältet — ingen direkt CSV-kolumn
    "laerosate":         ["organisation", "university", "institution"],
    "tidskrift":         ["journal", "tidskrift"],
    # issn: DiVA-CSV:n har JournalISSN/JournalEISSN/SeriesISSN — aldrig bara ISSN
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
    # Citationsfält för artiklar och böcker
    "volym":             ["volume"],
    "nummer":            ["issue"],
    "startpage":         ["startpage", "start page"],
    "slutpage":          ["endpage", "end page"],
    "sidor":             ["pages"],
    "hostpublication":   ["hostpublication", "host publication"],
    # Examensarbetesnivå för kalibrerad epistemisk poäng
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
# Verifierade mot DiVA: BTH-konferenspapper med CELEX-liknande ID.
# Utöka via DIVA_FILLER_IDS-miljövariabel (kommaseparerad) vid behov.
_extra_filler = {
    s.strip()
    for s in os.getenv("DIVA_FILLER_IDS", "").split(",")
    if s.strip()
}
_KANDA_FILLER_IDS: frozenset[str] = frozenset(
    {"diva2:833794", "diva2:837011"} | _extra_filler
)

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
                        (default 2). Hämtas ur ThesisLevel-kolumnen.
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

    # Filtrera bort kända filler-poster
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

    # Examensarbetesnivå — avgör om grundnivå eller avancerad
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
    # en separat organisations-kolumn
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
        # Citationsfält
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
#
# Servern bygger helt på export.jsf i formatet csvall2. Ett giltigt svar är
# alltid CSV med en rubrikrad, även vid noll träffar. Allt annat — en HTML-sida,
# en omdirigering till en annan webbplats, ett tomt svar eller en CSV utan de
# kända rubrikerna — betyder att källan har ändrats. Det ska ge ett tydligt fel,
# aldrig tomma träffar som ser ut som ett riktigt sökresultat.

# Utan dessa kolumner går varken id eller titel att läsa ut ur en rad.
# Internt fältnamn → rubriken i DiVA:s CSV, för felmeddelandet.
_OBLIGATORISKA_KOLUMNER: dict[str, str] = {"diva_id": "PID", "titel": "Title"}


class DivaKallaFel(ToolError):
    """DiVA svarade, men inte med den CSV-export servern är byggd för."""


def _kontrollera_csv_svar(svar: httpx.Response, text: str) -> None:
    """Kastar DivaKallaFel om svaret från export.jsf inte ser ut som CSV."""
    innehallstyp = svar.headers.get("content-type", "").lower()
    borjan = text.lstrip()[:200].lower()
    ar_html = (
        "html" in innehallstyp
        or borjan.startswith("<")
        or "<html" in borjan
    )
    if ar_html:
        _logg.error(
            "DiVA svarade med HTML i stället för CSV (content-type %r, url %s)",
            innehallstyp, svar.url,
        )
        raise DivaKallaFel(
            "DiVA svarade med en webbsida i stället för CSV-export "
            f"(adress efter omdirigering: {svar.url}). Källan kan ha bytt "
            "plattform, eller export.jsf kan vara tillfälligt ur drift. Försök "
            "igen senare; kvarstår felet behöver servern anpassas till den nya "
            "källan."
        )
    if not text.strip():
        _logg.error("DiVA svarade med tom kropp (url %s)", svar.url)
        raise DivaKallaFel(
            "DiVA svarade med ett tomt svar utan kolumnrubriker. Export.jsf ger "
            "normalt en rubrikrad även när sökningen saknar träffar, så svaret "
            "tyder på ett tillfälligt fel eller att källan har bytt plattform."
        )


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
    except httpx.HTTPError as e:
        _logg.error("DiVA API-fel: %s", e)
        raise ToolError(
            f"DiVA svarade inte ({type(e).__name__}: {e}). Försök igen om en stund."
        ) from e

    # 404 och 410 betyder att adressen inte längre finns, inte att DiVA är
    # tillfälligt nere — det mest sannolika skälet är att export.jsf har
    # avvecklats.
    if svar.status_code in (404, 410):
        _logg.error("DiVA export.jsf gav HTTP %s (url %s)", svar.status_code, svar.url)
        raise DivaKallaFel(
            f"DiVA:s export.jsf svarade HTTP {svar.status_code} — adressen finns "
            "inte längre. Källan kan ha bytt plattform; servern behöver då "
            "anpassas till den nya källan."
        )
    if svar.is_error:
        _logg.error("DiVA export.jsf gav HTTP %s (url %s)", svar.status_code, svar.url)
        raise ToolError(
            f"DiVA svarade HTTP {svar.status_code}. Försök igen om en stund."
        )

    text = svar.content.decode("utf-8-sig", errors="replace")
    _kontrollera_csv_svar(svar, text)

    forsta_rad = text.split("\n")[0] if "\n" in text else text[:500]
    separator  = ";" if forsta_rad.count(";") > forsta_rad.count(",") else ","

    lasare   = csv.DictReader(io.StringIO(text), delimiter=separator)
    rubriker = list(lasare.fieldnames or [])
    rubrikindex = _bygg_rubrikindex(rubriker)

    saknade = [csv_namn for falt, csv_namn in _OBLIGATORISKA_KOLUMNER.items()
               if falt not in rubrikindex]
    if saknade:
        _logg.error(
            "DiVA-svaret saknar obligatoriska kolumner %s. Faktiska rubriker: %s",
            saknade, rubriker[:15],
        )
        raise DivaKallaFel(
            "DiVA:s CSV-export saknar de kolumnrubriker servern bygger på "
            f"(saknas: {', '.join(saknade)}). Källan kan ha bytt plattform eller "
            "exportformat; servern behöver då anpassas. Träffar kan inte "
            "redovisas förrän dess."
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
        raise ToolError(f"Ingen fulltextlänk tillgänglig för {diva_id}")

    # Unikt filnamn per anrop: två samtidiga anrop för samma post ska inte
    # skriva i och radera varandras fil.
    fd, sokvag = tempfile.mkstemp(
        prefix=f"{re.sub(r'[:/]', '_', diva_id)}-", suffix=".pdf", dir=_PDF_CACHE
    )
    os.close(fd)
    pdf_fil = Path(sokvag)

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
        raise ToolError(f"PDF-nedladdning misslyckades för {diva_id}: {e}") from e

    try:
        try:
            import pymupdf4llm
        except ImportError as imp_err:
            raise ToolError(
                "pymupdf4llm är inte installerat. Kör: pip install pymupdf4llm"
            ) from imp_err

        with _tysta_fd1():
            text = pymupdf4llm.to_markdown(str(pdf_fil))

        if not text or len(text.strip()) < 100:
            _logg.info("Tomt textlager för %s — provar OCR", diva_id)
            text = _ocr_pdf(pdf_fil)

    except ToolError:
        raise
    except Exception as e:
        _logg.error("Textextraktion misslyckades för %s: %s", diva_id, e)
        raise ToolError(f"Textextraktion misslyckades för {diva_id}: {e}") from e
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


# ── Svarstyper ────────────────────────────────────────────────────────────────
#
# Svaren valideras mot typerna nedan innan de skickas; ett fält med fel typ
# får hela anropet att misslyckas. Alla textfält i en post kommer ur
# _normalisera_rad, som ger tom sträng — aldrig None — när DiVA:s CSV har en
# tom cell eller saknar kolumnen. Bara `ar` kan vara None (tom eller
# icke-numerisk Year-cell), och bara den typas därför som valfri.


class EpistemiskStatus(TypedDict):
    """Källvärdering 1–7 utifrån publikationstyp, DOI och öppen fulltext."""

    pong: int
    typtext: str
    motivering: str
    display: str


# Funktionell syntax eftersom nyckeln `_sokterm` börjar med understreck.
# Den finns bara i sökträffar och anger vilken av söktermerna som gav träffen.
DivaPost = TypedDict(
    "DivaPost",
    {
        "diva_id": str,
        "titel": str,
        "forfattare": str,
        "ar": int | None,
        "publikationstyp": str,
        "sprak": str,
        "abstract": str,
        "nyckelord": str,
        "amne": str,
        "laerosate": str,
        "tidskrift": str,
        "issn": str,
        "doi": str,
        "urn": str,
        "isbn": str,
        "foerlag": str,
        "volym": str,
        "nummer": str,
        "sidor": str,
        "startpage": str,
        "slutpage": str,
        "hostpublication": str,
        "fulltext_url": str,
        "open_access": bool,
        "granskad": str,
        "handledare": str,
        "examinator": str,
        "disputationsdatum": str,
        "epistemisk_status": EpistemiskStatus,
        "_sokterm": NotRequired[str],
        # Bara i träfflistor, när abstractet har kortats; diva_hamta_post ger hela.
        "abstract_kapad": NotRequired[bool],
        "abstract_tecken_totalt": NotRequired[int],
    },
)


class Sokresultat(TypedDict):
    """Svar från diva_sok."""

    sokterm_original: str
    antal_termer_sokta: int
    termer_med_traffar: NotRequired[list[str]]
    antal_returnerade: int
    poster: list[DivaPost]
    meddelande: NotRequired[str]
    misslyckade_termer: NotRequired[list[str]]
    trunkerad: NotRequired[bool]
    utelamnade_poster: NotRequired[int]


class Fulltext(TypedDict):
    """Svar från diva_hamta_fulltext."""

    diva_id: str
    kalla: str
    fulltext_md: str
    tecken_totalt: int
    tecken_visade: int
    trunkerad: bool
    fortsatt_fran_tecken: int | None
    las_vidare: NotRequired[str]
    meddelande: NotRequired[str]


class Relaterade(TypedDict):
    """Svar från diva_relaterade."""

    ursprung_diva_id: str
    relationstyp: str
    beskrivning: str
    antal: int
    poster: list[DivaPost]
    meddelande: NotRequired[str]
    trunkerad: NotRequired[bool]
    utelamnade_poster: NotRequired[int]


# ── Storlek på träfflistor ────────────────────────────────────────────────────
#
# MCP-klienter avvisar svar över ungefär 1 MB, och ett typat svar skickas två
# gånger: som JSON-text och som structuredContent. En träfflista med 200 poster
# och fulla abstracts blir över 1,3 MB. Därför kortas abstracts i listor, och
# listan kapas när posterna tillsammans når ett bytetak som håller hela svaret
# under cirka 800 KB. diva_hamta_post ger alltid posten oförkortad.

_LISTA_ABSTRACT_MAX_TECKEN = 600
_LISTA_MAX_BYTE = 300_000


def _korta_abstract(post: dict) -> dict:
    """Kortar abstractet på ordgräns och markerar att det är kapat."""
    abstract = post.get("abstract", "")
    if len(abstract) <= _LISTA_ABSTRACT_MAX_TECKEN:
        return post
    utdrag = abstract[:_LISTA_ABSTRACT_MAX_TECKEN]
    brytpunkt = utdrag.rfind(" ")
    if brytpunkt > _LISTA_ABSTRACT_MAX_TECKEN * 0.6:
        utdrag = utdrag[:brytpunkt]
    post = dict(post)
    post["abstract"] = utdrag.rstrip() + " …"
    post["abstract_kapad"] = True
    post["abstract_tecken_totalt"] = len(abstract)
    return post


def _begransa_traffar(poster: list[dict]) -> tuple[list[dict], int]:
    """Kortar abstracts och kapar listan vid bytetaket.

    Returnerar posterna som ryms och antalet som utelämnades. Posterna hålls i
    inkommande ordning, så att de som utelämnas är de lägst rankade.
    """
    ryms: list[dict] = []
    storlek = 0
    for post in poster:
        kort = _korta_abstract(post)
        storlek += len(json.dumps(kort, ensure_ascii=False).encode("utf-8"))
        if ryms and storlek > _LISTA_MAX_BYTE:
            break
        ryms.append(kort)
    return ryms, len(poster) - len(ryms)


def _markera_utelamnade(svar: dict, utelamnade: int, rad: str) -> None:
    if utelamnade:
        svar["trunkerad"] = True
        svar["utelamnade_poster"] = utelamnade
        svar["meddelande"] = (
            f"Svaret är kapat: {utelamnade} poster till matchade men ryms inte "
            f"inom svarsgränsen. {rad}"
        )


# ── MCP-server ────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "diva",
    instructions=(
        "MCP-server för DiVA (Digitala Vetenskapliga Arkivet): avhandlingar, "
        "artiklar, rapporter och examensarbeten från svenska lärosäten och "
        "myndigheter. Verktygen har prefixet diva_. Kedjan är diva_sok → "
        "diva_hamta_post → diva_hamta_fulltext; diva_relaterade utgår från en "
        "känd post. Varje post bär epistemisk_status (1–7) som värderar "
        "källtypen. Abstracts i träfflistor är kortade (abstract_kapad) och "
        "långa listor kapas vid svarsgränsen (trunkerad); diva_hamta_post ger "
        "hela posten. Fulltexten kapas vid max_tecken; ett kapat svar bär "
        "trunkerad och fortsatt_fran_tecken, och ordagranna citat ska aldrig "
        "tas ur ett kapat utdrag."
    ),
    version=SERVER_VERSION,
    cache_hints=CACHE_HINTAR,
)


def _hamta_post(diva_id: str = "", urn: str = "", doi: str = "") -> DivaPost:
    """Slår upp en post på diva_id, URN:NBN eller DOI. Kastar ToolError om den saknas."""
    if diva_id:
        poster = _hamta_diva_export({
            "searchtype": "all",
            "aq":         json.dumps([[{"pid": diva_id}]]),
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
        raise ToolError(f"Ingen post hittades för: {diva_id or urn or doi}")
    return poster[0]


@mcp.tool(title="Sök i DiVA", annotations=LASNING_EXTERN)
def diva_sok(
    sokterm: Annotated[str, Field(description=(
        "Sökterm eller kommaseparerade termer. Varje kommasegment "
        "blir ett eget DiVA-anrop. Flerordstermer fungerar som "
        "frasmatchning (implicit AND): 'rule of law' söker efter "
        "poster som innehåller alla tre orden."
    ))],
    publikationstyp: Annotated[str, Field(description=(
        "Filtrera på publikationstyp. Möjliga värden: "
        "doctoralThesis, licentiateThesis, article, review, "
        "book, chapter, conferencePaper, report, studentThesis, other. "
        "Kommaseparera för flera typer."
    ))] = "",
    fran_ar: Annotated[int | None, Field(
        description="Publicerat från och med detta år. Kräver till_ar.",
    )] = None,
    till_ar: Annotated[int | None, Field(
        description="Publicerat till och med detta år. Kräver fran_ar.",
    )] = None,
    laerosate: Annotated[str, Field(description=(
        "Begränsa till ett lärosäte (klartext, t.ex. 'Uppsala universitet'). "
        "Kräver att lärosätets DiVA-ID finns i serverns lookup-tabell. "
        "Vid okänt lärosäte returneras ett felmeddelande med instruktion."
    ))] = "",
    open_access: Annotated[bool, Field(
        description="Om true: returnera bara poster med öppen fulltext.",
    )] = False,
    max_traffar: Annotated[int, Field(
        description="Antal träffar (1–250). Standard: 200.",
    )] = 200,
) -> Sokresultat:
    """Söker i DiVA (Digitala Vetenskapliga Arkivet) — ~1,5 miljoner vetenskapliga publikationer från ~50 svenska lärosäten och myndigheter. Täcker doktorsavhandlingar, licentiatavhandlingar, vetenskapliga artiklar, böcker, rapporter, konferensbidrag och examensarbeten. Varje träff innehåller metadata, abstract och epistemisk_status (poäng 1–7 för källans tillförlitlighet). Flerspråkig sökning: kommaseparerade termer ger separata parallella anrop till DiVA — ett anrop per term — som sedan mergas och dedupliceras. Flerordstermer bevaras som fraser (implicit AND). Exempel: 'sokterm': 'rättssäkerhet,rule of law,Rechtssicherheit'"""
    sokterm_ra = (sokterm or "").strip()
    if not sokterm_ra:
        raise ToolError("sokterm krävs")

    sokterm_expanderad = _expandera_sokterm(sokterm_ra)

    sett_termer: set[str] = set()
    termer: list[str] = []
    for t in (t.strip() for t in sokterm_expanderad.split(",")):
        if t and t.lower() not in sett_termer:
            sett_termer.add(t.lower())
            termer.append(t)

    publikationstyper = [p.strip() for p in (publikationstyp or "").split(",") if p.strip()]
    laerosate = (laerosate or "").strip()

    org_id: Optional[str] = None
    if laerosate:
        org_id = _LAEROSATE_ORG_ID.get(laerosate.lower())
        if not org_id:
            kanda = ", ".join(sorted(_LAEROSATE_ORG_ID))
            raise ToolError(
                "Organisationsfiltrering kräver ett DiVA-organisations-ID. "
                f"Ingen mappning hittades för '{laerosate}'. "
                f"Lärosäten med känd mappning: {kanda}. "
                "Sök utan laerosate, eller lägg till lärosätet i serverns tabell: "
                "sök på organisationens namn i DiVA-portalen och läs "
                "URL-parametern 'organisationId' i sökfrågan."
            )

    # Typfiltreringen sker här i servern efter hämtning, så fler rader hämtas
    # för att filtret inte ska tömma resultatet.
    hamta_rader = 300 if publikationstyper else 200

    def _bygg_params(term: str) -> dict:
        """DiVA-sökparametrar för en term: fritext, årsintervall och lärosäte i en AND-grupp."""
        p: dict[str, Any] = {"searchtype": "all", "rows": hamta_rader}
        and_villkor: list[dict] = [{"freeText": _formatera_enkel_sokterm(term)}]
        if fran_ar and till_ar:
            and_villkor.append(
                {"dateIssued": {"from": str(int(fran_ar)), "to": str(int(till_ar))}}
            )
        if org_id:
            and_villkor.append({"organisationId": org_id, "organisationId-Xtra": False})
        p["aq"]  = json.dumps([and_villkor])
        p["aqe"] = "[]"
        p["af"]  = "[]"
        p["aq2"] = "[[]]"
        if open_access:
            p["onlyFullText"] = "true"
        return p

    _logg.info(
        "diva_sok: %d termer parallellt: %s",
        len(termer), ", ".join(f'"{t}"' for t in termer),
    )
    # En tråd per term, högst fyra samtidigt, så att en lång termlista från
    # begreppsexpansionen inte blir en störtflod av anrop mot DiVA.
    with ThreadPoolExecutor(max_workers=min(4, len(termer))) as pool:
        framtider = [(t, pool.submit(_hamta_diva_export, _bygg_params(t))) for t in termer]
        resultat: list[tuple[str, list[dict] | Exception]] = []
        for term, framtid in framtider:
            try:
                resultat.append((term, framtid.result()))
            except Exception as e:  # noqa: BLE001 – redovisas per term nedan
                resultat.append((term, e))

    # Misslyckas varje term är det källan som felar, inte sökningen som saknar
    # träffar. Ett tomt resultat skulle då dölja felet.
    misslyckade = [(t, r) for t, r in resultat if isinstance(r, Exception)]
    if misslyckade and len(misslyckade) == len(resultat):
        raise ToolError(str(misslyckade[0][1]))

    sett_ids: set[str] = set()
    alla_poster: list[dict] = []
    for term, poster in resultat:
        if isinstance(poster, Exception):
            _logg.warning("Sökanrop misslyckades för %r: %s", term, poster)
            continue
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

    alla_poster.sort(
        key=lambda p: (p["epistemisk_status"]["pong"], p.get("ar") or 0),
        reverse=True,
    )
    alla_poster, utelamnade = _begransa_traffar(alla_poster[:max_traffar])

    svar: Sokresultat = {
        "sokterm_original":   sokterm_ra,
        "antal_termer_sokta": len(termer),
        # antal_returnerade är antalet poster i svaret, inte DiVA:s totalantal
        # för sökningen — export.jsf redovisar inget totalantal.
        "antal_returnerade":  len(alla_poster),
        "poster":             alla_poster,  # type: ignore[typeddict-item]
    }
    if alla_poster:
        svar["termer_med_traffar"] = sorted({p["_sokterm"] for p in alla_poster})
    else:
        svar["meddelande"] = (
            "Inga träffar hittades i DiVA. "
            "Söktes som separata anrop per term: "
            + ", ".join(f'"{t}"' for t in termer)
        )
    if misslyckade:
        svar["misslyckade_termer"] = [f"{t}: {fel}" for t, fel in misslyckade]
    _markera_utelamnade(
        svar, utelamnade,
        "Snäva in sökningen med publikationstyp, fran_ar/till_ar eller "
        "open_access, eller sänk max_traffar.",
    )
    return svar


@mcp.tool(title="Hämta DiVA-post", annotations=LASNING_EXTERN)
def diva_hamta_post(
    diva_id: Annotated[str, Field(description="DiVA-id, t.ex. 'diva2:123456'.")] = "",
    urn: Annotated[str, Field(description="URN:NBN, t.ex. 'urn:nbn:se:uu:diva-12345'.")] = "",
    doi: Annotated[str, Field(description="DOI, t.ex. '10.1234/example'.")] = "",
) -> DivaPost:
    """Hämtar fullständig metadata för en enskild DiVA-post. Ange diva_id (t.ex. 'diva2:123456'), urn (URN:NBN) eller doi. Minst ett av dessa fält krävs."""
    diva_id = (diva_id or "").strip()
    urn     = (urn or "").strip()
    doi     = (doi or "").strip()
    if not any([diva_id, urn, doi]):
        raise ToolError("Ange minst ett av: diva_id, urn, doi")
    return _hamta_post(diva_id=diva_id, urn=urn, doi=doi)


def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.

    fortsatt_fran_tecken är utdragets faktiska slut, inte fran_tecken +
    max_tecken: kapningen på ordgräns gör utdraget kortare än max_tecken, och
    på varandra följande utdrag ska tillsammans bli exakt hela texten.
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
        # Ett utdrag av bara blanktecken skulle ge slut == start, och
        # läs-vidare-positionen skulle då peka på samma ställe igen.
        utdrag    = utdrag.rstrip() or rest[:max_tecken]
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


def _fulltext_svar(diva_id: str, kalla: str, text: str, max_tecken: int, fran_tecken: int) -> Fulltext:
    begransad = max_tecken <= 0 or max_tecken > DIVA_MAX_TECKEN_TAK
    effektiv  = DIVA_MAX_TECKEN_TAK if begransad else max_tecken
    utdrag = _skar_ut(text, effektiv, fran_tecken)
    svar: Fulltext = {
        "diva_id":              diva_id,
        "kalla":                kalla,
        "fulltext_md":          utdrag["text"],
        "tecken_totalt":        utdrag["tecken_totalt"],
        "tecken_visade":        utdrag["tecken_visade"],
        "trunkerad":            utdrag["trunkerad"],
        "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
    }
    if utdrag["fortsatt_fran_tecken"] is not None:
        svar["las_vidare"] = (
            f'diva_hamta_fulltext(diva_id="{diva_id}", max_tecken={max_tecken}, '
            f'fran_tecken={utdrag["fortsatt_fran_tecken"]})'
        )
    if begransad and utdrag["trunkerad"]:
        svar["meddelande"] = (
            f"Ett svar rymmer högst {DIVA_MAX_TECKEN_TAK} tecken fulltext. "
            "Läs resten i delar med las_vidare; utdragen blir tillsammans exakt "
            "hela texten."
        )
    return svar


@mcp.tool(title="Hämta fulltext från DiVA", annotations=LASNING_EXTERN)
def diva_hamta_fulltext(
    diva_id: Annotated[str, Field(description="DiVA-id, t.ex. 'diva2:123456'.")],
    max_tecken: Annotated[int, Field(description=(
        "Teckentak för fulltexten (standard 60 000, högst 300 000; 0 = "
        "största tillåtna utdrag). Avhandlingar kan vara över en miljon "
        "tecken; läs då vidare i delar med fran_tecken."
    ))] = DIVA_MAX_TECKEN,
    fran_tecken: Annotated[int, Field(
        description="Börja texten vid denna teckenposition — för att läsa vidare.",
    )] = 0,
) -> Fulltext:
    """Hämtar och cachar fulltext-PDF on-demand för en DiVA-post. Returnerar extraherad text som markdown. Kräver att posten har öppen fulltext (open_access=true i diva_sok-svaret). Återanvänder cache vid upprepade anrop."""
    diva_id = (diva_id or "").strip()
    if not diva_id:
        raise ToolError("diva_id krävs")
    max_tecken  = int(max_tecken or 0)
    fran_tecken = int(fran_tecken or 0)

    try:
        cachad = db.hamta_cachad_fulltext(diva_id)
    except Exception as e:
        _logg.error("Fulltextcachen kunde inte läsas: %s", e)
        raise ToolError(
            f"Fulltextcachen i databasen kunde inte läsas ({type(e).__name__}). "
            "Kontrollera att databasen i DATABASE_URL är igång och nåbar."
        ) from e
    if cachad:
        return _fulltext_svar(diva_id, "cache", cachad, max_tecken, fran_tecken)

    post = _hamta_post(diva_id=diva_id)
    fulltext_url = post.get("fulltext_url", "")
    urn          = post.get("urn", "")

    if not fulltext_url and not urn:
        raise ToolError(
            f"Ingen öppen fulltext tillgänglig för {diva_id} "
            f"(open_access={post.get('open_access', False)}). "
            "Kontrollera om posten har open_access=true i diva_sok-svaret."
        )

    fulltext_md = _hamta_pdf_fulltext(diva_id, fulltext_url, urn)
    if not fulltext_md:
        raise ToolError(f"Kunde inte extrahera text från {diva_id}")

    try:
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
    except Exception as e:
        # Texten är redan extraherad; att den inte kunde cachas ska inte kosta
        # anroparen svaret. Nästa anrop hämtar PDF:en på nytt.
        _logg.error("Fulltext för %s kunde inte sparas i cachen: %s", diva_id, e)

    # Databasen har alltid hela texten — trunkeringen gäller bara svaret.
    return _fulltext_svar(diva_id, "diva", fulltext_md, max_tecken, fran_tecken)


@mcp.tool(title="Relaterade DiVA-poster", annotations=LASNING_EXTERN)
def diva_relaterade(
    diva_id: Annotated[str, Field(description="DiVA-id för utgångspunkten.")],
    relationstyp: Annotated[Literal["forfattare", "amne", "organisation"], Field(description=(
        "forfattare: andra verk av samma författare. "
        "amne: publikationer inom samma ämnesområde (standard). "
        "organisation: publikationer från samma lärosäte "
        "(kräver att lärosätets DiVA-ID finns i serverns lookup-tabell)."
    ))] = "amne",
    max_traffar: Annotated[int, Field(
        description="Max antal relaterade poster (1–50). Standard: 10.",
    )] = 10,
) -> Relaterade:
    """Hittar publikationer relaterade till en given DiVA-post. Söker baserat på samma författare, samma ämnesområde eller samma organisation."""
    diva_id = (diva_id or "").strip()
    if not diva_id:
        raise ToolError("diva_id krävs")
    max_traffar = min(int(max_traffar), 50)

    post = _hamta_post(diva_id=diva_id)

    diva_params: dict[str, Any] = {
        "searchtype": "all",
        "noOfRows":   max_traffar + 1,
        "sort":       "year desc",
    }

    if relationstyp == "forfattare":
        forfattare_rad = post.get("forfattare", "")
        if not forfattare_rad:
            raise ToolError("Ingen författarinformation tillgänglig för denna post")
        forste_full = forfattare_rad.split(";")[0].strip()
        forste      = re.sub(r'\s*[\[\(].*', '', forste_full).strip()
        if not forste:
            raise ToolError("Kunde inte tolka författarnamn")
        diva_params["aq"] = json.dumps([[{"name": forste}]])
        beskrivning = f"Andra verk av {forste}"

    elif relationstyp == "amne":
        kandidat     = post.get("amne") or post.get("nyckelord") or post.get("titel", "")
        sokterm_amne = kandidat.split(";")[0].split(",")[0].strip()[:80]
        if not sokterm_amne:
            raise ToolError("Ingen ämneskategori eller nyckelord tillgängliga för denna post")
        diva_params["freetext"] = sokterm_amne
        beskrivning = f"Publikationer inom ämnet: {sokterm_amne}"

    elif relationstyp == "organisation":
        laerosate_post = post.get("laerosate", "")
        if not laerosate_post:
            raise ToolError("Ingen organisationsinformation tillgänglig för denna post")
        org_id = _LAEROSATE_ORG_ID.get(laerosate_post.lower())
        if not org_id:
            raise ToolError(
                "Organisationsfiltrering kräver DiVA-organisations-ID. "
                f"Ingen mappning för '{laerosate_post}'. Lärosäten med känd "
                f"mappning: {', '.join(sorted(_LAEROSATE_ORG_ID))}. "
                "Prova relationstyp 'amne' eller 'forfattare' i stället."
            )
        diva_params["aq"] = json.dumps(
            [[{"organisationId": org_id, "organisationId-Xtra": False}]]
        )
        diva_params["searchtype"] = "postgraduate"
        beskrivning = f"Publikationer från {laerosate_post}"

    else:
        raise ToolError(
            f"Okänd relationstyp: {relationstyp}. "
            "Tillåtna: forfattare, amne, organisation"
        )

    poster = _hamta_diva_export(diva_params)
    poster = [p for p in poster if p.get("diva_id") != diva_id][:max_traffar]
    poster, utelamnade = _begransa_traffar(poster)

    svar: Relaterade = {
        "ursprung_diva_id": diva_id,
        "relationstyp":     relationstyp,
        "beskrivning":      beskrivning,
        "antal":            len(poster),
        "poster":           poster,  # type: ignore[typeddict-item]
    }
    _markera_utelamnade(svar, utelamnade, "Sänk max_traffar.")
    return svar

# ── Serverstart ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _logg.info("DiVA MCP-server startar (transport=%s)", os.getenv("MCP_TRANSPORT", "stdio"))
    starta(mcp, standardport=8015, initiera=db.initiera_schema)
