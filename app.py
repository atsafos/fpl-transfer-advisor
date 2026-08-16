"""
FPL Transfer Advisor
---------------------
A Streamlit app that pulls your current squad from the official Fantasy
Premier League API, supplements it with underlying-stats data from Understat
and ownership-momentum signals, flags problems (injuries/doubts, poor form,
tough upcoming fixtures), and suggests affordable replacements — capped to
however many free transfers you actually have banked.

Data sources:
- https://fantasy.premierleague.com/api/   (official FPL data - squad, form, fixtures, prices)
- https://understat.com/league/EPL/{season} (season-aggregate xG/xA per player - public page,
  no login/paywall; parsed the same way the open-source `understatapi` / `understat` community
  packages do: a JSON blob embedded in the page's own <script> tag)
"""

import json
import re
import unicodedata

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

st.set_page_config(page_title="FPL Transfer Advisor", page_icon="⚽", layout="wide")

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
    """Simulate FPL's free-transfer banking (max 5, +1 per GW, reset to 1 after a
    Wildcard/Free Hit, unaffected by chip weeks) using the manager's own history.
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


def player_score(p, avg_fdr, understat_match, total_players):
    """Composite desirability score for ranking transfer-in candidates.
    Blends official FPL signals (form, expected points, fixture ease,
    availability) with two supplementary layers:
      - Understat underlying stats (xG90 + xA90) - rewards players creating
        more than raw points suggest, the classic 'buy before the points come'
        signal content creators lean on.
      - Ownership momentum from official transfer-in/out data this GW.
    """
    form = float(p["form"] or 0)
    ep_next = float(p["ep_next"] or 0)
    availability = 1.0 if p["status"] == "a" else 0.3 if p["status"] == "d" else 0.0
    fixture_ease = (6 - avg_fdr) if avg_fdr is not None else 3  # invert FDR (1=easy..5=hard)

    understat_signal = 0.0
    if understat_match and float(understat_match.get("time", 0) or 0) > 0:
        mins = float(understat_match["time"])
        xg90 = float(understat_match.get("xG", 0)) / mins * 90
        xa90 = float(understat_match.get("xA", 0)) / mins * 90
        understat_signal = min(5.0, (xg90 + xa90) * 10)  # scale to a roughly 0-5 range

    momentum_score, _ = ownership_momentum(p, total_players)

    score = (
        (form * 0.30)
        + (ep_next * 0.25)
        + (fixture_ease * 0.15)
        + (availability * 5 * 0.10)
        + (understat_signal * 0.12)
        + (momentum_score * 0.08)
    )
    return round(score, 2)


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

st.title("⚽ FPL Transfer Advisor")
st.caption("Pulls live data from the official Fantasy Premier League API — refresh any time before your deadline.")

with st.sidebar:
    st.header("Your Team")
    team_id = st.text_input("FPL Team ID", value=st.session_state.get("team_id", "4935366"),
                             help="Found in the URL when viewing 'Points' for your team on fantasy.premierleague.com, "
                                  "e.g. .../entry/1234567/event/5 → your ID is 1234567")
    run = st.button("Load / Refresh my squad", type="primary", use_container_width=True)
    st.divider()
    st.subheader("Settings")
    form_threshold = st.slider("Poor form threshold", 0.0, 5.0, 2.5, 0.5,
                                help="Players with recent form below this are flagged.")
    fdr_threshold = st.slider("Tough fixtures threshold (avg FDR)", 2.0, 5.0, 3.8, 0.1,
                               help="Higher = only flag really tough runs.")
    n_fixtures = st.slider("Fixtures to look ahead", 3, 8, 5)
    st.divider()
    st.caption("Data refreshes from the FPL API every 30 minutes automatically, "
               "or click 'Load / Refresh' any time for the latest.")

if not team_id:
    st.info("👈 Enter your FPL Team ID in the sidebar to get started. "
            "Not sure where to find it? Log into fantasy.premierleague.com, click **Points**, "
            "and look at the number in the web address.")
    st.stop()

try:
    team_id_int = int(team_id)
except ValueError:
    st.error("Team ID should be numbers only.")
    st.stop()

with st.spinner("Fetching latest FPL data..."):
    try:
        bootstrap = get_bootstrap()
        fixtures = get_fixtures()
        entry = get_entry(team_id_int)
        history = get_history(team_id_int)
    except requests.exceptions.HTTPError:
        st.error("Couldn't find that Team ID. Double check it and try again.")
        st.stop()
    except requests.exceptions.RequestException:
        st.error("Couldn't reach the FPL API right now. Try again in a minute.")
        st.stop()

teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
st.session_state["teams_by_id"] = teams_by_id
players_by_id = {p["id"]: p for p in bootstrap["elements"]}
events = bootstrap["events"]
total_players_in_game = bootstrap.get("total_players", 1)

squad_event = current_or_last_event(events)
upcoming_event = next_event(events)

# Understat season is labelled by its start year (e.g. the 2026/27 PL season = "2026")
season_start_year = squad_event["deadline_time"][:4]
if int(squad_event["deadline_time"][5:7]) < 7:  # Jan-Jun deadline means season started the previous year
    season_start_year = str(int(season_start_year) - 1)
with st.spinner("Fetching supplementary xG/xA data from Understat..."):
    understat_players = get_understat_players(season_start_year)
by_full, by_last = build_understat_lookup(understat_players)
if not understat_players:
    st.caption("ℹ️ Understat supplementary data unavailable right now (early season or site unreachable) — "
               "suggestions below are based on official FPL data only.")

free_transfers = free_transfers_available(history, events)

try:
    picks_data = get_picks(team_id_int, squad_event["id"])
except requests.exceptions.HTTPError:
    st.error("Couldn't load your squad for this gameweek yet — the season may not have started, "
             "or picks aren't published for this gameweek.")
    st.stop()

st.success(f"Loaded **{entry['name']}** ({entry['player_first_name']} {entry['player_last_name']}) — "
           f"squad as of GW{squad_event['id']}. Suggestions target **GW{upcoming_event['id']}** "
           f"(deadline: {datetime.fromisoformat(upcoming_event['deadline_time'].replace('Z','+00:00')).strftime('%a %d %b, %H:%M UTC')}).")

bank = picks_data["entry_history"]["bank"] / 10
squad_value = picks_data["entry_history"]["value"] / 10
col1, col2, col3, col4 = st.columns(4)
col1.metric("Bank", f"£{bank:.1f}m")
col2.metric("Squad Value", f"£{squad_value:.1f}m")
col3.metric("Overall Rank", f"{entry.get('summary_overall_rank', 'N/A'):,}" if entry.get('summary_overall_rank') else "N/A")
col4.metric("Free Transfers", free_transfers, help="Estimated from your transfer history. Wildcard/Free Hit weeks don't affect your banked free transfers.")

# ----------------------------------------------------------------------------
# Build squad table with flags
# ----------------------------------------------------------------------------
squad_rows = []
for pick in picks_data["picks"]:
    p = players_by_id[pick["element"]]
    avg_fdr, fixture_str = team_fixture_difficulty(p["team"], fixtures, upcoming_event["id"], n=n_fixtures)
    reasons = flag_reasons({**p, "form": p["form"]}, avg_fdr)
    # apply user thresholds (flag_reasons uses fixed defaults internally for form msg wording,
    # so re-check against sliders here for the "needs attention" flag)
    needs_attention = bool(reasons) or float(p["form"] or 0) < form_threshold or (avg_fdr is not None and avg_fdr >= fdr_threshold)
    _, momentum_label = ownership_momentum(p, total_players_in_game)
    squad_rows.append({
        "id": p["id"],
        "Pos": POSITION_MAP[p["element_type"]],
        "Player": p["web_name"],
        "Team": teams_by_id[p["team"]]["short_name"],
        "Cost": p["now_cost"] / 10,
        "Form": float(p["form"] or 0),
        "PPG": float(p["points_per_game"] or 0),
        "Status": STATUS_LABELS.get(p["status"], p["status"]),
        "Next fixtures": fixture_str,
        "Avg FDR": round(avg_fdr, 1) if avg_fdr is not None else None,
        "Ownership trend": momentum_label,
        "Flags": " | ".join(reasons) if reasons else "✅ No issues",
        "Needs attention": needs_attention,
        "captain": pick["is_captain"],
        "vice": pick["is_vice_captain"],
    })

squad_df = pd.DataFrame(squad_rows)

st.subheader("Your Squad")
display_df = squad_df.drop(columns=["id", "Needs attention", "captain", "vice"]).copy()
display_df["C/V"] = ["©" if r["captain"] else "Ⓥ" if r["vice"] else "" for _, r in squad_df.iterrows()]
st.dataframe(display_df, use_container_width=True, hide_index=True)

# ----------------------------------------------------------------------------
# Flagged players + suggestions
# ----------------------------------------------------------------------------
flagged = squad_df[squad_df["Needs attention"]].copy()
# Priority order: worst-affected players first, so if you can only afford your
# free transfers, you know which ones matter most.
flagged["severity"] = flagged["Flags"].apply(lambda f: f.count("⛔") * 3 + f.count("⚠️") * 2 + f.count("📉") + f.count("🗓️"))
flagged = flagged.sort_values("severity", ascending=False)

st.subheader(f"🔍 Players Worth Reviewing ({len(flagged)})")

if free_transfers >= 1:
    st.info(f"💡 You have **{free_transfers} free transfer{'s' if free_transfers != 1 else ''}** available for "
            f"GW{upcoming_event['id']}. Transfers beyond that cost **-4 points** each — the list below is "
            f"ordered by priority (most serious issues first) so you know where to spend them.")

if flagged.empty:
    st.success("No red flags this week — your squad looks fine on form, fixtures, ownership trend and availability.")
else:
    squad_ids = set(squad_df["id"])
    squad_team_counts = squad_df["Team"].value_counts().to_dict()

    for rank, (_, row) in enumerate(flagged.iterrows(), start=1):
        p = players_by_id[row["id"]]
        within_free = rank <= free_transfers
        budget_tag = "✅ within free transfers" if within_free else f"💰 would cost -4 pts (transfer #{rank})"
        with st.expander(f"#{rank} · {row['Pos']} — {row['Player']} ({row['Team']}, £{row['Cost']}m)  |  {row['Flags']}  |  {budget_tag}", expanded=(rank == 1)):
            st.write(f"**Why flagged:** {row['Flags']}")
            st.write(f"**Next fixtures:** {row['Next fixtures']} (avg FDR {row['Avg FDR']})")
            st.write(f"**Ownership trend:** {row['Ownership trend']}")

            # budget available for replacement = this player's sale value + bank
            budget = row["Cost"] + bank

            def find_candidates(relax_team_limit=False):
                out = []
                for cand in bootstrap["elements"]:
                    if cand["id"] in squad_ids:
                        continue
                    if cand["element_type"] != p["element_type"]:
                        continue
                    if cand["now_cost"] / 10 > budget + 0.001:
                        continue
                    cand_team_short = teams_by_id[cand["team"]]["short_name"]
                    current_count = squad_team_counts.get(cand_team_short, 0)
                    if not relax_team_limit and cand_team_short != row["Team"] and current_count >= MAX_PER_TEAM:
                        continue  # would breach 3-per-team rule
                    if cand["status"] in ("i", "s", "u"):
                        continue  # don't suggest unavailable players
                    c_avg_fdr, c_fixture_str = team_fixture_difficulty(cand["team"], fixtures, upcoming_event["id"], n=n_fixtures)
                    understat_match = match_understat(cand, by_full, by_last)
                    score = player_score(cand, c_avg_fdr, understat_match, total_players_in_game)
                    _, mom_label = ownership_momentum(cand, total_players_in_game)
                    xg90 = xa90 = None
                    if understat_match and float(understat_match.get("time", 0) or 0) > 0:
                        mins = float(understat_match["time"])
                        xg90 = round(float(understat_match.get("xG", 0)) / mins * 90, 2)
                        xa90 = round(float(understat_match.get("xA", 0)) / mins * 90, 2)
                    out.append({
                        "Player": cand["web_name"],
                        "Team": cand_team_short,
                        "Cost": cand["now_cost"] / 10,
                        "Form": float(cand["form"] or 0),
                        "EP next": float(cand["ep_next"] or 0),
                        "xG/90": xg90,
                        "xA/90": xa90,
                        "Own %": cand["selected_by_percent"],
                        "Trend": mom_label,
                        "Next fixtures": c_fixture_str,
                        "Score": score,
                    })
                return out

            candidates = find_candidates(relax_team_limit=False)
            relaxed_note = False
            if len(candidates) < MIN_CANDIDATES:
                relaxed_note = True
                candidates = find_candidates(relax_team_limit=True)

            cand_df = pd.DataFrame(candidates).sort_values("Score", ascending=False).head(max(MIN_CANDIDATES, 5))
            if cand_df.empty:
                st.warning("No affordable same-position replacements found even within budget alone — "
                           "you may need to free up more funds by selling elsewhere too.")
            else:
                st.write(f"**Top replacement options (within £{budget:.1f}m budget):**")
                st.dataframe(cand_df, use_container_width=True, hide_index=True)
                if relaxed_note:
                    st.caption("⚠️ Fewer than 3 options fit strictly within your 3-players-per-club limit at this "
                               "budget, so this list is relaxed on that rule — double-check your final XI won't "
                               "breach it before confirming.")
                st.caption("xG/90 and xA/90 are season-aggregate underlying stats from Understat, where available — "
                           "a player creating more than their points suggest can be a good 'buy before the rise' signal.")

st.divider()
st.caption("Scoring blends official FPL data (form, projected points, fixture difficulty, availability) with "
           "Understat underlying stats and ownership-momentum as supplementary signals. It's a decision aid, "
           "not a guarantee — always sanity-check team news nearer the deadline before making transfers.")
