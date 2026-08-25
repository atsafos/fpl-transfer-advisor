"""
FPL Transfer Advisor
---------------------
A professional FPL transfer decision dashboard. It combines official Fantasy
Premier League data with fixture context, position-specific opponent strength,
set-piece roles, expected goal involvement, minutes security, ownership momentum
and optional Understat xG/xA to explain which transfers genuinely improve a squad.

Data sources:
- https://fantasy.premierleague.com/api/   (official FPL data - squad, form, fixtures, prices)
- https://understat.com/league/EPL/{season} (season-aggregate xG/xA per player - public page,
  no login/paywall; parsed the same way the open-source `understatapi` / `understat` community
  packages do: a JSON blob embedded in the page's own <script> tag)
"""

import json
import re
import unicodedata
import html
import os
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from itertools import combinations

import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = "https://fantasy.premierleague.com/api"
BUILD_LABEL = "UI V3 — GW1 Builder"
CACHE_TTL = 1800          # 30 minutes for FPL data - keeps within FPL's rate limits
UNDERSTAT_TTL = 6 * 3600  # 6 hours - season xG totals don't need refreshing often
CORE_CONNECT_TIMEOUT = 3.05
CORE_READ_TIMEOUT = 7
SUPPLEMENTARY_TIMEOUT = 8

POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_RULES = {1: 2, 2: 5, 3: 5, 4: 3}  # required numbers per position
MAX_PER_TEAM = 3
MIN_CANDIDATES = 3  # always try to show at least this many replacement options

# Scoring presets. These weights are adjusted again by the user's risk appetite.
STRATEGY_WEIGHTS = {
    "Balanced": {
        "form": 0.12, "ep": 0.20, "fixtures": 0.15, "availability": 0.10,
        "underlying": 0.15, "momentum": 0.06, "minutes": 0.12, "value": 0.10,
    },
    "Short-term (next 3 GWs)": {
        "form": 0.12, "ep": 0.24, "fixtures": 0.22, "availability": 0.10,
        "underlying": 0.12, "momentum": 0.05, "minutes": 0.10, "value": 0.05,
    },
    "Long-term hold": {
        "form": 0.10, "ep": 0.16, "fixtures": 0.14, "availability": 0.10,
        "underlying": 0.16, "momentum": 0.04, "minutes": 0.16, "value": 0.14,
    },
    "Differential": {
        "form": 0.10, "ep": 0.17, "fixtures": 0.14, "availability": 0.10,
        "underlying": 0.18, "momentum": 0.06, "minutes": 0.10, "value": 0.08,
    },
    "Protect rank": {
        "form": 0.13, "ep": 0.20, "fixtures": 0.14, "availability": 0.12,
        "underlying": 0.10, "momentum": 0.08, "minutes": 0.15, "value": 0.08,
    },
}

FDR_COLORS = {1: "#01FC7A", 2: "#6DFFA6", 3: "#E7E7E7", 4: "#FF8F8F", 5: "#FF1D1D"}

STATUS_LABELS = {
    "a": "Available",
    "d": "Doubtful",
    "i": "Injured",
    "s": "Suspended",
    "u": "Unavailable",
    "n": "Not in squad",
}

# Understat uses full club names; FPL uses short codes. Map short -> Understat name.
UNDERSTAT_TEAM_NAME = {
    "ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
    "BHA": "Brighton", "BUR": "Burnley", "CHE": "Chelsea", "CRY": "Crystal Palace",
    "EVE": "Everton", "FUL": "Fulham", "IPS": "Ipswich", "LEI": "Leicester",
    "LIV": "Liverpool", "MCI": "Manchester City", "MUN": "Manchester United",
    "NEW": "Newcastle United", "NFO": "Nottingham Forest", "SOU": "Southampton",
    "TOT": "Tottenham", "WHU": "West Ham", "WOL": "Wolverhampton Wanderers",
    "LEE": "Leeds", "SUN": "Sunderland",
}

st.set_page_config(page_title="FPL Transfer Advisor", page_icon="⚽", layout="wide", initial_sidebar_state="expanded")

