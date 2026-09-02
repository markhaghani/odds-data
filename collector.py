#!/usr/bin/env python3
"""
Odds Drift collector.

Pulls season-long football odds from Polymarket, gates them for quality,
stores every snapshot in SQLite, and exports a compact JSON for the chart at
markhaghani.com/odds.

Runs hourly via GitHub Actions (see .github/workflows/collect.yml).
On an empty database it backfills from Polymarket's own price history:
daily bars to market open, hourly bars for the ~31 days they retain.

Sources are adapters; Betfair slots in beside Polymarket without touching
the schema (see SPEC.md in this repo).
"""

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "odds-drift-collector/1.0 (github.com/markhaghani/odds-data)"}

DB = "data/odds.sqlite"
EXPORT = "data/epl.json"

# Season rollover (each July): bump SEASON, swap the MARKETS slugs for the new
# season's events, add promoted teams to CANON. Old rows keep their season tag,
# so series from different seasons never concatenate into one line.
SEASON = "2026-27"

# market key -> (polymarket event slug, slots)
MARKETS = {
    ("EPL", "WINNER"): ("epl-2027-champion-20260701200428749", 1),
    ("EPL", "TOP_4"): ("premier-league-top-4-finishers-2026-27", 4),
    # ("EFL_CHAMPIONSHIP", "PROMOTION"): untraded on Polymarket (book sums to
    # ~11 vs a target of 3). Add via the Betfair adapter, not here.
}

