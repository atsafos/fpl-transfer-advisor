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

import requests
import pandas as pd
import streamlit as st
from datetime import datetime

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = "https://fantasy.premierleague.com/api"
CACHE_TTL = 1800          # 30 minutes for FPL data - keeps within FPL's rate limits
UNDERSTAT_TTL = 6 * 3600  # 6 hours - season xG totals don't need refreshing often

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
</style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# DATA FETCHING (cached so we don't hammer the FPL API)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=CACHE_TTL)
def get_bootstrap():
    r = requests.get(f"{BASE}/bootstrap-static/", timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_fixtures():
    r = requests.get(f"{BASE}/fixtures/", timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_entry(team_id):
    r = requests.get(f"{BASE}/entry/{team_id}/", timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_picks(team_id, event):
    r = requests.get(f"{BASE}/entry/{team_id}/event/{event}/picks/", timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=CACHE_TTL)
def get_history(team_id):
    r = requests.get(f"{BASE}/entry/{team_id}/history/", timeout=15)
    r.raise_for_status()
    return r.json()


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
        r = requests.get(url, headers=headers, timeout=15)
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
# UI
# ----------------------------------------------------------------------------

with st.sidebar:
    st.markdown("## ⚽ FPL Advisor")
    st.caption("Transfer intelligence built around your squad")
    st.divider()
    team_id = st.text_input(
        "FPL Team ID",
        value=st.session_state.get("team_id", "4935366"),
        help="Found in the URL when viewing your FPL Points page, e.g. /entry/1234567/event/5.",
    )
    run = st.button("Refresh squad & data", type="primary", use_container_width=True)
    if run:
        st.cache_data.clear()
        st.rerun()

    st.markdown("### Transfer brief")
    strategy = st.selectbox(
        "Strategy",
        ["Balanced", "Short-term (next 3 GWs)", "Long-term hold", "Differential", "Protect rank"],
        index=0,
        help="Changes the weights used to rank transfer targets.",
    )
    risk_appetite = st.slider(
        "Risk appetite", 1, 5, 3,
        help="1 favours safety, minutes and availability. 5 gives more weight to upside and underlying data.",
    )
    reserve_bank = st.number_input(
        "Keep in bank (£m)", min_value=0.0, max_value=10.0, value=0.0, step=0.1,
        help="Replacement suggestions will preserve at least this much cash.",
    )
    n_fixtures = st.slider("Fixture horizon", 3, 8, 5)

    with st.expander("Advanced filters"):
        form_threshold = st.slider("Poor form threshold", 0.0, 5.0, 2.5, 0.5)
        fdr_threshold = st.slider("Tough fixture threshold", 2.0, 5.0, 3.8, 0.1)
        min_minutes_security = st.slider(
            "Minimum minutes security", 0.0, 5.0, 2.0, 0.5,
            help="Filters out candidates with weak playing-time security. Early-season players are treated neutrally.",
        )
        include_doubtful = st.checkbox("Include doubtful candidates", value=False)
        shortlist_size = st.slider("Shortlist size", 3, 8, 5)

    st.divider()
    st.caption("Official FPL data refreshes every 30 minutes. Understat is supplementary and may be unavailable.")

if not team_id:
    st.info("Enter your FPL Team ID in the sidebar to begin.")
    st.stop()

try:
    team_id_int = int(team_id)
except ValueError:
    st.error("Team ID should contain numbers only.")
    st.stop()

with st.spinner("Loading your FPL squad and current market data..."):
    try:
        bootstrap = get_bootstrap()
        fixtures = get_fixtures()
        entry = get_entry(team_id_int)
        history = get_history(team_id_int)
    except requests.exceptions.HTTPError:
        st.error("I couldn't find that FPL Team ID. Double-check it and try again.")
        st.stop()
    except requests.exceptions.RequestException:
        st.error("The FPL API could not be reached right now. Try refreshing shortly.")
        st.stop()

teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
st.session_state["teams_by_id"] = teams_by_id
players_by_id = {p["id"]: p for p in bootstrap["elements"]}
events = bootstrap["events"]
total_players_in_game = bootstrap.get("total_players", 1)

squad_event = current_or_last_event(events)
upcoming_event = next_event(events)

season_start_year = squad_event["deadline_time"][:4]
if int(squad_event["deadline_time"][5:7]) < 7:
    season_start_year = str(int(season_start_year) - 1)

with st.spinner("Adding underlying xG/xA context..."):
    understat_players = get_understat_players(season_start_year)
by_full, by_last = build_understat_lookup(understat_players)

free_transfers = free_transfers_available(history, events)

try:
    picks_data = get_picks(team_id_int, squad_event["id"])
except requests.exceptions.HTTPError:
    st.error("Your squad is not available for this Gameweek yet. The season may not have started or picks may not be published.")
    st.stop()

bank = safe_float(picks_data.get("entry_history", {}).get("bank"), 0) / 10
squad_value = safe_float(picks_data.get("entry_history", {}).get("value"), 0) / 10
overall_rank = entry.get("summary_overall_rank")
deadline_dt = datetime.fromisoformat(upcoming_event["deadline_time"].replace("Z", "+00:00"))
deadline_text = deadline_dt.strftime("%a %d %b · %H:%M UTC")

# Build squad intelligence once and reuse it across views.
squad_rows = []
for pick in picks_data["picks"]:
    p = players_by_id[pick["element"]]
    avg_fdr, fixture_str = team_fixture_difficulty(p["team"], fixtures, upcoming_event["id"], n=n_fixtures)
    details = fixture_details(p["team"], fixtures, upcoming_event["id"], n=n_fixtures)
    current_understat = match_understat(p, by_full, by_last)
    comps = player_fpl_components(p, fixtures, teams_by_id, upcoming_event["id"], n_fixtures, current_understat)
    current_score = player_score(
        p, avg_fdr, current_understat, total_players_in_game, strategy, risk_appetite,
        fixtures, teams_by_id, upcoming_event["id"], n_fixtures,
    )
    reasons = flag_reasons({**p, "form": p.get("form")}, avg_fdr)
    mins_security = minutes_security_score(p)
    if safe_float(p.get("minutes"), 0) > 0 and mins_security < 2.4:
        reasons.append("⏱️ Minutes/rotation risk")
    if comps["fixture_score"] < 2.3 and not any("Tough run" in r for r in reasons):
        reasons.append("🧱 Position-adjusted fixtures look difficult")
    has_played = safe_float(p.get("minutes"), 0) > 0 or safe_float(p.get("points_per_game"), 0) > 0
    poor_form_flag = has_played and safe_float(p.get("form"), 0) < form_threshold
    needs_attention = (
        bool(reasons)
        or poor_form_flag
        or (avg_fdr is not None and avg_fdr >= fdr_threshold)
    )
    _, momentum_label = ownership_momentum(p, total_players_in_game)
    selling_price = safe_float(pick.get("selling_price", p.get("now_cost", 0)), 0) / 10
    squad_rows.append({
        "id": p["id"],
        "Pos": POSITION_MAP[p["element_type"]],
        "Player": p["web_name"],
        "Team": teams_by_id[p["team"]]["short_name"],
        "Price": safe_float(p.get("now_cost"), 0) / 10,
        "Sell price": selling_price,
        "Form": safe_float(p.get("form"), 0),
        "PPG": safe_float(p.get("points_per_game"), 0),
        "xGI/90": comps["xgi90"],
        "Fixture score": comps["fixture_score"],
        "Set pieces": ", ".join(comps["set_piece_badges"]) if comps["set_piece_badges"] else "—",
        "Minutes": round(mins_security, 1),
        "Own %": safe_float(p.get("selected_by_percent"), 0),
        "Trend": momentum_label,
        "Advisor": current_score,
        "Status": STATUS_LABELS.get(p.get("status"), p.get("status")),
        "Fixtures": fixture_str,
        "Avg FDR": round(avg_fdr, 1) if avg_fdr is not None else None,
        "Flags": " | ".join(reasons) if reasons else "✅ No material issues",
        "Needs attention": needs_attention,
        "captain": pick.get("is_captain", False),
        "vice": pick.get("is_vice_captain", False),
        "fixture_details": details,
        "components": comps,
    })

squad_df = pd.DataFrame(squad_rows)
flagged = squad_df[squad_df["Needs attention"]].copy()
if not flagged.empty:
    flagged["severity"] = flagged["Flags"].apply(
        lambda f: f.count("⛔") * 4 + f.count("⚠️") * 3 + f.count("⏱️") * 2 + f.count("📉") + f.count("🗓️") + f.count("🧱")
    )
    flagged = flagged.sort_values(["severity", "Advisor"], ascending=[False, True])

# Hero
st.markdown(
    f"""<div class="fpl-hero">
        <div class="fpl-eyebrow">Gameweek {upcoming_event['id']} transfer dashboard</div>
        <h1>{html.escape(entry.get('name', 'Your FPL Team'))}</h1>
        <p>{html.escape(entry.get('player_first_name', ''))} {html.escape(entry.get('player_last_name', ''))} · Deadline {deadline_text} · Strategy: {html.escape(strategy)}</p>
    </div>""",
    unsafe_allow_html=True,
)

m1, m2, m3, m4, m5 = st.columns(5)
metrics = [
    (m1, "Overall rank", f"{overall_rank:,}" if overall_rank else "—", "Current overall position"),
    (m2, "Squad value", f"£{squad_value:.1f}m", "Current squad value"),
    (m3, "Bank", f"£{bank:.1f}m", f"£{max(0, bank-reserve_bank):.1f}m spendable"),
    (m4, "Free transfers", str(free_transfers), "Estimated from transfer history"),
    (m5, "Review", str(len(flagged)), "Players currently flagged"),
]
for col, label, value, sub in metrics:
    with col:
        st.markdown(
            f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div><div class="metric-sub">{sub}</div></div>',
            unsafe_allow_html=True,
        )

if not understat_players:
    st.caption("ℹ️ Understat is unavailable right now, so xGI uses official FPL data where available.")

overview_tab, squad_tab, transfer_tab, model_tab = st.tabs(["Overview", "Squad", "Transfer Lab", "How the model thinks"])

with overview_tab:
    st.markdown("### Manager snapshot")
    left, right = st.columns([1.25, 1])
    with left:
        if flagged.empty:
            st.success("Your squad has no strong transfer red flags under the current settings. Rolling a free transfer may be valuable.")
        else:
            st.markdown('<div class="section-kicker">Priority review</div>', unsafe_allow_html=True)
            for rank, (_, row) in enumerate(flagged.head(4).iterrows(), start=1):
                badge = "© " if row["captain"] else "Ⓥ " if row["vice"] else ""
                st.markdown(f"**{rank}. {badge}{row['Player']} · {row['Pos']} · {row['Team']}**")
                st.caption(row["Flags"])
                st.markdown(fixture_chips_html(row["fixture_details"], max_items=5), unsafe_allow_html=True)
    with right:
        st.markdown('<div class="section-kicker">Your brief</div>', unsafe_allow_html=True)
        st.markdown(f"**Strategy:** {strategy}")
        st.markdown(f"**Risk:** {risk_appetite}/5")
        st.markdown(f"**Fixture horizon:** {n_fixtures} GWs")
        st.markdown(f"**Cash reserve:** £{reserve_bank:.1f}m")
        if free_transfers > 1:
            st.info(f"You have {free_transfers} estimated free transfers, so the model ranks multiple issues but still avoids recommending marginal moves simply because a transfer is available.")
        else:
            st.info("With one estimated free transfer, upgrade size matters: small score gains are treated as potentially sideways moves.")

    st.markdown("### FPL signals in your current squad")
    a, b, c = st.columns(3)
    with a:
        best_xgi = squad_df.sort_values("xGI/90", ascending=False).iloc[0]
        st.metric("Best xGI/90", best_xgi["Player"], f"{best_xgi['xGI/90']:.2f}")
    with b:
        best_fixture = squad_df.sort_values("Fixture score", ascending=False).iloc[0]
        st.metric("Best fixture run", best_fixture["Player"], f"{best_fixture['Fixture score']:.1f}/5")
    with c:
        set_piece_players = squad_df[squad_df["Set pieces"] != "—"]
        st.metric("Set-piece assets", len(set_piece_players), "in current squad")

with squad_tab:
    st.markdown("### Your 15-player squad")
    st.caption("Advisor is the model score under your selected strategy. Fixture score is position-adjusted: attackers care more about opponent defence; GKP/DEF care more about opponent attack.")
    display_cols = ["Pos", "Player", "Team", "Price", "Sell price", "Form", "PPG", "xGI/90", "Fixture score", "Set pieces", "Minutes", "Own %", "Advisor", "Status", "Fixtures", "Flags"]
    display_df = squad_df[display_cols].copy()
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Price": st.column_config.NumberColumn("Price", format="£%.1fm"),
            "Sell price": st.column_config.NumberColumn("Sell", format="£%.1fm"),
            "xGI/90": st.column_config.NumberColumn("xGI/90", format="%.2f"),
            "Fixture score": st.column_config.ProgressColumn("Fixtures", min_value=0, max_value=5, format="%.1f"),
            "Minutes": st.column_config.ProgressColumn("Minutes", min_value=0, max_value=5, format="%.1f"),
            "Own %": st.column_config.NumberColumn("Own %", format="%.1f%%"),
            "Advisor": st.column_config.NumberColumn("Advisor", format="%.2f"),
        },
    )

with transfer_tab:
    st.markdown("### Transfer Lab")
    st.caption("Candidates must be affordable, same-position, available under your filters and legal under the three-players-per-club rule. The model no longer relaxes that rule just to pad the shortlist.")

    if flagged.empty:
        st.success("No player is strongly flagged. The model currently sees more value in holding than forcing a transfer.")
    else:
        squad_ids = set(squad_df["id"])
        squad_team_counts = squad_df["Team"].value_counts().to_dict()

        for rank, (_, row) in enumerate(flagged.iterrows(), start=1):
            outgoing = players_by_id[row["id"]]
            within_free = rank <= free_transfers
            transfer_cost_label = "Free-transfer slot" if within_free else "Would normally cost -4"

            with st.expander(
                f"#{rank}  {row['Player']} · {row['Pos']} · {row['Team']} · £{row['Sell price']:.1f}m sell value — {transfer_cost_label}",
                expanded=(rank == 1),
            ):
                top_l, top_r = st.columns([1.4, 1])
                with top_l:
                    st.markdown(f"**Why review:** {row['Flags']}")
                    st.markdown(fixture_chips_html(row["fixture_details"], max_items=6), unsafe_allow_html=True)
                with top_r:
                    st.markdown(f"**Current advisor score:** {row['Advisor']:.2f}")
                    st.markdown(f"**xGI/90:** {row['xGI/90']:.2f}")
                    st.markdown(f"**Minutes security:** {row['Minutes']:.1f}/5")

                spendable_bank = max(0.0, bank - reserve_bank)
                budget = row["Sell price"] + spendable_bank
                outgoing_score = float(row["Advisor"])
                candidates = []

                for cand in bootstrap["elements"]:
                    if cand["id"] in squad_ids:
                        continue
                    if cand["element_type"] != outgoing["element_type"]:
                        continue
                    cost = safe_float(cand.get("now_cost"), 0) / 10
                    if cost > budget + 0.001:
                        continue
                    cand_team_short = teams_by_id[cand["team"]]["short_name"]
                    current_count = squad_team_counts.get(cand_team_short, 0)
                    effective_count = current_count - (1 if cand_team_short == row["Team"] else 0)
                    if effective_count >= MAX_PER_TEAM:
                        continue
                    if cand.get("status") in ("i", "s", "u", "n"):
                        continue
                    if cand.get("status") == "d" and not include_doubtful:
                        continue
                    mins_security = minutes_security_score(cand)
                    if mins_security < min_minutes_security:
                        continue

                    c_avg_fdr, c_fixture_str = team_fixture_difficulty(cand["team"], fixtures, upcoming_event["id"], n=n_fixtures)
                    c_understat = match_understat(cand, by_full, by_last)
                    comps = player_fpl_components(cand, fixtures, teams_by_id, upcoming_event["id"], n_fixtures, c_understat)
                    score = player_score(
                        cand, c_avg_fdr, c_understat, total_players_in_game, strategy, risk_appetite,
                        fixtures, teams_by_id, upcoming_event["id"], n_fixtures,
                    )
                    upgrade = round(score - outgoing_score, 2)
                    _, trend = ownership_momentum(cand, total_players_in_game)
                    reasons_buy = recommendation_reasons(cand, comps)
                    watchouts = []
                    if mins_security < 3.2:
                        watchouts.append("rotation/minutes risk")
                    if cand.get("status") == "d":
                        watchouts.append("currently doubtful")
                    if strategy == "Differential" and safe_float(cand.get("selected_by_percent"), 0) > 20:
                        watchouts.append("high ownership for a differential strategy")
                    if comps["fixture_score"] < 2.6:
                        watchouts.append("difficult fixture run")

                    candidates.append({
                        "id": cand["id"],
                        "Player": cand["web_name"],
                        "Team": cand_team_short,
                        "Cost": cost,
                        "Form": safe_float(cand.get("form"), 0),
                        "EP next": safe_float(cand.get("ep_next"), 0),
                        "xGI/90": comps["xgi90"],
                        "xGI source": comps["xgi_source"],
                        "Fixtures": c_fixture_str,
                        "Fixture score": comps["fixture_score"],
                        "Set pieces": ", ".join(comps["set_piece_badges"]) if comps["set_piece_badges"] else "—",
                        "Minutes": mins_security,
                        "Def upside": comps["defensive_upside"],
                        "DC signal": comps["defensive_contribution"],
                        "Bonus/BPS": comps["bonus_bps"],
                        "Captaincy": comps["captaincy"],
                        "Own %": safe_float(cand.get("selected_by_percent"), 0),
                        "Trend": trend,
                        "Score": score,
                        "Upgrade": upgrade,
                        "Confidence": confidence_label(upgrade, cand),
                        "Reasons": reasons_buy,
                        "Watchouts": watchouts,
                        "fixture_details": fixture_details(cand["team"], fixtures, upcoming_event["id"], n=n_fixtures),
                    })

                if not candidates:
                    st.warning(f"No legal candidates fit the £{budget:.1f}m budget and your current filters. Try lowering the minutes-security filter or reducing the cash reserve.")
                    continue

                cand_df = pd.DataFrame(candidates).sort_values(["Upgrade", "Score"], ascending=False).head(shortlist_size)
                best = cand_df.iloc[0]

                if best["Upgrade"] >= 0.45:
                    st.markdown(f"#### Recommended: {row['Player']} → {best['Player']}")
                    st.success(f"Model upgrade {best['Upgrade']:+.2f} · {best['Confidence']} confidence · leaves £{bank + row['Sell price'] - best['Cost']:.1f}m in the bank")
                elif best["Upgrade"] >= 0.15:
                    st.info(f"Best move is only {best['Upgrade']:+.2f} better on the model. This is a borderline/sideways transfer; team news or a specific tactical plan should justify it.")
                else:
                    st.info("No candidate offers a meaningful model upgrade. Rolling the transfer currently looks stronger than forcing a move.")

                cards = st.columns(min(3, len(cand_df)))
                for i, (_, cand_row) in enumerate(cand_df.head(3).iterrows()):
                    with cards[i]:
                        confidence_class = "confidence-high" if cand_row["Confidence"] == "HIGH" else "confidence-medium" if cand_row["Confidence"] == "MEDIUM" else "confidence-low"
                        reason_text = " · ".join(cand_row["Reasons"][:3]) if cand_row["Reasons"] else "Balanced statistical profile"
                        watch_text = " · ".join(cand_row["Watchouts"][:2]) if cand_row["Watchouts"] else "No major model watch-outs"
                        set_piece_html = f'<span class="pill pill-green">{html.escape(cand_row["Set pieces"])}</span>' if cand_row["Set pieces"] != "—" else ""
                        st.markdown(
                            f"""<div class="transfer-card {'best' if i == 0 else ''}">
                                <div class="fpl-eyebrow">#{i+1} target</div>
                                <h3>{html.escape(cand_row['Player'])}</h3>
                                <div><span class="pill">{html.escape(cand_row['Team'])}</span><span class="pill">£{cand_row['Cost']:.1f}m</span><span class="pill pill-cyan">{cand_row['Upgrade']:+.2f}</span>{set_piece_html}</div>
                                {fixture_chips_html(cand_row['fixture_details'], max_items=5)}
                                <p><b>Score {cand_row['Score']:.2f}</b> · <span class="{confidence_class}">{cand_row['Confidence']}</span></p>
                                <p style="font-size:.82rem;color:#625b66"><b>Why:</b> {html.escape(reason_text)}</p>
                                <p style="font-size:.78rem;color:#8a6870"><b>Watch:</b> {html.escape(watch_text)}</p>
                            </div>""",
                            unsafe_allow_html=True,
                        )

                st.markdown("**Detailed shortlist**")
                table_cols = ["Player", "Team", "Cost", "Form", "EP next", "xGI/90", "Fixture score", "Set pieces", "Minutes", "Def upside", "DC signal", "Bonus/BPS", "Captaincy", "Own %", "Score", "Upgrade", "Confidence"]
                st.dataframe(
                    cand_df[table_cols],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Cost": st.column_config.NumberColumn("Cost", format="£%.1fm"),
                        "xGI/90": st.column_config.NumberColumn("xGI/90", format="%.2f"),
                        "Fixture score": st.column_config.ProgressColumn("Fixtures", min_value=0, max_value=5, format="%.1f"),
                        "Minutes": st.column_config.ProgressColumn("Minutes", min_value=0, max_value=5, format="%.1f"),
                        "Def upside": st.column_config.ProgressColumn("Def", min_value=0, max_value=5, format="%.1f"),
                        "DC signal": st.column_config.ProgressColumn("DC", min_value=0, max_value=5, format="%.1f"),
                        "Bonus/BPS": st.column_config.ProgressColumn("Bonus", min_value=0, max_value=5, format="%.1f"),
                        "Captaincy": st.column_config.ProgressColumn("Captain", min_value=0, max_value=5, format="%.1f"),
                        "Own %": st.column_config.NumberColumn("Own %", format="%.1f%%"),
                        "Score": st.column_config.NumberColumn("Score", format="%.2f"),
                        "Upgrade": st.column_config.NumberColumn("Upgrade", format="%+.2f"),
                    },
                )