# FPL-inspired visual system: deep plum, electric green and cyan accents, without
# reproducing Premier League logos or proprietary artwork.
st.markdown("""
<style>
    :root {
        --fpl-plum: #37003c;
        --fpl-plum-2: #24102a;
        --fpl-green: #00ff87;
        --fpl-cyan: #04f5ff;
        --fpl-bg: #f5f6f8;
        --fpl-ink: #19151c;
        --fpl-muted: #6b6570;
        --fpl-card: #ffffff;
    }
    .stApp { background: var(--fpl-bg); }
    .block-container { max-width: 1380px; padding-top: 1.25rem; padding-bottom: 3rem; }
    section[data-testid="stSidebar"] { background: linear-gradient(180deg, #37003c 0%, #24102a 100%); }
    section[data-testid="stSidebar"] * { color: #ffffff; }
    section[data-testid="stSidebar"] label p { color: #ffffff !important; }
    section[data-testid="stSidebar"] [data-baseweb="select"] * { color: #19151c; }
    section[data-testid="stSidebar"] input { color: #19151c !important; }
    .fpl-hero {
        background: linear-gradient(115deg, #37003c 0%, #5a1464 55%, #103947 100%);
        color: white; border-radius: 20px; padding: 26px 30px; margin-bottom: 18px;
        box-shadow: 0 14px 34px rgba(55,0,60,.18); position: relative; overflow: hidden;
    }
    .fpl-hero:after {
        content: ""; position: absolute; width: 220px; height: 220px; right: -80px; top: -110px;
        border: 38px solid rgba(0,255,135,.18); border-radius: 50%;
    }
    .fpl-eyebrow { text-transform: uppercase; letter-spacing: .12em; font-size: .72rem; font-weight: 800; color: #00ff87; }
    .fpl-hero h1 { color: white; margin: .15rem 0 .2rem; font-size: 2.15rem; }
    .fpl-hero p { color: rgba(255,255,255,.82); margin: 0; }
    .metric-card { background: white; border-radius: 16px; padding: 16px 18px; border: 1px solid #e8e3ea; min-height: 104px; box-shadow: 0 5px 16px rgba(33,24,36,.05); }
    .metric-label { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color: #746d78; font-weight: 800; }
    .metric-value { font-size: 1.55rem; color: #37003c; font-weight: 850; margin-top: 6px; }
    .metric-sub { color: #827b86; font-size: .78rem; margin-top: 4px; }
    .transfer-card { background: white; border: 1px solid #e5dfe7; border-radius: 17px; padding: 17px; min-height: 230px; box-shadow: 0 7px 20px rgba(33,24,36,.06); }
    .transfer-card.best { border-top: 5px solid #00ff87; }
    .transfer-card h3 { color: #37003c; margin: 4px 0 8px; }
    .pill { display:inline-block; padding:4px 8px; border-radius:999px; background:#f0ebf2; color:#37003c; font-size:.72rem; font-weight:750; margin:2px 4px 2px 0; }
    .pill-green { background:#dcfff0; color:#006b3a; }
    .pill-cyan { background:#ddfbff; color:#006672; }
    .pill-warn { background:#fff0d6; color:#8a4d00; }
    .fixture-row { display:flex; gap:5px; flex-wrap:wrap; margin:8px 0; }
    .fixture-chip { min-width:48px; text-align:center; padding:5px 7px; border-radius:7px; font-size:.72rem; font-weight:850; color:#1c1720; }
    .section-kicker { color:#37003c; font-size:.76rem; font-weight:850; text-transform:uppercase; letter-spacing:.09em; margin-bottom:2px; }
    div[data-testid="stTabs"] button { font-weight: 750; }
    div[data-testid="stDataFrame"] { border-radius: 14px; overflow: hidden; }
    .confidence-high { color:#007a45; font-weight:850; }
    .confidence-medium { color:#9a5a00; font-weight:850; }
    .confidence-low { color:#9b2637; font-weight:850; }

    section[data-testid="stSidebar"] { border-right: 0; box-shadow: 12px 0 32px rgba(33,0,38,.12); }
    section[data-testid="stSidebar"] > div { padding-top: 1rem; }
    .rail-brand { padding: 8px 6px 14px; }
    .rail-brand .mark { width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#00ff87,#04f5ff);display:flex;align-items:center;justify-content:center;color:#37003c;font-weight:950;font-size:1.1rem;margin-bottom:10px; }
    .rail-brand h2 { margin:0;color:white;font-size:1.25rem;letter-spacing:-.02em; }
    .rail-brand p { margin:.25rem 0 0;color:rgba(255,255,255,.62);font-size:.77rem; }
    .rail-team { background:rgba(255,255,255,.085); border:1px solid rgba(255,255,255,.12); border-radius:15px; padding:12px 13px; margin:6px 0 14px; }
    .rail-team .name { font-weight:850;font-size:.94rem;color:white; }
    .rail-team .sub { font-size:.72rem;color:rgba(255,255,255,.62);margin-top:3px; }
    section[data-testid="stSidebar"] div[role="radiogroup"] { gap:6px; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label { background:transparent;border-radius:11px;padding:8px 10px;margin:0;transition:.15s ease; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background:rgba(255,255,255,.08); }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) { background:linear-gradient(90deg,rgba(0,255,135,.20),rgba(4,245,255,.10)); box-shadow:inset 3px 0 0 #00ff87; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label p { font-weight:750!important;font-size:.88rem!important; }
    section[data-testid="stSidebar"] div[data-testid="stExpander"] { background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.10);border-radius:13px; }
    section[data-testid="stSidebar"] button[kind="primary"] { background:linear-gradient(90deg,#00ff87,#04f5ff)!important;color:#27002b!important;border:0!important;font-weight:900!important; }
    .decision-banner { border-radius:20px;padding:22px 24px;background:white;border:1px solid #e7e0e8;box-shadow:0 10px 28px rgba(33,24,36,.07);margin:6px 0 18px; }
    .decision-banner .tag { display:inline-block;border-radius:999px;padding:5px 10px;font-size:.72rem;font-weight:900;letter-spacing:.08em;background:#dcfff0;color:#006b3a; }
    .decision-banner h2 { margin:10px 0 5px;color:#37003c;font-size:1.65rem; }
    .decision-banner p { margin:0;color:#716977; }
    .signal-card { background:white;border:1px solid #ebe5ed;border-radius:16px;padding:15px 16px;min-height:130px;box-shadow:0 6px 18px rgba(33,24,36,.045); }
    .signal-card .icon { font-size:1.2rem;margin-bottom:8px; }
    .signal-card .title { font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:#766e7a;font-weight:850; }
    .signal-card .big { color:#37003c;font-size:1.08rem;font-weight:900;margin-top:5px; }
    .signal-card .small { color:#837a86;font-size:.75rem;margin-top:4px; }
    .proj-badge { display:inline-block;padding:5px 9px;border-radius:9px;background:#eefcfd;color:#006672;font-weight:850;font-size:.76rem;margin-right:5px; }
    #MainMenu, footer { visibility:hidden; }

    /* Product-shell polish */
    header[data-testid="stHeader"], div[data-testid="stToolbar"], div[data-testid="stDecoration"] {
        height: 0 !important; min-height: 0 !important; visibility: hidden !important; display: none !important;
    }
    button[data-testid="baseButton-headerNoPadding"] { display:none !important; }
    .block-container { max-width: 1240px; padding: 2.0rem 2.2rem 4rem; }
    section[data-testid="stSidebar"] {
        width: 304px !important; min-width: 304px !important;
        background: radial-gradient(circle at 20% 0%, rgba(4,245,255,.10), transparent 28%), linear-gradient(180deg,#210024 0%,#300033 46%,#18001b 100%) !important;
        box-shadow: 14px 0 38px rgba(22,0,27,.16) !important;
    }
    section[data-testid="stSidebar"] > div:first-child { padding: 1.25rem 1.05rem 1.2rem !important; }
    .rail-brand { padding: 2px 2px 17px; display:flex; align-items:center; gap:11px; border-bottom:1px solid rgba(255,255,255,.08); margin-bottom:14px; }
    .rail-brand .mark { width:38px;height:38px;border-radius:12px;margin:0;background:linear-gradient(135deg,#00ff87,#04f5ff);box-shadow:0 7px 18px rgba(0,255,135,.18);font-size:.78rem;letter-spacing:-.03em; }
    .rail-brand .copy { min-width:0; }
    .rail-brand h2 { font-size:1.02rem;line-height:1.15; }
    .rail-brand p { font-size:.68rem;line-height:1.35;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }
    .rail-team { background:linear-gradient(145deg,rgba(255,255,255,.105),rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:13px 14px;margin:0 0 13px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04); }
    .rail-team .topline { display:flex;align-items:center;justify-content:space-between;gap:8px; }
    .rail-team .name { font-size:.93rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }
    .rail-team .gw { background:rgba(0,255,135,.16);color:#83ffc1;font-size:.62rem;font-weight:900;padding:4px 7px;border-radius:999px;white-space:nowrap; }
    .rail-team .sub { color:rgba(255,255,255,.60);font-size:.68rem;margin-top:6px; }
    .rail-section-label { color:rgba(255,255,255,.40);text-transform:uppercase;letter-spacing:.11em;font-size:.60rem;font-weight:900;margin:15px 6px 6px; }
    section[data-testid="stSidebar"] div[role="radiogroup"] { gap:3px !important; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label { min-height:38px;padding:7px 10px !important;border-radius:10px !important;position:relative; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) { background:rgba(255,255,255,.10) !important; box-shadow:none !important; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked):before { content:"";width:4px;height:20px;border-radius:6px;background:#00ff87;position:absolute;left:0;top:9px; }
    section[data-testid="stSidebar"] div[role="radiogroup"] label p { font-size:.80rem!important;font-weight:720!important; }
    section[data-testid="stSidebar"] div[data-testid="stExpander"] { background:rgba(255,255,255,.045)!important;border:1px solid rgba(255,255,255,.08)!important;border-radius:12px!important;box-shadow:none!important;margin-top:7px; }
    section[data-testid="stSidebar"] div[data-testid="stExpander"] details summary { font-size:.76rem!important;font-weight:760!important; }
    section[data-testid="stSidebar"] .stButton > button { border-radius:10px !important;min-height:38px;font-size:.78rem;font-weight:800; }
    section[data-testid="stSidebar"] button[kind="primary"] { background:linear-gradient(90deg,#00ff87,#04f5ff)!important;color:#210024!important;box-shadow:0 8px 20px rgba(0,255,135,.12)!important; }
    section[data-testid="stSidebar"] input, section[data-testid="stSidebar"] [data-baseweb="select"] > div { border-radius:10px !important; }
    .rail-foot { margin-top:16px;padding:11px 6px 0;border-top:1px solid rgba(255,255,255,.07);color:rgba(255,255,255,.40);font-size:.63rem;line-height:1.45; }

    .empty-shell { max-width:760px;margin:7vh auto 0;text-align:center; }
    .empty-orb { width:74px;height:74px;margin:0 auto 20px;border-radius:24px;background:linear-gradient(135deg,#37003c,#6d1778);display:flex;align-items:center;justify-content:center;font-size:2rem;box-shadow:0 18px 40px rgba(55,0,60,.18); }
    .empty-shell h1 { color:#2a1730;font-size:2rem;margin:0 0 8px;letter-spacing:-.035em; }
    .empty-shell p { color:#77707b;font-size:.96rem;line-height:1.6;max-width:620px;margin:0 auto; }
    .empty-meta { display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:19px; }
    .empty-chip { background:white;border:1px solid #e8e2ea;border-radius:999px;padding:7px 11px;color:#544c58;font-size:.72rem;font-weight:760;box-shadow:0 4px 12px rgba(31,20,34,.035); }
    .empty-note { margin:22px auto 0;background:white;border:1px solid #e9e3eb;border-radius:16px;padding:15px 18px;text-align:left;max-width:600px;color:#655e69;font-size:.80rem;line-height:1.55;box-shadow:0 8px 22px rgba(31,20,34,.045); }

    @media (max-width: 900px) {
        .block-container { padding:1.25rem 1rem 3rem; }
        section[data-testid="stSidebar"] { width:285px!important;min-width:285px!important; }
        .fpl-hero h1 { font-size:1.7rem; }
    }
</style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# DATA FETCHING (cached so we don't hammer the FPL API)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=CACHE_TTL)
def get_bootstrap():
    r = requests.get(f"{BASE}/bootstrap-static/", timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_fixtures():
    r = requests.get(f"{BASE}/fixtures/", timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_entry(team_id):
    r = requests.get(f"{BASE}/entry/{team_id}/", timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_picks(team_id, event):
    r = requests.get(f"{BASE}/entry/{team_id}/event/{event}/picks/", timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_history(team_id):
    r = requests.get(f"{BASE}/entry/{team_id}/history/", timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json()


def _raw_json_get(url):
    """Small request primitive used by the parallel startup loader."""
    started = time.perf_counter()
    r = requests.get(url, timeout=(CORE_CONNECT_TIMEOUT, CORE_READ_TIMEOUT))
    r.raise_for_status()
    return r.json(), round(time.perf_counter() - started, 3)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_core_fpl_data(team_id):
    """Fetch independent FPL startup endpoints concurrently.

    A slow endpoint can no longer serially delay the other three. The result also
    carries per-endpoint timings which are printed into the Streamlit deployment log.
    """
    endpoints = {
        "bootstrap": f"{BASE}/bootstrap-static/",
        "fixtures": f"{BASE}/fixtures/",
        "entry": f"{BASE}/entry/{team_id}/",
        "history": f"{BASE}/entry/{team_id}/history/",
    }
    payload, timings = {}, {}
    started_all = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_raw_json_get, url): name for name, url in endpoints.items()}
        for future in as_completed(futures):
            name = futures[future]
            data, elapsed = future.result()
            payload[name] = data
            timings[name] = elapsed
    timings["total"] = round(time.perf_counter() - started_all, 3)
    return payload, timings


@st.cache_data(ttl=UNDERSTAT_TTL)
def get_understat_players(season_start_year):
    """Season-aggregate xG/xA per player, parsed from Understat's own league page.
    Understat has no formal public API; this mirrors the well-established open-source
    approach (e.g. the `understat` / `understatapi` PyPI packages) of reading the JSON
    that the page embeds for its own charts. Public page, no login, no paywall.
    Returns {} gracefully if the page shape changes or the site is unreachable -
    Understat data is a bonus layer, never a hard dependency."""
    try:
        url = f"https://understat.com/league/EPL/{season_start_year}"
        headers = {"User-Agent": "Mozilla/5.0 (FPL-Transfer-Advisor personal tool)"}
        r = requests.get(url, headers=headers, timeout=(CORE_CONNECT_TIMEOUT, SUPPLEMENTARY_TIMEOUT))
        r.raise_for_status()
        match = re.search(r"playersData\s*=\s*JSON\.parse\('(.+?)'\)", r.text)
        if not match:
            return {}
        raw = match.group(1).encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
        players = json.loads(raw)
        return {p["player_name"]: p for p in players}
    except Exception:
        return {}


_MANUAL_FOLDS = {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH", "ß": "ss"}


def normalize_name(name):
    """Strip accents/punctuation and lowercase, for fuzzy matching FPL <-> Understat names.
    Handles Scandinavian/Icelandic letters (ø, æ, ð...) that NFKD decomposition alone drops
    rather than transliterates."""
    for src, dst in _MANUAL_FOLDS.items():
        name = name.replace(src, dst)
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", ascii_name.lower()).strip()


def build_understat_lookup(understat_players):
    """Index Understat players by normalized full name AND normalized surname,
    so we can match against FPL's `first_name second_name` fields."""
    by_full = {}
    by_last = {}
    for name, data in understat_players.items():
        norm = normalize_name(name)
        by_full[norm] = data
        last = norm.split(" ")[-1] if norm else norm
        by_last.setdefault(last, []).append(data)
    return by_full, by_last


def match_understat(fpl_player, by_full, by_last):
    full_norm = normalize_name(f"{fpl_player['first_name']} {fpl_player['second_name']}")
    if full_norm in by_full:
        return by_full[full_norm]
    last_norm = normalize_name(fpl_player["second_name"]).split(" ")[-1]
    candidates = by_last.get(last_norm, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        team_name = UNDERSTAT_TEAM_NAME.get(
            st.session_state["teams_by_id"].get(fpl_player["team"], {}).get("short_name"), None
        )
        for c in candidates:
            if c.get("team_title") == team_name:
                return c
    return None


def ownership_momentum(p, total_players):
    """A cheap proxy for the 'template/differential' chatter you'd get from content
    creators, built entirely from official FPL data: net transfer activity this GW
    relative to the size of the player pool."""
    net = int(p["transfers_in_event"]) - int(p["transfers_out_event"])
    pct_of_pool = (net / total_players * 100) if total_players else 0
    if pct_of_pool > 0.5:
        label = f"📈 Trending in ({net:+,})"
    elif pct_of_pool < -0.5:
        label = f"📉 Trending out ({net:+,})"
    else:
        label = "➖ Stable"
    momentum_score = max(-5, min(5, pct_of_pool))
    return momentum_score, label


def free_transfers_available(history, events):
    """Simulate FPL's free-transfer banking (max 5, +1 per GW). Banked transfers
    are preserved through Wildcard/Free Hit weeks under the current rules.
    This is the same logic FPL applies; approximate only if data is sparse (e.g.
    very start of season)."""
    finished_ids = {e["id"] for e in events if e.get("finished")}
    chips_by_event = {c["event"]: c["name"] for c in history.get("chips", [])}
    rows = sorted(
        [row for row in history.get("current", []) if row["event"] in finished_ids],
        key=lambda r: r["event"],
    )
    ft = 1
    for row in rows:
        ev = row["event"]
        if ev == 1:
            continue
        if chips_by_event.get(ev) in ("wildcard", "freehit"):
            continue  # unlimited transfers that week; banked FT untouched
        transfers_made = row.get("event_transfers", 0)
        cost = row.get("event_transfers_cost", 0)
        paid = cost // 4
        free_used = min(max(0, transfers_made - paid), ft)
        ft = min(5, (ft - free_used) + 1)
    return max(1, ft)


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def current_or_last_event(events):
    """Return the gameweek we should read the squad from: the current one if
    the season is mid-gameweek, otherwise the most recently finished one."""
    current = [e for e in events if e.get("is_current")]
    if current:
        return current[0]
    finished = [e for e in events if e.get("finished")]
    if finished:
        return finished[-1]
    return events[0]


def next_event(events):
    nxt = [e for e in events if e.get("is_next")]
    if nxt:
        return nxt[0]
    return current_or_last_event(events)


def team_fixture_difficulty(team_id, fixtures, from_event, n=5):
    """Average FDR for a team's next n fixtures, plus a short string like
    'ARS(H) MCI(A) ...' for display."""
    upcoming = [
        f for f in fixtures
        if not f["finished"] and f["event"] is not None and f["event"] >= from_event
        and (f["team_h"] == team_id or f["team_a"] == team_id)
    ]
    upcoming = sorted(upcoming, key=lambda f: f["event"])[:n]
    if not upcoming:
        return None, "No fixtures scheduled"
    diffs = []
    labels = []
    teams_by_id = st.session_state["teams_by_id"]
    for f in upcoming:
        home = f["team_h"] == team_id
        opp_id = f["team_a"] if home else f["team_h"]
        diff = f["team_h_difficulty"] if home else f["team_a_difficulty"]
        diffs.append(diff)
        opp_short = teams_by_id.get(opp_id, {}).get("short_name", "?")
        labels.append(f"{opp_short}({'H' if home else 'A'})")
    avg = sum(diffs) / len(diffs)
    return avg, "  ".join(labels)



def fixture_details(team_id, fixtures, from_event, n=5):
    """Return structured upcoming fixtures for chips, FDR and opponent-strength logic."""
    teams_by_id = st.session_state["teams_by_id"]
    upcoming = [
        f for f in fixtures
        if not f.get("finished") and f.get("event") is not None and f["event"] >= from_event
        and (f["team_h"] == team_id or f["team_a"] == team_id)
    ]
    upcoming = sorted(upcoming, key=lambda f: f["event"])[:n]
    details = []
    for f in upcoming:
        home = f["team_h"] == team_id
        opp_id = f["team_a"] if home else f["team_h"]
        fdr = f["team_h_difficulty"] if home else f["team_a_difficulty"]
        details.append({
            "event": f["event"],
            "home": home,
            "opp_id": opp_id,
            "opp": teams_by_id.get(opp_id, {}).get("short_name", "?"),
            "fdr": int(fdr),
        })
    return details


def fixture_chips_html(details, max_items=6):
    if not details:
        return '<span class="pill">No fixtures</span>'
    chips = []
    for d in details[:max_items]:
        bg = FDR_COLORS.get(d["fdr"], "#E7E7E7")
        label = f'{d["opp"]} {"H" if d["home"] else "A"}'
        chips.append(f'<span class="fixture-chip" style="background:{bg}">{label}</span>')
    return '<div class="fixture-row">' + ''.join(chips) + '</div>'


def position_fixture_score(team_id, fixtures, teams_by_id, from_event, position_id, n=5):
    """0-5 fixture score adjusted for what matters by position.

    Attackers face opponent defensive strength; GKP/DEF face opponent attacking strength.
    Official FPL FDR remains the anchor and team-strength fields provide a directional tilt.
    """
    details = fixture_details(team_id, fixtures, from_event, n=n)
    if not details:
        return 3.0

    # Pull the relevant opponent strength measure per venue. FPL team strength fields
    # are relative ratings, so normalise against the current league rather than assume
    # a hard-coded range.
    keys = ("strength_attack_home", "strength_attack_away") if position_id in (1, 2) else ("strength_defence_home", "strength_defence_away")
    league_values = []
    for t in teams_by_id.values():
        for k in keys:
            v = t.get(k)
            if isinstance(v, (int, float)):
                league_values.append(float(v))
    lo = min(league_values) if league_values else 900.0
    hi = max(league_values) if league_values else 1400.0
    span = max(1.0, hi - lo)

    per_fixture = []
    for d in details:
        opp = teams_by_id.get(d["opp_id"], {})
        # If our player is home, opponent is away and vice versa.
        if position_id in (1, 2):
            key = "strength_attack_away" if d["home"] else "strength_attack_home"
        else:
            key = "strength_defence_away" if d["home"] else "strength_defence_home"
        strength = float(opp.get(key, (lo + hi) / 2) or (lo + hi) / 2)
        strength_ease = 5.0 - 4.0 * ((strength - lo) / span)  # weaker opponent => higher score
        fdr_ease = 6.0 - float(d["fdr"])
        home_bonus = 0.15 if d["home"] else 0.0
        per_fixture.append(max(1.0, min(5.0, 0.72 * fdr_ease + 0.28 * strength_ease + home_bonus)))
    return round(sum(per_fixture) / len(per_fixture), 2)


def safe_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return float(default)


def official_xgi90(p):
    """Prefer FPL's own expected-goal-involvement per 90 when exposed by bootstrap."""
    direct = safe_float(p.get("expected_goal_involvements_per_90"), -1)
    if direct >= 0:
        return direct
    mins = safe_float(p.get("minutes"), 0)
    if mins > 0:
        xgi = safe_float(p.get("expected_goal_involvements"), 0)
        if xgi > 0:
            return xgi / mins * 90
    xg90 = safe_float(p.get("expected_goals_per_90"), 0)
    xa90 = safe_float(p.get("expected_assists_per_90"), 0)
    return xg90 + xa90


def understat_xgi90(understat_match):
    if not understat_match or safe_float(understat_match.get("time"), 0) <= 0:
        return None
    mins = safe_float(understat_match.get("time"), 0)
    return (safe_float(understat_match.get("xG")) + safe_float(understat_match.get("xA"))) / mins * 90


def blended_xgi90(p, understat_match):
    official = official_xgi90(p)
    extra = understat_xgi90(understat_match)
    if official > 0 and extra is not None:
        return round(0.70 * official + 0.30 * extra, 3), "FPL + Understat"
    if official > 0:
        return round(official, 3), "FPL"
    if extra is not None:
        return round(extra, 3), "Understat"
    return 0.0, "Unavailable"


def set_piece_profile(p):
    """Use official FPL set-piece order fields when present in bootstrap-static."""
    pens = p.get("penalties_order")
    direct = p.get("direct_freekicks_order")
    corners = p.get("corners_and_indirect_freekicks_order")
    badges = []
    score = 0.0
    if pens == 1:
        badges.append("Pens #1")
        score += 3.2
    elif pens == 2:
        badges.append("Pens #2")
        score += 1.5
    if direct == 1:
        badges.append("Direct FK #1")
        score += 1.0
    elif direct == 2:
        score += 0.4
    if corners == 1:
        badges.append("Corners #1")
        score += 1.0
    elif corners == 2:
        score += 0.4
    return min(5.0, score), badges


def defensive_contribution_score(p):
    """0-5 signal for 2026/27 defensive-contribution potential when FPL exposes a per-90 field.

    Field naming can evolve, so the app checks a small set of plausible official-payload
    keys and otherwise stays neutral rather than inventing data.
    """
    per90 = None
    for key in ("defensive_contribution_per_90", "defensive_contributions_per_90", "defensive_contributions_per90"):
        if p.get(key) not in (None, ""):
            per90 = safe_float(p.get(key), 0)
            break
    if per90 is None:
        return 2.5
    pos = int(p.get("element_type", 0) or 0)
    threshold = 10.0 if pos == 2 else 12.0 if pos in (3, 4) else 12.0
    # A player averaging the single-match threshold per 90 is an elite DC profile.
    return round(max(0.0, min(5.0, per90 / threshold * 5.0)), 2)


def defensive_upside_score(p):
    """0-5 defensive/goalkeeping upside from official FPL fields."""
    pos = int(p.get("element_type", 0) or 0)
    if pos not in (1, 2):
        return 2.5
    xgc90 = safe_float(p.get("expected_goals_conceded_per_90"), 0)
    clean_sheets = safe_float(p.get("clean_sheets"), 0)
    starts = max(1.0, safe_float(p.get("starts"), 0))
    cs_rate = clean_sheets / starts
    base = 2.5 + min(1.4, cs_rate * 4.5)
    if xgc90 > 0:
        base += max(-1.2, min(1.2, (1.45 - xgc90) * 1.25))
    if pos == 1:
        saves = safe_float(p.get("saves_per_90"), 0)
        base += min(0.8, saves / 6)
    return round(max(0.0, min(5.0, base)), 2)


def bonus_bps_score(p):
    """0-5 proxy for bonus-point involvement using official bonus/BPS data."""
    starts = max(1.0, safe_float(p.get("starts"), 0))
    bonus_per_start = safe_float(p.get("bonus"), 0) / starts
    bps_per_start = safe_float(p.get("bps"), 0) / starts
    return round(max(0.0, min(5.0, bonus_per_start * 2.0 + bps_per_start / 18.0)), 2)


def captaincy_score(p, fixture_score, xgi90, set_piece_score):
    """0-5 captaincy-upside indicator; mainly useful for MID/FWD premiums."""
    if int(p.get("element_type", 0) or 0) not in (3, 4):
        return 0.0
    ep = safe_float(p.get("ep_next"), 0)
    form = safe_float(p.get("form"), 0)
    raw = 0.34 * min(5, ep) + 0.24 * min(5, xgi90 * 7.5) + 0.20 * fixture_score + 0.12 * min(5, form) + 0.10 * set_piece_score
    return round(max(0.0, min(5.0, raw)), 2)


def player_fpl_components(p, fixtures, teams_by_id, from_event, n_fixtures, understat_match):
    fixture_score = position_fixture_score(p["team"], fixtures, teams_by_id, from_event, p["element_type"], n=n_fixtures)
    xgi90, xgi_source = blended_xgi90(p, understat_match)
    sp_score, sp_badges = set_piece_profile(p)
    return {
        "fixture_score": fixture_score,
        "xgi90": xgi90,
        "xgi_source": xgi_source,
        "set_piece_score": sp_score,
        "set_piece_badges": sp_badges,
        "defensive_upside": defensive_upside_score(p),
        "defensive_contribution": defensive_contribution_score(p),
        "bonus_bps": bonus_bps_score(p),
        "captaincy": captaincy_score(p, fixture_score, xgi90, sp_score),
    }


def recommendation_reasons(candidate, comps):
    reasons = []
    if comps["fixture_score"] >= 3.8:
        reasons.append("strong position-adjusted fixtures")
    if comps["xgi90"] >= 0.45:
        reasons.append(f'high xGI/90 ({comps["xgi90"]:.2f})')
    if comps["set_piece_badges"]:
        reasons.append("set-piece role: " + ", ".join(comps["set_piece_badges"][:2]))
    if minutes_security_score(candidate) >= 4.0:
        reasons.append("secure minutes")
    if comps["defensive_upside"] >= 3.8 and candidate.get("element_type") in (1, 2):
        reasons.append("good clean-sheet/defensive profile")
    if comps.get("defensive_contribution", 2.5) >= 3.8:
        reasons.append("defensive-contribution upside")
    if comps["captaincy"] >= 3.8:
        reasons.append("captaincy upside")
    if safe_float(candidate.get("form"), 0) >= 4:
        reasons.append("good recent form")
    return reasons[:4]


def minutes_security_score(p):
    """0-5 score: rewards players who reliably accumulate minutes and starts.

    At the very start of a season, lack of minutes is treated as neutral rather than
    as evidence of rotation risk.
    """
    minutes = safe_float(p.get("minutes"), 0)
    starts = safe_float(p.get("starts"), 0)
    if minutes <= 0 and starts <= 0:
        return 3.0 if p.get("status") == "a" else 1.5
    mins_per_start = minutes / starts if starts > 0 else minutes
    if mins_per_start >= 80:
        return 5.0
    if mins_per_start >= 70:
        return 4.2
    if mins_per_start >= 60:
        return 3.4
    if mins_per_start >= 45:
        return 2.4
    return 1.4


def value_score(p):
    """0-5 price efficiency using PPG, with a neutral early-season fallback."""
    price = safe_float(p.get("now_cost"), 0) / 10
    ppg = safe_float(p.get("points_per_game"), 0)
    if price <= 0:
        return 0.0
    if ppg <= 0 and safe_float(p.get("minutes"), 0) <= 0:
        return 2.5
    return max(0.0, min(5.0, (ppg / price) * 7.5))


def ownership_strategy_adjustment(p, strategy):
    """Small ownership tilt: differentials favour low ownership; protect-rank favours template picks."""
    own = safe_float(p.get("selected_by_percent"), 0)
    if strategy == "Differential":
        if own < 5:
            return 0.6
        if own < 10:
            return 0.3
        if own > 25:
            return -0.3
    elif strategy == "Protect rank":
        if own >= 20:
            return 0.5
        if own >= 10:
            return 0.25
        if own < 5:
            return -0.2
    return 0.0


def player_score(p, avg_fdr, understat_match, total_players, strategy="Balanced", risk_appetite=3,
                 fixtures=None, teams_by_id=None, from_event=None, n_fixtures=5):
    """Composite FPL desirability score with strategy + position-aware signals."""
    form = safe_float(p.get("form"), 0)
    ep_next = safe_float(p.get("ep_next"), 0)
    availability = 5.0 if p.get("status") == "a" else 1.5 if p.get("status") == "d" else 0.0
    generic_fixture_ease = (6 - avg_fdr) if avg_fdr is not None else 3.0

    momentum_score, _ = ownership_momentum(p, total_players)
    momentum_norm = max(0.0, min(5.0, 2.5 + momentum_score / 2))
    minutes_score = minutes_security_score(p)
    val_score = value_score(p)

    # Richer FPL signals. Fall back gracefully if called without fixture context.
    if fixtures is not None and teams_by_id is not None and from_event is not None:
        comps = player_fpl_components(p, fixtures, teams_by_id, from_event, n_fixtures, understat_match)
        fixture_signal = comps["fixture_score"]
        xgi_signal = min(5.0, comps["xgi90"] * 8.0)
    else:
        xgi, _ = blended_xgi90(p, understat_match)
        comps = {
            "fixture_score": generic_fixture_ease,
            "xgi90": xgi,
            "set_piece_score": set_piece_profile(p)[0],
            "defensive_upside": defensive_upside_score(p),
            "defensive_contribution": defensive_contribution_score(p),
            "bonus_bps": bonus_bps_score(p),
            "captaincy": 0.0,
        }
        fixture_signal = generic_fixture_ease
        xgi_signal = min(5.0, xgi * 8.0)

    weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["Balanced"]).copy()
    risk_delta = (risk_appetite - 3) * 0.015
    weights["underlying"] += risk_delta
    weights["momentum"] += risk_delta / 2
    weights["minutes"] -= risk_delta
    weights["availability"] -= risk_delta / 2

    # Base score remains explainable and stable across positions.
    raw = (
        min(5.0, form) * weights["form"]
        + min(5.0, ep_next) * weights["ep"]
        + fixture_signal * weights["fixtures"]
        + availability * weights["availability"]
        + xgi_signal * weights["underlying"]
        + momentum_norm * weights["momentum"]
        + minutes_score * weights["minutes"]
        + val_score * weights["value"]
    )

    # Position-specific FPL layer. These are small adjustments, not a second score,
    # so a single specialist metric cannot overwhelm the core evidence.
    pos = int(p.get("element_type", 0) or 0)
    if pos in (1, 2):
        raw += 0.14 * (comps["defensive_upside"] - 2.5)
        raw += 0.07 * (comps["defensive_contribution"] - 2.5)
        raw += 0.05 * (comps["bonus_bps"] - 2.5)
        if pos == 2:
            raw += 0.04 * (xgi_signal - 2.5)  # attacking defenders still matter
    elif pos in (3, 4):
        raw += 0.09 * (comps["set_piece_score"] - 2.0)
        raw += 0.04 * (comps["defensive_contribution"] - 2.5)
        raw += 0.05 * (comps["bonus_bps"] - 2.5)
        raw += 0.04 * (comps["captaincy"] - 2.5)

    raw += ownership_strategy_adjustment(p, strategy)
    return round(raw, 2)

def confidence_label(score_delta, candidate):
    """Simple explainable confidence label for a proposed transfer."""
    status = candidate.get("status")
    mins_score = minutes_security_score(candidate)
    if score_delta >= 1.0 and status == "a" and mins_score >= 3.4:
        return "HIGH"
    if score_delta >= 0.4 and status in ("a", "d"):
        return "MEDIUM"
    return "LOW"


def flag_reasons(p, avg_fdr):
    reasons = []
    if p["status"] in ("i", "s", "u"):
        reasons.append(f"⛔ {STATUS_LABELS.get(p['status'], p['status'])}" + (f" — {p['news']}" if p["news"] else ""))
    elif p["status"] == "d":
        cop = p.get("chance_of_playing_next_round")
        reasons.append(f"⚠️ Doubtful ({cop}% chance)" + (f" — {p['news']}" if p["news"] else ""))
    form = float(p["form"] or 0)
    ppg = float(p["points_per_game"] or 0)
    if form < 2.5 and ppg >= 3:
        reasons.append(f"📉 Poor recent form ({form} vs {ppg} season PPG)")
    if avg_fdr is not None and avg_fdr >= 3.8:
        reasons.append(f"🗓️ Tough run of fixtures (avg FDR {avg_fdr:.1f}/5)")
    return reasons




# ----------------------------------------------------------------------------
# ENRICHMENT + FORECASTING
# ----------------------------------------------------------------------------
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
FOOTBALL_DATA_TTL = 6 * 3600  # recent results/form only need a few refreshes per day
HISTORICAL_TTL = 24 * 3600

def get_secret(name, default=""):
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)