# Canonical team names. Every source spelling must map here; unknown names
# fail loudly rather than silently splitting a team into two series.
CANON = {
    "Arsenal": "Arsenal", "Aston Villa": "Aston Villa", "Bournemouth": "Bournemouth",
    "Brentford": "Brentford", "Brighton": "Brighton", "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace", "Everton": "Everton", "Fulham": "Fulham",
    "Leeds United": "Leeds United", "Liverpool": "Liverpool",
    "Manchester City": "Manchester City", "Manchester United": "Manchester United",
    "Newcastle United": "Newcastle United", "Nottingham Forest": "Nottingham Forest",
    "Tottenham": "Tottenham", "Sunderland": "Sunderland", "Coventry City": "Coventry City",
    "Ipswich Town": "Ipswich Town", "Hull City": "Hull City", "West Ham": "West Ham",
    "Wolves": "Wolves", "Burnley": "Burnley", "Sheffield United": "Sheffield United",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshot (
  ts          TEXT NOT NULL,
  source      TEXT NOT NULL,
  season      TEXT NOT NULL,
  competition TEXT NOT NULL,
  market      TEXT NOT NULL,
  team        TEXT NOT NULL,
  raw_prob    REAL NOT NULL,
  norm_prob   REAL,
  liquidity   REAL,
  book_sum    REAL NOT NULL,
  quality     TEXT NOT NULL,
  PRIMARY KEY (ts, source, season, competition, market, team)
);
CREATE INDEX IF NOT EXISTS idx_series
  ON odds_snapshot (competition, market, team, ts);
"""


def get(url, params):
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# Placeholder runners Polymarket parks in an event before teams are known.
SKIP = {"Team A", "Team B", "Team C", "Another Team", "Other", "Field"}


def canon(name):
    if name in SKIP:
        return None
    if name not in CANON:
        sys.exit(f"unknown team name from source: {name!r} - add it to CANON")
    return CANON[name]


def gate(probs, slots):
    """Quality verdict for one book snapshot. Flag, never silently drop."""
    total = sum(probs.values())
    distinct = len(set(round(p, 4) for p in probs.values()))
    if total > slots * 1.6 or distinct <= 4:
        return "untraded", total
    # a book far below the slot count means teams are missing from the
    # snapshot; scaling the survivors up paints a fake all-teams spike
    if total < slots * 0.7:
        return "partial", total
    if total > slots * 1.25:
        return "wide", total
    return "ok", total


def normalize(probs, slots, verdict, total):
    if verdict in ("untraded", "partial") or not total:
        return {t: None for t in probs}
    return {t: round(p * slots / total, 5) for t, p in probs.items()}


def write(db, ts, source, comp, market, probs, liq, slots):
    verdict, total = gate(probs, slots)
    norm = normalize(probs, slots, verdict, total)
    rows = [
        (ts, source, SEASON, comp, market, t, probs[t], norm[t], liq.get(t),
         round(total, 5), verdict)
        for t in probs
    ]
    db.executemany(
        "INSERT OR REPLACE INTO odds_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return verdict, total


def snapshot_now(db):
    """One current reading per market - the hourly heartbeat."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    for (comp, market), (slug, slots) in MARKETS.items():
        ev = get(f"{GAMMA}/events", {"slug": slug})
        if not ev:
            print(f"!! {comp}/{market}: event not found, skipping")
            continue
        probs, liq = {}, {}
        for m in ev[0].get("markets", []):
            team = m.get("groupItemTitle")
            prices = json.loads(m.get("outcomePrices") or "[]")
            if not team or not prices:
                continue
            c = canon(team)
            if c is None:
                continue
            probs[c] = float(prices[0])
            liq[c] = float(m.get("liquidity") or 0)
        verdict, total = write(db, ts, "polymarket", comp, market, probs, liq, slots)
        print(f"{ts} {comp}/{market}: {len(probs)} teams, "
              f"book_sum={total:.3f}, quality={verdict}")


def backfill(db):
    """Seed an empty database from Polymarket's retained history.

    fidelity=1440 reaches back to market open; fidelity=60 covers ~31 days.
    startTs is broken server-side - interval+fidelity is the working call.
    """
    print("empty database - backfilling from Polymarket history")
    for (comp, market), (slug, slots) in MARKETS.items():
        ev = get(f"{GAMMA}/events", {"slug": slug})
        if not ev:
            continue
        tokens = {}
        for m in ev[0].get("markets", []):
            team = m.get("groupItemTitle")
            toks = json.loads(m.get("clobTokenIds") or "[]")
            c = canon(team) if team else None
            if c and toks:
                tokens[c] = toks[0]

        for fidelity, bucket in ((1440, 86400), (60, 3600)):
            series = {}
            for team, tok in tokens.items():
                try:
                    h = get(f"{CLOB}/prices-history",
                            {"market": tok, "interval": "max",
                             "fidelity": fidelity}).get("history", [])
                except Exception as e:
                    print(f"   ! history {team}: {e}")
                    continue
                for p in h:
                    t = (p["t"] // bucket) * bucket
                    series.setdefault(t, {})[team] = p["p"]
                time.sleep(0.12)
            for t in sorted(series):
                ts = datetime.fromtimestamp(t, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                write(db, ts, "polymarket", comp, market, series[t], {}, slots)
            print(f"{comp}/{market}: backfilled {len(series)} snapshots "
                  f"at fidelity={fidelity}")


def export(db):
    """Compact JSON for the site, split by source so the chart can switch
    between them: daily series full-history plus hourly for the last 14 days,
    gated rows excluded (they render as gaps)."""
    out = {"generated": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "season": SEASON, "markets": {}}
    for (comp, market), (_, slots) in MARKETS.items():
        rows = db.execute(
            """SELECT source, ts, team, norm_prob FROM odds_snapshot
               WHERE season=? AND competition=? AND market=?
                 AND norm_prob IS NOT NULL ORDER BY ts""",
            (SEASON, comp, market)).fetchall()
        sources = {}
        cutoff = time.time() - 14 * 86400
        for source, ts, team, p in rows:
            blk = sources.setdefault(source, {"daily": {}, "hourly": {}})
            epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            blk["daily"].setdefault(team, {})[ts[:10]] = p  # last write per day
            if epoch >= cutoff:
                blk["hourly"].setdefault(team, {})[ts] = p
        key = f"{comp}_{market}"
        out["markets"][key] = {"slots": slots, "sources": sources}
        for source, blk in sources.items():
            n = sum(len(v) for v in blk["daily"].values())
            print(f"export {key}/{source}: {len(blk['daily'])} teams, {n} daily points")
    with open(EXPORT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {EXPORT}")


def main():
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    empty = db.execute("SELECT COUNT(*) FROM odds_snapshot").fetchone()[0] == 0
    if empty:
        backfill(db)
    snapshot_now(db)
    db.commit()
    export(db)
    db.close()


if __name__ == "__main__":
    main()