with model_tab:
    st.markdown("### How the recommendation model thinks")
    st.markdown(
        "The model uses official FPL signals first, then supplements them with Understat when available. "
        "It deliberately keeps the final score explainable rather than treating it as a black box."
    )
    st.markdown("**Core signals**")
    st.markdown(
        "- **Expected points + form:** immediate FPL output signals.\n"
        "- **Position-adjusted fixtures:** official FDR plus opponent attack/defence strength and home/away context.\n"
        "- **xGI/90:** official FPL expected goal involvement where available, blended lightly with Understat.\n"
        "- **Set pieces:** penalty, direct-free-kick and corner order when present in FPL data.\n"
        "- **Minutes security:** protects against attractive but unreliable rotation picks.\n"
        "- **Defensive upside:** clean-sheet/xGC profile for defenders and goalkeepers, with save upside for keepers.\n"
        "- **Defensive contributions:** 2026/27 DC potential when a per-90 signal is exposed by the live FPL payload; otherwise neutral.\n"
        "- **Bonus/BPS:** rewards players whose underlying FPL actions often translate into bonus points.\n"
        "- **Captaincy:** an extra upside signal for midfielders and forwards, not a dominant weighting.\n"
        "- **Ownership + momentum:** changes meaning depending on Differential vs Protect Rank strategy."
    )
    st.info("The Upgrade score is usually more important than a player's absolute score: it asks whether the proposed incoming player is sufficiently better than the player you would actually sell.")
    st.caption("This is a decision-support tool, not a prediction guarantee. Check late team news, press conferences and injuries before confirming transfers.")

st.divider()
st.caption("FPL Transfer Advisor · Uses official Fantasy Premier League data with optional Understat context. FPL-inspired interface; not affiliated with or endorsed by the Premier League.")