def _safe_int_header(headers, name, default=None):
    try:
        value = headers.get(name)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default

@st.cache_data(ttl=FOOTBALL_DATA_TTL, show_spinner=False)
def get_football_data_bundle(api_key, season_start_year):
    """
    Rate-limit-aware football-data.org enrichment.

    One broad Premier League matches request is cached for six hours. The function
    never retries a 429 response; instead it returns status metadata and lets the
    app fall back to FPL + Understat. This prevents normal Streamlit reruns or page
    navigation from repeatedly consuming the free API allowance.
    """
    if not api_key:
        return {
            "matches": [], "status": "not_configured", "http_status": None,
            "requests_remaining": None, "reset_seconds": None,
            "fetched_at": None, "error": None,
        }

    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        response = requests.get(
            f"{FOOTBALL_DATA_BASE}/competitions/PL/matches",
            params={"season": int(season_start_year)},
            headers={"X-Auth-Token": api_key},
            timeout=(CORE_CONNECT_TIMEOUT, SUPPLEMENTARY_TIMEOUT),
        )

        remaining = _safe_int_header(response.headers, "X-RequestsAvailable")
        reset_seconds = _safe_int_header(response.headers, "X-RequestCounter-Reset")

        if response.status_code == 429:
            return {
                "matches": [], "status": "rate_limited", "http_status": 429,
                "requests_remaining": remaining, "reset_seconds": reset_seconds,
                "fetched_at": fetched_at,
                "error": "football-data.org rate limit reached; enrichment disabled until a later cached refresh.",
            }

        if response.status_code in (401, 403):
            return {
                "matches": [], "status": "auth_error", "http_status": response.status_code,
                "requests_remaining": remaining, "reset_seconds": reset_seconds,
                "fetched_at": fetched_at,
                "error": "football-data.org rejected the API token.",
            }

        response.raise_for_status()
        payload = response.json()
        return {
            "matches": payload.get("matches", []),
            "status": "ok",
            "http_status": response.status_code,
            "requests_remaining": remaining,
            "reset_seconds": reset_seconds,
            "fetched_at": fetched_at,
            "error": None,
        }
    except requests.exceptions.RequestException as exc:
        return {
            "matches": [], "status": "unavailable", "http_status": None,
            "requests_remaining": None, "reset_seconds": None,
            "fetched_at": fetched_at, "error": str(exc),
        }
    except (ValueError, TypeError) as exc:
        return {
            "matches": [], "status": "invalid_response", "http_status": None,
            "requests_remaining": None, "reset_seconds": None,
            "fetched_at": fetched_at, "error": str(exc),
        }

