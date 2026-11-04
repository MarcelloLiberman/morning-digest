# morning-digest

A small tool that reads yesterday's shop numbers, checks the data is sane, and writes a
5-line morning summary of what needs action.

## How to run it

You need Python 3 and pandas.

```bash
pip install pandas
```

```bash
python3 digest.py daily_metrics.csv
```

It prints the summary and writes three files into `output/`:

| File | What's in it |
|---|---|
| `summary.txt` | The 5-line morning summary, most important first |
| `flags.csv` | Every flagged number: normal level, actual value, % change, how unusual it is |
| `data_quality.txt` | Every data check, and what it found |

The **report day** is the last date in the file (14 Oct 2026 here).
The results from `daily_metrics.csv` are committed in `output/` so you can read them without running anything.

## How it works

**1. Data checks.** The tool looks for unreadable dates, exact duplicate rows, the same shop and day
appearing twice with different numbers, empty cells, text where a number should be, negative values,
more orders than visits, orders without revenue (or revenue without orders), days where every number
is zero, and missing shop-days. Anything it finds is listed in `data_quality.txt`. Bad rows are
**left out when working out "normal"**, so one broken day doesn't make an ordinary day look strange.

**2. Normal level.** For each shop: the average of the 7 days before the report day (7–13 Oct).
Conversion = orders ÷ visits.

**3. Flag rule.** A number is flagged only if it passes **all three** tests:

1. **Big:** at least 30% above or below normal.
2. **Unusual for that shop:** the gap is at least 3× the shop's usual day-to-day wobble
   (standard deviation over the last 28 days). A shop that normally swings ±25% needs a bigger move.
3. **Big enough to matter:** shops averaging under 10 orders a day are only judged on visits.
   For a shop with ~2 orders a day, 0 orders is normal noise.

A shop that reports all zeros, has no row, or has a broken row on the report day is always flagged.

**4. Summary.** Flagged shops are ranked: (1) no data / all zeros / broken data, then (2) shops
losing money, biggest loss first, then (3) other unusual changes, largest first. Each line gives a
best guess at the cause, based on which numbers moved. The last lines cover data quality and the
rest of the shops. All the settings are at the top of `digest.py`.

## What it found on 14 Oct

1. **Shop 18** – all zeros (normally ~$6,700/day): the shop or its data feed is down.
2. **Shop 07** – orders −57% with normal visits: likely a checkout/payment problem (~$640 lost).
3. **Shop 13** – visits +254% but conversion −64%: likely bot traffic or a mis-targeted campaign.
4. **Data:** Shop 04 had an all-zero day on 2 Oct (probably a missing day); it was left out of "normal".
5. The other 17 shops were normal. Shop 11 is too small to judge orders.

## How I tested it

```bash
python3 -m unittest -v test_digest.py
```

There are 18 tests. Most build a small fake data file with **one known problem** planted in it,
then check the tool reacts correctly:

- **Data checks:** a clean file reports nothing; a duplicate row, a clash, a missing day, an empty
  cell, text in a number column, a negative value and a bad date are each found; an all-zero day
  is excluded from "normal"; a missing column gives a clear error.
- **Flagging:** a 50% drop is flagged; a 10% wobble is not; a 35% move on a naturally noisy shop is
  not; a tiny shop at 0 orders is not; an all-zero shop is ranked above a smaller drop; a missing
  report-day row is flagged; the summary is always exactly 5 lines (quiet day and 8-shops-broken day).
- **Real file:** Shops 18, 07 and 13 are flagged in that order, nothing else is flagged, and Shop 04's
  zero day is reported.

I also explored the real data by hand first (row counts, duplicates, zeros, weekday patterns,
each shop's change) to check the tool's results matched what a person would conclude.

## Where it breaks (known limits)

- **Weekday effect.** Weekends run ~20% below weekdays. The 7-day average mixes both, so a weekday
  looks a little "up" and a weekend day a little "down" by default (~5–8%). That's well under the 30%
  threshold, but it can hide a real 30% weekend drop or exaggerate a weekday rise. Comparing with
  the same weekday in earlier weeks would fix this once there's more history.
- **The thresholds are judgement calls.** 30%, 3× wobble and 10 orders/day suit this data. A different
  business may need different numbers (they're at the top of `digest.py`).
- **It can't tell why.** All zeros could be a closed shop or a broken data feed; a traffic spike
  could be bots or a real campaign. The causes in the summary are guesses for a human to check.
- **One day at a time.** It compares one day with the week before. A slow decline over two weeks
  becomes part of "normal" and won't be flagged.
- **Platform-wide problems.** If many shops break at once, it lists them one by one rather than
  saying "something is wrong everywhere".
- **Zero vs. "nothing happened".** It assumes a day with all zeros is bad data. A shop that is
  genuinely closed on some days (e.g. Sundays) would be flagged every time.
- **History needed.** A shop with fewer than 5 good days in the last week isn't judged (it's mentioned
  in the summary). New shops need a week of data.
- **Fixed format.** It expects the columns `date, item, visits, orders, revenue_usd` and dates like
  `2026-10-14`. Other formats are reported as unreadable rather than guessed.
- **Report day = last date in the file.** If the export includes a partial day for today, that partial
  day becomes the report day and will look like a crash.
- **Totals in the summary** use the 7-day average as "normal", so they carry the same weekday effect.
