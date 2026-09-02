# odds-data

Hourly collector for season-long football odds, feeding the chart at
[markhaghani.com/odds](https://markhaghani.com/odds).

- `collector.py` pulls Premier League title and top-4 odds from Polymarket,
  gates each snapshot for quality (untraded books are flagged, never charted),
  stores everything in `data/odds.sqlite`, and exports `data/epl.json`.
- `.github/workflows/collect.yml` runs it hourly and commits the result.
- The site fetches `data/epl.json` from raw.githubusercontent.com at page load,
  so data updates without rebuilding the site.

EFL Championship promotion is planned via a Betfair adapter (Polymarket's
promotion market is untraded - the book sums to ~11 against a target of 3).