@st.cache_data(ttl=HISTORICAL_TTL, show_spinner=False)
def get_historical_position_baselines():
    """Historical realised-points priors; intentionally excludes xP/look-ahead fields."""
    defaults = {"GK": 3.2, "GKP": 3.2, "DEF": 3.6, "MID": 4.1, "FWD": 4.0}
    frames=[]
    for season in ("2024-25","2025-26"):
        url=f"https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}/gws/merged_gw.csv"
        try:
            rr=requests.get(url,timeout=(CORE_CONNECT_TIMEOUT, SUPPLEMENTARY_TIMEOUT)); rr.raise_for_status()
            df=pd.read_csv(io.StringIO(rr.text),usecols=lambda c: c in {"position","total_points","minutes"})
            frames.append(df)
        except Exception:
            pass
    if not frames:
        return defaults
    df=pd.concat(frames,ignore_index=True)
    mins=pd.to_numeric(df.get("minutes"),errors="coerce").fillna(0)
    df=df[mins>=60]
    if df.empty or "position" not in df:
        return defaults
    out=defaults.copy()
    for pos,grp in df.groupby("position"):
        pts=pd.to_numeric(grp["total_points"],errors="coerce").dropna()
        if not pts.empty:
            out[str(pos)]=float(pts.clip(-2,15).mean())
    return out


def gw1_is_preseason(events):
    # True only before the GW1 deadline/start; builder vanishes afterwards.
    if not events:
        return False
    gw1 = sorted(events, key=lambda e: e.get("id", 99))[0]
    if gw1.get("started") or gw1.get("finished") or gw1.get("is_current"):
        return False
    try:
        deadline = datetime.fromisoformat(gw1["deadline_time"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < deadline
    except Exception:
        return True


def previous_season_label(start_year):
    y = int(start_year) - 1
    return f"{y}-{str(y+1)[-2:]}"


@st.cache_data(ttl=HISTORICAL_TTL, show_spinner=False)
def get_previous_season_players(season_label):
    # Prior-season realised FPL stats from the public Vaastav dataset.
    urls = [
        f"https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season_label}/players_raw.csv",
        f"https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season_label}/players.csv",
    ]
    for url in urls:
        try:
            rr = requests.get(url, timeout=(CORE_CONNECT_TIMEOUT, SUPPLEMENTARY_TIMEOUT))
            rr.raise_for_status()
            df = pd.read_csv(io.StringIO(rr.text))
            if df.empty:
                continue
            out = {}
            for _, row in df.iterrows():
                first = str(row.get("first_name", "") or "")
                second = str(row.get("second_name", "") or "")
                web = str(row.get("web_name", "") or "")
                full = normalize_name((first + " " + second).strip())
                key = full or normalize_name(web)
                if not key:
                    continue
                mins = safe_float(row.get("minutes"), 0)
                pts = safe_float(row.get("total_points"), 0)
                starts = safe_float(row.get("starts"), 0)
                out[key] = {
                    "minutes": mins, "points": pts, "starts": starts,
                    "ppg": safe_float(row.get("points_per_game"), 0),
                    "xgi": safe_float(row.get("expected_goal_involvements"), 0),
                    "bonus": safe_float(row.get("bonus"), 0), "bps": safe_float(row.get("bps"), 0),
                }
            if out:
                return out
        except Exception:
            continue
    return {}


def match_previous_season_player(p, lookup):
    full = normalize_name(f"{p.get('first_name','')} {p.get('second_name','')}")
    if full in lookup:
        return lookup[full]
    return lookup.get(normalize_name(p.get("web_name", "")), {})


def load_expert_consensus():
    # Optional repo CSV: player, score (0-5), mentions, note.
    for path in ("expert_consensus.csv", "data/expert_consensus.csv"):
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            if "player" not in df.columns:
                continue
            return {
                normalize_name(str(r.get("player", ""))): {
                    "score": max(0.0, min(5.0, safe_float(r.get("score"), 0))),
                    "mentions": int(safe_float(r.get("mentions"), 0)),
                    "note": str(r.get("note", "") or ""),
                }
                for _, r in df.iterrows()
            }
        except Exception:
            pass
    return {}


def preseason_player_profile(p, fixtures, teams_by_id, previous_lookup, previous_understat, prev_us_full, prev_us_last, expert_lookup, start_event=1):
    prev = match_previous_season_player(p, previous_lookup)
    us = match_understat(p, prev_us_full, prev_us_last) if previous_understat else None
    mins = safe_float(prev.get("minutes"), 0)
    pts = safe_float(prev.get("points"), 0)
    p90 = (pts / mins * 90) if mins > 180 else 0.0
    prev_ppg = safe_float(prev.get("ppg"), 0)
    starts = safe_float(prev.get("starts"), 0)
    minute_security = 2.5
    if starts >= 25 and mins / max(1, starts) >= 75:
        minute_security = 5.0
    elif starts >= 18:
        minute_security = 4.2
    elif starts >= 10:
        minute_security = 3.4
    elif mins > 0:
        minute_security = 2.4
    uxgi90 = 0.0
    if us and safe_float(us.get("time"), 0) > 180:
        uxgi90 = (safe_float(us.get("xG"), 0) + safe_float(us.get("xA"), 0)) / safe_float(us.get("time"), 1) * 90
    elif mins > 180:
        uxgi90 = safe_float(prev.get("xgi"), 0) / mins * 90
    fx3 = position_fixture_score(p["team"], fixtures, teams_by_id, start_event, p["element_type"], n=3)
    fx5 = position_fixture_score(p["team"], fixtures, teams_by_id, start_event, p["element_type"], n=5)
    sp_score, sp_badges = set_piece_profile(p)
    own = safe_float(p.get("selected_by_percent"), 0)
    expert = expert_lookup.get(normalize_name(p.get("web_name", "")), {})
    expert_score = safe_float(expert.get("score"), 0)
    availability = 5.0 if p.get("status") == "a" else 2.5 if p.get("status") == "d" else 0.0
    pos = int(p.get("element_type", 0) or 0)
    hist_attack = min(5.0, uxgi90 * (7.2 if pos in (3, 4) else 5.0))
    hist_points = min(5.0, max(prev_ppg, p90) * 0.9)
    template = min(5.0, own / 8.0)
    base_score = (0.25 * hist_points + 0.19 * hist_attack + 0.18 * fx3 + 0.10 * fx5
                  + 0.11 * minute_security + 0.06 * sp_score + 0.06 * availability
                  + 0.03 * template + 0.02 * expert_score)
    price = safe_float(p.get("now_cost"), 0) / 10
    value = base_score / max(4.0, price) * 5.0
    proj1 = max(1.0, 0.65 * max(prev_ppg, p90, 2.6) + 0.42 * (fx3 - 3.0) + 0.28 * hist_attack + 0.12 * sp_score)
    proj3 = proj1 * 3 * (0.93 + 0.035 * (fx3 - 3.0))
    proj5 = proj1 * 5 * (0.92 + 0.03 * (fx5 - 3.0))
    return {
        "id": p["id"], "Player": p["web_name"], "Team": teams_by_id[p["team"]]["short_name"],
        "Pos": POSITION_MAP[p["element_type"]], "Price": price,
        "Build score": round(base_score, 3), "Value": round(value, 2),
        "Proj GW1": round(proj1, 1), "Proj 3": round(proj3, 1), "Proj 5": round(proj5, 1),
        "Prior minutes": round(mins), "Prior PPG": round(prev_ppg, 2), "Prior pts/90": round(p90, 2), "Prior xGI/90": round(uxgi90, 2),
        "Minutes": round(minute_security, 1), "Fixtures 3": round(fx3, 2), "Fixtures 5": round(fx5, 2),
        "Set pieces": ", ".join(sp_badges) or "—", "Own %": own, "Expert": expert_score,
        "Expert mentions": int(expert.get("mentions", 0) or 0), "Expert note": expert.get("note", ""),
        "Status": STATUS_LABELS.get(p.get("status"), p.get("status")),
        "Captaincy": round(min(5.0, proj1 / 1.5 + sp_score * 0.15), 2),
        "Fixtures": fixture_details(p["team"], fixtures, start_event, n=5),
    }


def squad_style_utility(row, style):
    utility = row["Build score"]
    if style == "Template / Safe":
        utility += min(0.7, row["Own %"] / 45.0) + 0.10 * row["Minutes"]
    elif style == "Aggressive / Differential":
        utility += max(0.0, (12.0 - row["Own %"]) / 25.0) + 0.10 * row["Prior xGI/90"] * 5
    elif style == "Value":
        utility += 0.20 * row["Value"]
    elif style == "First 3 GWs":
        utility += 0.06 * row["Proj 3"]
    elif style == "Premium captaincy":
        utility += 0.08 * row["Proj GW1"] + (0.35 if row["Price"] >= 10 and row["Pos"] in ("MID", "FWD") else 0)
    return utility


def optimise_gw1_squad(player_rows, style="Balanced", budget=100.0, bank_target=0.0):
    df = pd.DataFrame(player_rows)
    if df.empty:
        return []
    df = df[(df["Status"].isin(["Available", "Doubtful"])) & (df["Price"] > 0)].copy()
    df["Utility"] = df.apply(lambda r: squad_style_utility(r, style), axis=1)
    pool_limits = {"GKP": 12, "DEF": 18, "MID": 18, "FWD": 14}
    need = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
    group_choices = {}
    for pos, n in need.items():
        g = df[df["Pos"] == pos].sort_values(["Utility", "Value"], ascending=False).head(pool_limits[pos])
        choices = []
        for combo in combinations(g.to_dict("records"), n):
            counts = defaultdict(int)
            for r in combo:
                counts[r["Team"]] += 1
            if max(counts.values(), default=0) > 3:
                continue
            choices.append((sum(r["Utility"] for r in combo), sum(r["Price"] for r in combo), combo, dict(counts)))
        group_choices[pos] = sorted(choices, key=lambda x: x[0], reverse=True)[:220]
    states = [(0.0, 0.0, tuple(), {})]
    spend_limit = budget - bank_target
    for pos in ("GKP", "DEF", "MID", "FWD"):
        nxt = []
        for util0, cost0, rows0, counts0 in states:
            for util, cost, combo, counts in group_choices.get(pos, []):
                if cost0 + cost > spend_limit + 1e-9:
                    continue
                merged = dict(counts0)
                for team, c in counts.items():
                    merged[team] = merged.get(team, 0) + c
                if max(merged.values(), default=0) <= 3:
                    nxt.append((util0 + util, cost0 + cost, rows0 + combo, merged))
        states = sorted(nxt, key=lambda x: (x[0], -x[1]), reverse=True)[:1200]
        if not states:
            return []
    return list(states[0][2])


def render_gw1_builder(bootstrap, fixtures, teams_by_id, season_start_year, deadline_text):
    st.markdown('<div class="fpl-hero"><div class="fpl-eyebrow">Pre-season workspace · automatically hides after GW1 begins</div><h1>GW1 Squad Builder</h1><p>Build a legal £100m squad from live 2026/27 prices and positions, opening fixtures and prior-season evidence.</p></div>', unsafe_allow_html=True)
    st.info(f"This is separate from the in-season Transfer Advisor and disappears automatically once GW1 begins. GW1 deadline: **{deadline_text}**.")
    with st.spinner("Preparing pre-season player model…"):
        previous_label = previous_season_label(season_start_year)
        prev_lookup = get_previous_season_players(previous_label)
        try:
            prev_understat = get_understat_players(int(season_start_year) - 1)
        except Exception:
            prev_understat = {}
        prev_us_full, prev_us_last = build_understat_lookup(prev_understat)
        expert_lookup = load_expert_consensus()
        rows = [preseason_player_profile(p, fixtures, teams_by_id, prev_lookup, prev_understat, prev_us_full, prev_us_last, expert_lookup, 1) for p in bootstrap["elements"]]
    a, b, c, d = st.columns(4)
    a.metric("Live player pool", len(rows)); b.metric("Prior-player matches", len(prev_lookup)); c.metric("Underlying profiles", len(prev_understat)); d.metric("Expert-rated players", len(expert_lookup))
    c1, c2, c3 = st.columns([1.3, 1, 1])
    with c1:
        style = st.selectbox("Squad style", ["Balanced", "Template / Safe", "First 3 GWs", "Aggressive / Differential", "Value", "Premium captaincy"])
    with c2:
        bank_target = st.number_input("Leave in bank (£m)", 0.0, 5.0, 0.0, 0.5, key="gw1_bank")
    with c3:
        min_prior_minutes = st.number_input("Min prior-season minutes", 0, 3000, 450, 90)
    eligible = [r for r in rows if r["Prior minutes"] >= min_prior_minutes or r["Prior PPG"] == 0]
    squad = optimise_gw1_squad(eligible, style, 100.0, bank_target)
    if not squad:
        st.warning("No legal squad could be generated with those filters. Reduce the prior-minutes threshold or bank target.")
        return
    sdf = pd.DataFrame(squad)
    total_cost = sdf["Price"].sum(); projected = sdf["Proj 3"].sum(); ownership = sdf["Own %"].mean()
    x, y, z, w = st.columns(4)
    x.metric("Squad cost", f"£{total_cost:.1f}m", f"£{100-total_cost:.1f}m bank"); y.metric("15-player 3GW projection", f"{projected:.1f}"); z.metric("Average ownership", f"{ownership:.1f}%"); w.metric("Prior-data coverage", f"{sum(sdf['Prior PPG']>0)}/15")
    xi, bench, caps = optimal_starting_xi(sdf.to_dict("records"))
    st.markdown("### Suggested starting XI")
    st.dataframe(pd.DataFrame(xi)[["Pos", "Player", "Team", "Price", "Proj GW1", "Proj 3", "Prior xGI/90", "Fixtures 3", "Set pieces", "Own %"]], use_container_width=True, hide_index=True)
    if caps:
        st.success(f"Captain: **{caps[0]['Player']}** · Vice-captain: **{caps[1]['Player'] if len(caps)>1 else '—'}**")
    st.caption("Bench: " + " → ".join(f"{r['Player']} (£{r['Price']:.1f}m)" for r in bench))
    st.markdown("### Full 15-player squad")
    st.dataframe(sdf[["Pos", "Player", "Team", "Price", "Proj GW1", "Proj 3", "Proj 5", "Prior PPG", "Prior pts/90", "Prior xGI/90", "Minutes", "Fixtures 3", "Set pieces", "Own %", "Expert"]].sort_values(["Pos", "Price"], ascending=[True, False]), use_container_width=True, hide_index=True)
    st.markdown("### Compare structures")
    summaries = []
    for alt in ["Balanced", "Template / Safe", "Aggressive / Differential", "Value"]:
        sq = optimise_gw1_squad(eligible, alt, 100.0, bank_target)
        if sq:
            dd = pd.DataFrame(sq)
            summaries.append({"Structure": alt, "Cost": round(dd["Price"].sum(), 1), "3GW projection": round(dd["Proj 3"].sum(), 1), "Avg ownership %": round(dd["Own %"].mean(), 1), "Premium core": ", ".join(dd.sort_values("Price", ascending=False).head(3)["Player"].tolist())})
    if summaries:
        st.dataframe(pd.DataFrame(summaries), use_container_width=True, hide_index=True)
    with st.expander("Expert consensus layer", expanded=False):
        if expert_lookup:
            st.caption("Loaded from `expert_consensus.csv` in your repository. Expert opinion remains a small, transparent input rather than overriding the statistical model.")
            expert_rows = [r for r in rows if r["Expert"] > 0]
            if expert_rows:
                st.dataframe(pd.DataFrame(expert_rows)[["Player", "Team", "Pos", "Expert", "Expert mentions", "Expert note", "Build score"]].sort_values("Expert", ascending=False), use_container_width=True, hide_index=True)
        else:
            st.caption("Optional: add `expert_consensus.csv` with columns `player,score,mentions,note` (score 0–5). This avoids fragile automatic scraping of editorial sites.")
    st.caption("Pre-season projections are lower-confidence than in-season forecasts. Live FPL prices/positions are authoritative; prior-season and expert layers are supplementary.")

def build_team_form(matches):
    by_team=defaultdict(list)
    for m in matches or []:
        if m.get("status")!="FINISHED": continue
        h=m.get("homeTeam",{}).get("tla") or ""; a=m.get("awayTeam",{}).get("tla") or ""
        score=m.get("score",{}).get("fullTime",{}); hg=score.get("home"); ag=score.get("away")
        if not h or not a or hg is None or ag is None: continue
        dt=m.get("utcDate","")
        by_team[h].append({"date":dt,"venue":"H","gf":hg,"ga":ag,"pts":3 if hg>ag else 1 if hg==ag else 0})
        by_team[a].append({"date":dt,"venue":"A","gf":ag,"ga":hg,"pts":3 if ag>hg else 1 if ag==hg else 0})
    form={}
    for tla,rows in by_team.items():
        rows=sorted(rows,key=lambda r:r["date"],reverse=True)
        def agg(xs):
            if not xs: return {"ppg":1.5,"gf":1.4,"ga":1.4,"n":0}
            return {"ppg":sum(r["pts"] for r in xs)/len(xs),"gf":sum(r["gf"] for r in xs)/len(xs),"ga":sum(r["ga"] for r in xs)/len(xs),"n":len(xs)}
        form[tla]={"overall":agg(rows[:5]),"home":agg([r for r in rows if r["venue"]=="H"][:5]),"away":agg([r for r in rows if r["venue"]=="A"][:5])}
    return form

def market_pressure(p,total_players):
    net=int(p.get("transfers_in_event",0) or 0)-int(p.get("transfers_out_event",0) or 0); pct=net/max(1,total_players)*100
    if pct>=0.8:return "Hot buy",pct
    if pct>=0.25:return "Buying pressure",pct
    if pct<=-0.5:return "Heavy selling",pct
    if pct<=-0.2:return "Selling pressure",pct
    return "Stable",pct

def eo_proxy(p,comps):
    own=min(100.0,safe_float(p.get("selected_by_percent"),0)); explosive=min(5.0,comps.get("captaincy",0)+comps.get("xgi90",0)*2.5)
    return round(min(100.0,own*.82+explosive*3.6),1)

def fixture_window(team_id,fixtures,teams_by_id,from_event,position_id):
    short=position_fixture_score(team_id,fixtures,teams_by_id,from_event,position_id,n=3); later=position_fixture_score(team_id,fixtures,teams_by_id,from_event+3,position_id,n=3); delta=round(short-later,2)
    label="Great short-term window" if delta>=.7 else "Fixtures improve later" if delta<=-.7 else "Stable fixture run"
    return short,later,delta,label

def congestion_score(team_id,fixtures,from_event,n=5,caution_clubs=None,short_name=None):
    fs=[f for f in fixtures if not f.get("finished") and f.get("event") is not None and f["event"]>=from_event and (f["team_h"]==team_id or f["team_a"]==team_id)]
    fs=sorted(fs,key=lambda x:x.get("kickoff_time") or "")[:n]; dates=[]
    for f in fs:
        try: dates.append(datetime.fromisoformat(f["kickoff_time"].replace("Z","+00:00")))
        except Exception: pass
    score=sum(1.0 for a,b in zip(dates,dates[1:]) if (b-a).days<=4)
    if short_name and caution_clubs and short_name.upper() in caution_clubs: score+=1.25
    return min(5.0,score)

def rotation_risk(p,congestion):
    return round(min(5.0,max(0.0,(5.0-minutes_security_score(p))*.7+congestion*.6)),2)

def team_form_fixture_adjustment(opp_short,is_home,position_id,team_form):
    if not team_form or opp_short not in team_form:return 0.0
    opp=team_form[opp_short]["away" if is_home else "home"]
    if opp.get("n",0)<2:return 0.0
    if position_id in (3,4):return max(-.35,min(.35,(opp["ga"]-1.35)*.25))
    return max(-.35,min(.35,(1.35-opp["gf"])*.25))

def event_projection(p,event_id,fixtures,teams_by_id,understat_match,historical_baselines,team_form=None):
    pos=int(p.get("element_type",0) or 0); details=[d for d in fixture_details(p["team"],fixtures,event_id,n=12) if d["event"]==event_id]
    if not details:return 0.0
    xgi90,_=blended_xgi90(p,understat_match); mins=minutes_security_score(p); minutes_factor=max(.25,min(1.0,mins/5)); availability=1.0 if p.get("status")=="a" else .55 if p.get("status")=="d" else .05
    key=POSITION_MAP.get(pos,"MID"); hist=historical_baselines.get(key,historical_baselines.get("GK" if key=="GKP" else key,3.8)); form=safe_float(p.get("form"),hist); ppg=safe_float(p.get("points_per_game"),hist); ep=safe_float(p.get("ep_next"),0); sp,_=set_piece_profile(p); bonus=bonus_bps_score(p); defensive=defensive_upside_score(p)
    total=0.0
    for d in details:
        ease=6.0-d["fdr"]; form_adj=team_form_fixture_adjustment(d["opp"],d["home"],pos,team_form or {}); appearance=1.75*minutes_factor
        attack_mult=3.2 if pos==3 else 2.9 if pos==4 else 2.0 if pos==2 else .4; attack=min(5.0,xgi90*attack_mult*(.86+ease*.055))
        defend=max(0.0,(ease-1.6)*.55+(defensive-2.5)*.22) if pos in (1,2) else max(0.0,(ease-2.0)*.10) if pos==3 else 0.0
        prior=.30*hist+.35*min(8,ppg)+.20*min(8,form)+(.25*ep if event_id==st.session_state.get("next_event_id") else 0)
        total+=max(0.0,appearance+attack+defend+(ease-3)*.22+form_adj+.12*sp+.11*bonus+.28*prior)*availability
    return round(total,1)

def projected_horizon(p,start_event,horizon,fixtures,teams_by_id,understat_match,historical_baselines,team_form=None):
    vals=[event_projection(p,ev,fixtures,teams_by_id,understat_match,historical_baselines,team_form) for ev in range(start_event,start_event+horizon)]
    return round(sum(vals),1),vals

def optimal_starting_xi(rows):
    rows=[dict(r) for r in rows]; gks=sorted([r for r in rows if r["Pos"]=="GKP"],key=lambda r:r["Proj GW1"],reverse=True); defs=sorted([r for r in rows if r["Pos"]=="DEF"],key=lambda r:r["Proj GW1"],reverse=True); mids=sorted([r for r in rows if r["Pos"]=="MID"],key=lambda r:r["Proj GW1"],reverse=True); fwds=sorted([r for r in rows if r["Pos"]=="FWD"],key=lambda r:r["Proj GW1"],reverse=True)
    xi=gks[:1]+defs[:3]+mids[:2]+fwds[:1]; selected={r["id"] for r in xi}; remaining=sorted([r for r in rows if r["id"] not in selected and r["Pos"]!="GKP"],key=lambda r:r["Proj GW1"],reverse=True); xi+=remaining[:4]; ids={r["id"] for r in xi}; bench=sorted([r for r in rows if r["id"] not in ids],key=lambda r:r["Proj GW1"],reverse=True); caps=sorted(xi,key=lambda r:(r.get("Captaincy",0),r.get("Proj GW1",0)),reverse=True)
    return xi,bench,caps[:2]

def chip_recommendations(rows,gw,free_transfers):
    expiry="before GW19" if gw<=19 else "before season end"; bench_strength=sum(sorted([r.get("Proj GW1",0) for r in rows],reverse=True)[11:15]); cap=max([r.get("Captaincy",0) for r in rows] or [0]); flagged=sum(bool(r.get("Needs attention")) for r in rows)
    return {"Wildcard":(("CONSIDER" if flagged>=5 and free_transfers<=2 else "WATCH" if flagged>=3 else "HOLD"),f"{flagged} squad issues · chip set expires {expiry}."),"Bench Boost":(("CONSIDER" if bench_strength>=13 else "WATCH" if bench_strength>=9 else "HOLD"),f"Bench projects about {bench_strength:.1f} points."),"Triple Captain":(("CONSIDER" if cap>=4.6 else "WATCH" if cap>=4.1 else "HOLD"),f"Best captaincy signal {cap:.1f}/5."),"Free Hit":(("WATCH" if sum(r.get("Proj GW1",0)==0 for r in rows)>=3 else "HOLD"),"Best reserved for major blank/double disruption.")}

def build_replacement_candidates(outgoing_row,bootstrap,squad_df,players_by_id,teams_by_id,fixtures,event_id,by_full,by_last,total_players,strategy,risk,n_fixtures,reserve_bank,bank,min_minutes,include_doubtful,hist,team_form,limit=12):
    outgoing=players_by_id[outgoing_row["id"]]; budget=outgoing_row["Sell price"]+max(0,bank-reserve_bank); squad_ids=set(squad_df["id"]); team_counts=squad_df["Team"].value_counts().to_dict(); out=[]
    for cand in bootstrap["elements"]:
        if cand["id"] in squad_ids or cand["element_type"]!=outgoing["element_type"]:continue
        cost=safe_float(cand.get("now_cost"),0)/10
        if cost>budget+.001:continue
        short=teams_by_id[cand["team"]]["short_name"]; count=team_counts.get(short,0)-(1 if short==outgoing_row["Team"] else 0)
        if count>=3:continue
        if cand.get("status") in ("i","s","u","n") or (cand.get("status")=="d" and not include_doubtful):continue
        mins=minutes_security_score(cand)
        if mins<min_minutes:continue
        avg,fx=team_fixture_difficulty(cand["team"],fixtures,event_id,n=n_fixtures); us=match_understat(cand,by_full,by_last); comps=player_fpl_components(cand,fixtures,teams_by_id,event_id,n_fixtures,us); score=player_score(cand,avg,us,total_players,strategy,risk,fixtures,teams_by_id,event_id,n_fixtures); p1,_=projected_horizon(cand,event_id,1,fixtures,teams_by_id,us,hist,team_form); p3,_=projected_horizon(cand,event_id,3,fixtures,teams_by_id,us,hist,team_form); p5,_=projected_horizon(cand,event_id,5,fixtures,teams_by_id,us,hist,team_form); market,mp=market_pressure(cand,total_players)
        out.append({"id":cand["id"],"Player":cand["web_name"],"Team":short,"Cost":cost,"Score":score,"Upgrade":round(score-outgoing_row["Advisor score"],2),"Proj GW1":p1,"Proj 3":p3,"Proj 5":p5,"xGI/90":comps["xgi90"],"Minutes":mins,"Captaincy":comps["captaincy"],"Own %":safe_float(cand.get("selected_by_percent"),0),"Market":market,"Market %":mp,"Reasons":recommendation_reasons(cand,comps),"Confidence":confidence_label(round(score-outgoing_row["Advisor score"],2),cand),"fixture_details":fixture_details(cand["team"],fixtures,event_id,n=n_fixtures)})
    return sorted(out,key=lambda r:(r["Proj 5"],r["Upgrade"]),reverse=True)[:limit]


# ----------------------------------------------------------------------------
# UI / PRODUCT SHELL
# ----------------------------------------------------------------------------
# Keep the team identifier in session state so changing it feels like switching
# accounts rather than filling in a configuration form on every visit.
if "team_id" not in st.session_state:
    st.session_state["team_id"] = "4935366"
team_id = str(st.session_state.get("team_id", "4935366")).strip()

if not team_id:
    team_id = "4935366"
    st.session_state["team_id"] = team_id
try:
    team_id_int = int(team_id)
except ValueError:
    team_id_int = 4935366
    st.session_state["team_id"] = str(team_id_int)

# Render a lightweight shell before any network work, so the page never looks like
# an empty Streamlit canvas while external services respond.
startup_slot = st.empty()
startup_slot.markdown(
    '<div class="empty-shell" style="padding-top:70px">'
    '<div class="empty-orb">⚽</div><h1>Connecting to FPL</h1>'
    '<p>Loading your team, fixtures and Gameweek context…</p></div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    startup_rail = st.empty()
    startup_rail.markdown(
        '<div class="rail-brand"><div class="mark">FPL</div><div class="copy"><h2>Transfer Advisor</h2>'
        '<p>Decision intelligence for your squad</p></div></div>'
        '<div class="rail-foot">Connecting to live FPL data…</div>',
        unsafe_allow_html=True,
    )

try:
    core_payload, core_timings = get_core_fpl_data(team_id_int)
    bootstrap = core_payload["bootstrap"]
    fixtures = core_payload["fixtures"]
    entry = core_payload["entry"]
    history = core_payload["history"]
    print(
        "[FPL Advisor startup] "
        + " · ".join(f"{k}={v:.3f}s" for k, v in sorted(core_timings.items()))
    )
except requests.exceptions.HTTPError as exc:
    startup_slot.empty(); startup_rail.empty()
    status = getattr(exc.response, "status_code", None)
    with st.sidebar:
        st.markdown('<div class="rail-brand"><div class="mark">FPL</div><div class="copy"><h2>Transfer Advisor</h2><p>Decision intelligence for your squad</p><span style="display:inline-block;margin-top:.4rem;padding:.18rem .48rem;border-radius:999px;background:rgba(0,255,135,.14);color:#7dffb2;font-size:.68rem;font-weight:700;letter-spacing:.05em">UI V2</span></div></div>', unsafe_allow_html=True)
        st.error("Team/data unavailable")
        st.text_input("FPL Team ID", key="team_id")
    if status == 404:
        st.error("I couldn't find that FPL Team ID. Change it in the sidebar and try again.")
    else:
        st.error(f"FPL returned HTTP {status or 'error'} while the app was starting. Please try Refresh live data shortly.")
    st.stop()
except requests.exceptions.RequestException as exc:
    startup_slot.empty(); startup_rail.empty()
    print(f"[FPL Advisor startup] FPL request failed: {type(exc).__name__}: {exc}")
    st.error("The FPL API did not respond within the startup timeout. The app stopped cleanly rather than spinning indefinitely. Try again in a moment.")
    st.stop()
except Exception as exc:
    startup_slot.empty(); startup_rail.empty()
    print(f"[FPL Advisor startup] unexpected startup failure: {type(exc).__name__}: {exc}")
    st.exception(exc)
    st.stop()
finally:
    startup_slot.empty()
    startup_rail.empty()

teams_by_id={t["id"]:t for t in bootstrap["teams"]}; st.session_state["teams_by_id"]=teams_by_id
players_by_id={p["id"]:p for p in bootstrap["elements"]}; events=bootstrap["events"]
total_players_in_game=bootstrap.get("total_players",1); squad_event=current_or_last_event(events); upcoming_event=next_event(events); st.session_state["next_event_id"]=upcoming_event["id"]
season_start_year=squad_event["deadline_time"][:4]
if int(squad_event["deadline_time"][5:7])<7: season_start_year=str(int(season_start_year)-1)
free_transfers=free_transfers_available(history,events)
deadline_dt=datetime.fromisoformat(upcoming_event["deadline_time"].replace("Z","+00:00")); deadline_text=deadline_dt.strftime("%a %d %b · %H:%M UTC")
pre_gw1 = gw1_is_preseason(events)

# Separate pre-season workspace. This executes before manager-picks handling so the
# builder is useful even while GW1 picks are not yet published.
if pre_gw1:
    with st.sidebar:
        st.markdown('<div class="rail-brand"><div class="mark">FPL</div><div class="copy"><h2>Transfer Advisor</h2><p>Decision intelligence for your squad</p><span style="display:inline-block;margin-top:.4rem;padding:.18rem .48rem;border-radius:999px;background:rgba(0,255,135,.14);color:#7dffb2;font-size:.68rem;font-weight:700;letter-spacing:.05em">GW1 PRE-SEASON</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="rail-section-label">Pre-season</div>', unsafe_allow_html=True)
        preseason_page = st.radio("Pre-season navigation", ["GW1 Squad Builder", "Season status"], label_visibility="collapsed")
        with st.expander("Team & data", expanded=False):
            st.text_input("FPL Team ID", key="team_id")
            if st.button("Refresh live data", type="primary", use_container_width=True, key="pre_refresh"):
                st.cache_data.clear(); st.rerun()
        st.markdown('<div class="rail-foot">The GW1 Builder disappears automatically after the GW1 deadline.</div>', unsafe_allow_html=True)
    if preseason_page == "GW1 Squad Builder":
        render_gw1_builder(bootstrap, fixtures, teams_by_id, season_start_year, deadline_text)
    else:
        st.markdown(f'<div class="empty-shell"><div class="empty-orb">⚽</div><h1>2026/27 pre-season</h1><p>The in-season Transfer Advisor activates after GW1 begins. Until then, use the separate GW1 Squad Builder to construct and compare opening squads.</p><div class="empty-meta"><span class="empty-chip">GW1 deadline {deadline_text}</span><span class="empty-chip">{len(bootstrap["elements"])} live players</span></div></div>', unsafe_allow_html=True)
    st.stop()

# Picks can legitimately be unavailable before the first deadline / during some
# publication windows. Treat that as a polished empty state, not an app error.
try:
    picks_started = time.perf_counter()
    picks_data = get_picks(team_id_int, squad_event["id"])
    print(f"[FPL Advisor startup] picks_gw{squad_event['id']}={time.perf_counter()-picks_started:.3f}s")
except requests.exceptions.HTTPError as exc:
    print(f"[FPL Advisor startup] picks unavailable gw{squad_event['id']} status={getattr(exc.response, 'status_code', None)}")
    picks_data = None
except requests.exceptions.RequestException as exc:
    print(f"[FPL Advisor startup] picks timeout/failure gw{squad_event['id']}: {type(exc).__name__}: {exc}")
    picks_data = None

if picks_data is None:
    with st.sidebar:
        st.markdown('<div class="rail-brand"><div class="mark">FPL</div><div class="copy"><h2>Transfer Advisor</h2><p>Decision intelligence for your squad</p></div></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="rail-team"><div class="topline"><div class="name">{html.escape(entry.get("name","My Team"))}</div><div class="gw">GW{upcoming_event["id"]}</div></div><div class="sub">Team {team_id_int} · {free_transfers} FT available</div></div>', unsafe_allow_html=True)
        st.markdown('<div class="rail-section-label">Team</div>', unsafe_allow_html=True)
        with st.expander("Switch team / refresh", expanded=False):
            st.text_input("FPL Team ID", key="team_id")
            if st.button("Refresh live data", type="primary", use_container_width=True):
                st.cache_data.clear(); st.rerun()
        st.markdown('<div class="rail-foot">Live FPL data · Understat enrichment · optional football-data.org context</div>', unsafe_allow_html=True)
    st.markdown(f'''    <div class="empty-shell">
      <div class="empty-orb">⚽</div>
      <h1>Your team is connected</h1>
      <p>FPL hasn't published squad picks for the relevant Gameweek yet, so there isn't enough live squad data to build transfer recommendations. The app will become fully active as soon as those picks are available.</p>
      <div class="empty-meta">
        <span class="empty-chip">{html.escape(entry.get("name","My Team"))}</span>
        <span class="empty-chip">GW{upcoming_event["id"]}</span>
        <span class="empty-chip">Deadline {deadline_text}</span>
        <span class="empty-chip">{free_transfers} free transfer{'s' if free_transfers != 1 else ''}</span>
      </div>
      <div class="empty-note"><strong>Nothing is broken.</strong> This normally happens before the season starts, before the first squad is published, or during an FPL data transition. Use <strong>Switch team / refresh</strong> in the sidebar after the next FPL update.</div>
    </div>
    ''', unsafe_allow_html=True)
    st.stop()

# Supplementary sources are best-effort. None of them is allowed to prevent the
# core FPL product from rendering. Each source has its own timeout + fallback.
understat_players = {}
historical_baselines = {"GK": 3.2, "GKP": 3.2, "DEF": 3.6, "MID": 4.1, "FWD": 4.0}
fd_bundle = {"matches": [], "status": "not_configured", "requests_remaining": None}
football_data_key = get_secret("FOOTBALL_DATA_API_KEY")

enrichment_started = time.perf_counter()
try:
    t0 = time.perf_counter(); understat_players = get_understat_players(season_start_year)
    print(f"[FPL Advisor enrichment] understat={time.perf_counter()-t0:.3f}s players={len(understat_players)}")
except Exception as exc:
    print(f"[FPL Advisor enrichment] understat fallback: {type(exc).__name__}: {exc}")

by_full, by_last = build_understat_lookup(understat_players)

try:
    t0 = time.perf_counter(); historical_baselines = get_historical_position_baselines()
    print(f"[FPL Advisor enrichment] historical={time.perf_counter()-t0:.3f}s")
except Exception as exc:
    print(f"[FPL Advisor enrichment] historical fallback: {type(exc).__name__}: {exc}")

if football_data_key:
    try:
        t0 = time.perf_counter(); fd_bundle = get_football_data_bundle(football_data_key, season_start_year)
        print(f"[FPL Advisor enrichment] football-data={time.perf_counter()-t0:.3f}s status={fd_bundle.get('status')}")
    except Exception as exc:
        print(f"[FPL Advisor enrichment] football-data fallback: {type(exc).__name__}: {exc}")
        fd_bundle = {"matches": [], "status": "unavailable", "requests_remaining": None}

fd_matches = fd_bundle.get("matches", [])
team_form = build_team_form(fd_matches)
print(f"[FPL Advisor enrichment] total={time.perf_counter()-enrichment_started:.3f}s")
bank=safe_float(picks_data.get("entry_history",{}).get("bank"),0)/10; squad_value=safe_float(picks_data.get("entry_history",{}).get("value"),0)/10; overall_rank=entry.get("summary_overall_rank")

with st.sidebar:
    st.markdown('<div class="rail-brand"><div class="mark">FPL</div><div class="copy"><h2>Transfer Advisor</h2><p>Decision intelligence for your squad</p></div></div>', unsafe_allow_html=True)
    rank_label=("Rank " + format(overall_rank, ",")) if overall_rank else "Rank —"
    st.markdown(f'<div class="rail-team"><div class="topline"><div class="name">{html.escape(entry.get("name","My Team"))}</div><div class="gw">GW{upcoming_event["id"]}</div></div><div class="sub">{free_transfers} FT · £{bank:.1f}m bank · {rank_label}</div></div>',unsafe_allow_html=True)

    st.markdown('<div class="rail-section-label">Workspace</div>', unsafe_allow_html=True)
    page=st.radio("Navigation",["Home","Transfers","Planner","Squad","Player Lab","Chips","Analytics","Model"],label_visibility="collapsed")

    st.markdown('<div class="rail-section-label">Controls</div>', unsafe_allow_html=True)
    with st.expander("Strategy & filters", expanded=False):
        strategy=st.selectbox("Strategy",["Balanced","Short-term (next 3 GWs)","Long-term hold","Differential","Protect rank"],index=0)
        risk_appetite=st.slider("Risk appetite",1,5,3)
        reserve_bank=st.number_input("Keep in bank (£m)",0.0,10.0,0.0,0.1)
        n_fixtures=st.slider("Fixture horizon",3,8,5)
        min_minutes_security=st.slider("Minimum minutes security",0.0,5.0,2.0,0.5)
        include_doubtful=st.checkbox("Include doubtful targets",False)
        caution_text=st.text_input("Cup/Europe caution clubs",value="",placeholder="ARS,MCI,LIV")
        caution_clubs={x.strip().upper() for x in caution_text.split(",") if x.strip()}

    with st.expander("Team & data", expanded=False):
        st.text_input("FPL Team ID", key="team_id")
        if st.button("Refresh live data", type="primary", use_container_width=True):
            st.cache_data.clear(); st.rerun()
        st.caption("Official FPL · Connected")
        st.caption(f"Understat · {'Connected' if understat_players else 'Optional / unavailable'}")
        fd_status=fd_bundle.get("status")
        if fd_status=="ok":
            remaining=fd_bundle.get("requests_remaining")
            st.caption("football-data.org · Connected")
            if remaining is not None: st.caption(f"API allowance: {remaining} remaining")
            st.caption("6-hour cached snapshot")
        elif fd_status=="rate_limited":
            st.caption("football-data.org · Rate limited")
            st.caption("FPL + Understat fallback active")
        elif fd_status=="auth_error":
            st.caption("football-data.org · Check API token")
        elif fd_status in ("unavailable","invalid_response"):
            st.caption("football-data.org · Temporarily unavailable")
        else:
            st.caption("football-data.org · Optional")
        if st.checkbox("Show startup diagnostics", value=False, key="show_startup_diagnostics"):
            st.caption("Core FPL calls are fetched in parallel and cached for 30 minutes.")
            for name in ("bootstrap", "fixtures", "entry", "history", "total"):
                if name in core_timings:
                    st.caption(f"{name}: {core_timings[name]:.2f}s")

    st.markdown('<div class="rail-foot">Built around live FPL data. Forecasts are decision support, not guarantees.</div>', unsafe_allow_html=True)

squad_rows=[]
for pick in picks_data["picks"]:
    p=players_by_id[pick["element"]]; avg_fdr,fixture_str=team_fixture_difficulty(p["team"],fixtures,upcoming_event["id"],n=n_fixtures); us=match_understat(p,by_full,by_last); comps=player_fpl_components(p,fixtures,teams_by_id,upcoming_event["id"],n_fixtures,us); score=player_score(p,avg_fdr,us,total_players_in_game,strategy,risk_appetite,fixtures,teams_by_id,upcoming_event["id"],n_fixtures); p1,_=projected_horizon(p,upcoming_event["id"],1,fixtures,teams_by_id,us,historical_baselines,team_form); p3,_=projected_horizon(p,upcoming_event["id"],3,fixtures,teams_by_id,us,historical_baselines,team_form); p5,_=projected_horizon(p,upcoming_event["id"],5,fixtures,teams_by_id,us,historical_baselines,team_form); _,_,_,swing_label=fixture_window(p["team"],fixtures,teams_by_id,upcoming_event["id"],p["element_type"]); market,market_pct=market_pressure(p,total_players_in_game); short=teams_by_id[p["team"]]["short_name"]; rot=rotation_risk(p,congestion_score(p["team"],fixtures,upcoming_event["id"],5,caution_clubs,short)); reasons=flag_reasons(p,avg_fdr)
    if rot>=3: reasons.append("⏱ Rotation/congestion risk")
    needs=bool(reasons) or (safe_float(p.get("form"),0)<2.5 and safe_float(p.get("minutes"),0)>0); sell=safe_float(pick.get("selling_price",p.get("now_cost",0)),0)/10
    squad_rows.append({"id":p["id"],"Pos":POSITION_MAP[p["element_type"]],"Player":p["web_name"],"Team":short,"Price":safe_float(p.get("now_cost"),0)/10,"Sell price":sell,"Form":safe_float(p.get("form"),0),"Status":STATUS_LABELS.get(p.get("status"),p.get("status")),"Fixtures":fixture_str,"Fixture score":comps["fixture_score"],"Fixture swing":swing_label,"xGI/90":comps["xgi90"],"Set pieces":", ".join(comps["set_piece_badges"]) or "—","Minutes":minutes_security_score(p),"Bonus":comps["bonus_bps"],"DC":comps["defensive_contribution"],"Captaincy":comps["captaincy"],"Own %":safe_float(p.get("selected_by_percent"),0),"EO proxy":eo_proxy(p,comps),"Market":market,"Market %":market_pct,"Rotation":rot,"Proj GW1":p1,"Proj 3":p3,"Proj 5":p5,"Advisor score":score,"Needs attention":needs,"Flags":" · ".join(reasons) if reasons else "No major issues","News":p.get("news") or "","fixture_details":fixture_details(p["team"],fixtures,upcoming_event["id"],n=n_fixtures)})
squad_df=pd.DataFrame(squad_rows); xi,bench_rows,cap_pair=optimal_starting_xi(squad_rows); flagged=squad_df[squad_df["Needs attention"]].sort_values("Advisor score"); best_cap=cap_pair[0] if cap_pair else None; strongest_swing=max(squad_rows,key=lambda r:r["Fixture score"]) if squad_rows else None; hot_market=max(squad_rows,key=lambda r:r["Market %"]) if squad_rows else None
if len(flagged)==0: action_state,action_title,action_copy="ROLL","Your squad looks stable","No urgent issue stands out. Banking flexibility is a credible move this Gameweek."
elif len(flagged)<=2: action_state,action_title,action_copy="WATCH","There are one or two decisions worth monitoring","Check late team news before using a transfer; don't force a marginal move."
else: action_state,action_title,action_copy="ACT","Your squad has several pressure points","Prioritise the highest-impact issue first, then test whether a two-transfer restructure improves the five-Gameweek outlook."

st.markdown(f'<div class="fpl-hero"><div class="fpl-eyebrow">Gameweek {upcoming_event["id"]} · {deadline_text}</div><h1>{html.escape(entry.get("name","FPL Transfer Advisor"))}</h1><p>Live decision support using FPL data, underlying numbers, projections and squad optimisation.</p></div>',unsafe_allow_html=True)

if page=="Home":
    st.markdown(f'<div class="decision-banner"><span class="tag">{action_state}</span><h2>{action_title}</h2><p>{action_copy}</p></div>',unsafe_allow_html=True)
    c1,c2,c3,c4=st.columns(4)
    metrics=[(c1,"FREE TRANSFERS",str(free_transfers),"Bank up to five"),(c2,"BANK",f"£{bank:.1f}m",f"Squad £{squad_value:.1f}m"),(c3,"OVERALL RANK",f"{overall_rank:,}" if overall_rank else "—","Live FPL rank"),(c4,"DATA",("3 sources" if fd_matches else "2 sources"),("FPL + Understat + team form" if fd_matches else "FPL + Understat"))]
    for col,label,val,sub in metrics:
        with col: st.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{val}</div><div class="metric-sub">{sub}</div></div>',unsafe_allow_html=True)
    st.markdown("### This Gameweek"); a,b,c,d=st.columns(4); cards=[(a,"©","Captain",best_cap["Player"] if best_cap else "—",f"Projected {best_cap['Proj GW1']:.1f} pts" if best_cap else ""),(b,"↗","Best fixture window",strongest_swing["Player"] if strongest_swing else "—",strongest_swing["Fixture swing"] if strongest_swing else ""),(c,"🔥","Market heat",hot_market["Player"] if hot_market else "—",hot_market["Market"] if hot_market else ""),(d,"🛡","Secure minutes",f"{sum(r['Minutes']>=4 for r in squad_rows)}/15","Players ≥4/5")]
    for col,icon,title,big,small in cards:
        with col: st.markdown(f'<div class="signal-card"><div class="icon">{icon}</div><div class="title">{title}</div><div class="big">{html.escape(str(big))}</div><div class="small">{html.escape(str(small))}</div></div>',unsafe_allow_html=True)
    st.markdown("### Projection leaders"); st.dataframe(squad_df.sort_values("Proj 5",ascending=False).head(6)[["Player","Pos","Team","Proj GW1","Proj 3","Proj 5","Captaincy","Fixture swing"]],use_container_width=True,hide_index=True)
    if not football_data_key: st.info("Optional: add a free football-data.org token as `FOOTBALL_DATA_API_KEY` in Streamlit Secrets for recent team-form and home/away enrichment.")

elif page=="Transfers":
    st.markdown("## Transfer Lab"); st.caption("One-transfer recommendations first; then a two-transfer budget restructure.")
    outgoing_name=st.selectbox("Player to review",squad_df.sort_values(["Needs attention","Advisor score"],ascending=[False,True])["Player"].tolist()); outgoing=squad_df[squad_df["Player"]==outgoing_name].iloc[0]
    candidates=build_replacement_candidates(outgoing,bootstrap,squad_df,players_by_id,teams_by_id,fixtures,upcoming_event["id"],by_full,by_last,total_players_in_game,strategy,risk_appetite,n_fixtures,reserve_bank,bank,min_minutes_security,include_doubtful,historical_baselines,team_form,15)
    if candidates:
        best=candidates[0]; gain=best["Proj 5"]-outgoing["Proj 5"]
        st.markdown(f'<div class="decision-banner"><span class="tag">BEST 1-TRANSFER MOVE</span><h2>{html.escape(outgoing_name)} → {html.escape(best["Player"])}</h2><p>Projected five-Gameweek gain <b>{gain:+.1f} pts</b> · advisor upgrade {best["Upgrade"]:+.2f}.</p></div>',unsafe_allow_html=True)
        cols=st.columns(min(3,len(candidates)))
        for i,cand in enumerate(candidates[:3]):
            with cols[i]:
                reasons=" · ".join(cand["Reasons"][:3]) or "Balanced profile"; st.markdown(f'<div class="transfer-card {"best" if i==0 else ""}"><div class="fpl-eyebrow">#{i+1} target</div><h3>{html.escape(cand["Player"])}</h3><div><span class="pill">{cand["Team"]}</span><span class="pill">£{cand["Cost"]:.1f}m</span><span class="pill pill-cyan">5GW {cand["Proj 5"]:.1f}</span></div>{fixture_chips_html(cand["fixture_details"],5)}<p><span class="proj-badge">GW {cand["Proj GW1"]:.1f}</span><span class="proj-badge">3GW {cand["Proj 3"]:.1f}</span></p><p style="font-size:.8rem;color:#655d69">{html.escape(reasons)}</p></div>',unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(candidates)[["Player","Team","Cost","Proj GW1","Proj 3","Proj 5","xGI/90","Minutes","Captaincy","Own %","Market","Upgrade"]],use_container_width=True,hide_index=True)
    else: st.warning("No legal replacement fits the current filters and budget.")
    st.markdown("### Two-transfer squad optimiser"); st.caption("Includes enabler moves and compares the combined five-Gameweek projection.")
    plans=[]; pool=squad_df.sort_values(["Needs attention","Proj 5"],ascending=[False,True]).head(6)
    # Fast two-move search: evaluate top candidates for two weaker squad slots and shared budget.
    pool_rows=list(pool.iterrows())
    for i,(_,r1) in enumerate(pool_rows):
        for _,r2 in pool_rows[i+1:]:
            funds=r1["Sell price"]+r2["Sell price"]+max(0,bank-reserve_bank)
            # Let either incoming player use the combined two-sale budget. This is what enables
            # a cheap downgrade in one slot to unlock a premium in the other.
            search_bank_1=max(0.0,funds-r1["Sell price"]+reserve_bank)
            search_bank_2=max(0.0,funds-r2["Sell price"]+reserve_bank)
            c1=build_replacement_candidates(r1,bootstrap,squad_df,players_by_id,teams_by_id,fixtures,upcoming_event["id"],by_full,by_last,total_players_in_game,strategy,risk_appetite,n_fixtures,reserve_bank,search_bank_1,min_minutes_security,include_doubtful,historical_baselines,team_form,7)
            c2=build_replacement_candidates(r2,bootstrap,squad_df,players_by_id,teams_by_id,fixtures,upcoming_event["id"],by_full,by_last,total_players_in_game,strategy,risk_appetite,n_fixtures,reserve_bank,search_bank_2,min_minutes_security,include_doubtful,historical_baselines,team_form,7)
            for x in c1:
                for y in c2:
                    if x["id"]==y["id"] or x["Cost"]+y["Cost"]>funds+.001: continue
                    counts=squad_df["Team"].value_counts().to_dict(); counts[r1["Team"]]-=1; counts[r2["Team"]]-=1; counts[x["Team"]]=counts.get(x["Team"],0)+1; counts[y["Team"]]=counts.get(y["Team"],0)+1
                    if max(counts.values())>3: continue
                    gain=(x["Proj 5"]+y["Proj 5"])-(r1["Proj 5"]+r2["Proj 5"]); plans.append({"Out 1":r1["Player"],"In 1":x["Player"],"Out 2":r2["Player"],"In 2":y["Player"],"5GW gain":round(gain,1),"Bank after":round(funds-x["Cost"]-y["Cost"]+reserve_bank,1),"Transfer cost":"0" if free_transfers>=2 else "-4"})
    if plans: st.dataframe(pd.DataFrame(plans).sort_values("5GW gain",ascending=False).drop_duplicates().head(10),use_container_width=True,hide_index=True)
    else: st.info("No compelling legal two-transfer restructure found with the current filters.")

elif page=="Planner":
    st.markdown("## Gameweek & 5-GW Planner"); st.markdown("### Best XI"); st.dataframe(pd.DataFrame(xi)[["Pos","Player","Team","Proj GW1","Captaincy","Fixtures","Status"]],use_container_width=True,hide_index=True)
    if cap_pair: st.success(f"Captain: **{cap_pair[0]['Player']}** · Vice: **{cap_pair[1]['Player'] if len(cap_pair)>1 else '—'}**")
    st.markdown("### Bench order"); st.write(" → ".join(f"{r['Player']} ({r['Proj GW1']:.1f})" for r in bench_rows))
    roadmap=[]; sim_ft=free_transfers
    for ev in range(upcoming_event["id"],min(39,upcoming_event["id"]+5)):
        vals=[]
        for r in squad_rows:
            p=players_by_id[r["id"]]; us=match_understat(p,by_full,by_last); vals.append((event_projection(p,ev,fixtures,teams_by_id,us,historical_baselines,team_form),r))
        weak=min(vals,key=lambda x:x[0]) if vals else (0,None); action="REVIEW" if weak[1] and weak[0]<2.2 else "ROLL"; focus=f"{weak[1]['Player']} projects {weak[0]:.1f}" if action=="REVIEW" else "Bank flexibility"; roadmap.append({"GW":ev,"FT entering":sim_ft,"Action":action,"Focus":focus}); sim_ft=min(5,sim_ft+1) if action=="ROLL" else sim_ft
    st.markdown("### 3–5 Gameweek roadmap"); st.dataframe(pd.DataFrame(roadmap),use_container_width=True,hide_index=True); st.caption("Roadmap freezes today's prices and player information; rerun after each deadline.")

elif page=="Squad":
    st.markdown("## Squad intelligence"); cols=["Pos","Player","Team","Price","Sell price","Form","Status","Proj GW1","Proj 3","Proj 5","xGI/90","Fixture score","Fixture swing","Minutes","Rotation","Bonus","DC","Captaincy","Own %","EO proxy","Market","Advisor score","Flags"]; st.dataframe(squad_df[cols],use_container_width=True,hide_index=True)

elif page=="Player Lab":
    st.markdown("## Player Lab"); st.caption("Search, compare and run squad scenarios."); all_names=sorted([p["web_name"] for p in bootstrap["elements"]]); selected=st.selectbox("Search player",all_names); target=next(p for p in bootstrap["elements"] if p["web_name"]==selected); us=match_understat(target,by_full,by_last); comps=player_fpl_components(target,fixtures,teams_by_id,upcoming_event["id"],n_fixtures,us); p1,_=projected_horizon(target,upcoming_event["id"],1,fixtures,teams_by_id,us,historical_baselines,team_form); p3,_=projected_horizon(target,upcoming_event["id"],3,fixtures,teams_by_id,us,historical_baselines,team_form); p5,_=projected_horizon(target,upcoming_event["id"],5,fixtures,teams_by_id,us,historical_baselines,team_form)
    a,b,c,d=st.columns(4)
    for col,label,val in [(a,"GW projection",p1),(b,"Next 3",p3),(c,"Next 5",p5),(d,"xGI/90",f"{comps['xgi90']:.2f}")]:
        with col: st.metric(label,val)
    compare=st.selectbox("Compare with your player",squad_df["Player"].tolist()); cr=squad_df[squad_df["Player"]==compare].iloc[0]; comp=pd.DataFrame({"Metric":["Price","GW projection","Next 3","Next 5","xGI/90","Minutes","Captaincy","Ownership %"],compare:[cr["Price"],cr["Proj GW1"],cr["Proj 3"],cr["Proj 5"],cr["xGI/90"],cr["Minutes"],cr["Captaincy"],cr["Own %"]],selected:[safe_float(target.get("now_cost"),0)/10,p1,p3,p5,comps["xgi90"],minutes_security_score(target),comps["captaincy"],safe_float(target.get("selected_by_percent"),0)]}); st.dataframe(comp,use_container_width=True,hide_index=True)
    st.markdown("### Scenario mode"); scenario=st.radio("Scenario",["What if I sell…","How do I get…"],horizontal=True,label_visibility="collapsed")
    if scenario=="What if I sell…":
        who=st.selectbox("Sell",squad_df["Player"].tolist(),key="sellscenario"); rr=squad_df[squad_df["Player"]==who].iloc[0]; cc=build_replacement_candidates(rr,bootstrap,squad_df,players_by_id,teams_by_id,fixtures,upcoming_event["id"],by_full,by_last,total_players_in_game,strategy,risk_appetite,n_fixtures,reserve_bank,bank,min_minutes_security,include_doubtful,historical_baselines,team_form,8)
        if cc: st.dataframe(pd.DataFrame(cc)[["Player","Team","Cost","Proj 3","Proj 5","Upgrade"]],use_container_width=True,hide_index=True)
    else:
        wanted=st.selectbox("Target player",all_names,key="wanted"); wp=next(p for p in bootstrap["elements"] if p["web_name"]==wanted); needed=safe_float(wp.get("now_cost"),0)/10; compatible=squad_df[squad_df["Pos"]==POSITION_MAP[wp["element_type"]]].copy(); compatible["Funding gap"]=needed-(compatible["Sell price"]+bank-reserve_bank); st.write(f"Target price: **£{needed:.1f}m**"); st.dataframe(compatible[["Player","Sell price","Proj 5","Funding gap"]].sort_values("Funding gap"),use_container_width=True,hide_index=True); st.caption("Positive funding gap means an enabling move is needed elsewhere; use the two-transfer optimiser.")

elif page=="Chips":
    st.markdown("## Chip Centre"); chips=chip_recommendations(squad_rows,upcoming_event["id"],free_transfers); cols=st.columns(2)
    for i,(chip,(status,why)) in enumerate(chips.items()):
        with cols[i%2]: st.markdown(f'<div class="transfer-card"><div class="fpl-eyebrow">{chip}</div><h3>{status}</h3><span class="pill {"pill-green" if status=="CONSIDER" else "pill-warn" if status=="WATCH" else ""}">{status}</span><p style="font-size:.84rem;color:#665e69">{html.escape(why)}</p></div>',unsafe_allow_html=True)
    st.info("Chip suggestions are heuristics. Confirm current rules and upcoming blanks/doubles before activation.")

elif page=="Analytics":
    st.markdown("## Analytics"); st.markdown("### Fixture swings"); st.dataframe(squad_df[["Player","Pos","Team","Fixture score","Fixture swing","Proj 3","Proj 5"]].sort_values("Fixture score",ascending=False),use_container_width=True,hide_index=True); st.markdown("### Market & rank-risk"); st.dataframe(squad_df[["Player","Own %","EO proxy","Market","Market %","Captaincy","Rotation","News"]].sort_values("Market %",ascending=False),use_container_width=True,hide_index=True)
    if team_form:
        rows=[]
        for tla,data in team_form.items():
            if tla in {t["short_name"] for t in teams_by_id.values()}: rows.append({"Team":tla,"PPG last 5":data["overall"]["ppg"],"GF/game":data["overall"]["gf"],"GA/game":data["overall"]["ga"],"Home GF":data["home"]["gf"],"Away GF":data["away"]["gf"]})
        st.markdown("### Recent team form"); st.dataframe(pd.DataFrame(rows).sort_values("PPG last 5",ascending=False),use_container_width=True,hide_index=True)
    else:
        fd_status=fd_bundle.get("status")
        if fd_status=="rate_limited":
            st.info("Recent team-form enrichment is temporarily paused because football-data.org returned a rate-limit response. The app is using FPL + Understat and will not automatically retry in a loop.")
        elif fd_status=="auth_error":
            st.info("Recent team-form enrichment is off because football-data.org rejected the API token. Check `FOOTBALL_DATA_API_KEY` in Streamlit Secrets.")
        elif fd_status in ("unavailable","invalid_response"):
            st.info("Recent team-form enrichment is temporarily unavailable. The app is continuing with FPL + Understat.")
        else:
            st.info("Recent team-form enrichment is off. Add `FOOTBALL_DATA_API_KEY` in Streamlit Secrets to enable it.")

elif page=="Model":
    st.markdown("## How the model thinks")
    st.markdown("""The app separates **Advisor score** from **Projected FPL points**.

**Advisor score** is strategy-aware and uses form, expected points, position-adjusted fixtures, xGI/90, availability, minutes, value, set pieces, bonus/BPS, defensive upside, defensive contributions, ownership and market momentum.

**Projected FPL points** are a separate live ensemble using realised FPL output, xGI/90, expected minutes, fixture difficulty, position, set pieces, bonus and defensive signals. A small historical position prior comes from public realised Gameweek data. Optional football-data.org enrichment adds recent home/away team form.

This makes hit decisions more intuitive: compare the **projected 3/5-GW gain** with the four-point transfer cost rather than subtracting four from an abstract advisor score.

Projections remain estimates, not guarantees. Late injuries, tactical changes, prices and lineups can change after the model runs.""")
    st.markdown("### Data architecture"); st.markdown("- **Official FPL API:** primary live source.\n- **Understat:** supplementary xG/xA context.\n- **football-data.org:** optional results/home-away form API.\n- **Vaastav historical dataset:** realised-points calibration only; same-GW `xP` is excluded because its repository documents possible look-ahead bias."); st.markdown("### Historical projection priors"); st.json(historical_baselines)

st.divider(); st.caption("FPL Transfer Advisor · Independent decision-support tool using public football data. Not affiliated with or endorsed by the Premier League.")
