"""
Morning digest: checks the daily shop data, then flags big overnight changes.

How to run:
    python3 digest.py daily_metrics.csv

It prints a 5-line summary and writes three files into the output/ folder:
    summary.txt       the 5-line morning summary
    flags.csv         every flagged number, with its normal level and the actual value
    data_quality.txt  what the data checks found

The "report day" is the last date in the file (14 Oct 2026 for daily_metrics.csv).
"""

import os
import sys

import pandas as pd

# ---------------------------------------------------------------------------
# Settings. These are judgement calls; change them here if needed.
# ---------------------------------------------------------------------------
BIG_CHANGE = 0.30     # Test 1: a change must be at least 30% up or down ...
WOBBLES = 3           # Test 2: ... AND at least 3x the shop's usual day-to-day wobble ...
MIN_ORDERS = 10       # Test 3: ... AND the shop must average 10+ orders/day for orders,
                      #         revenue and conversion to count (smaller shops are too noisy).
BASELINE_DAYS = 7     # "normal" = average of the 7 days before the report day
WOBBLE_DAYS = 28      # the usual wobble is measured over the 28 days before the report day
MIN_HISTORY = 5       # a shop needs at least 5 good days in the last 7 to be judged

COLUMNS = ["date", "item", "visits", "orders", "revenue_usd"]
NUMBERS = ["visits", "orders", "revenue_usd"]
METRICS = ["visits", "orders", "revenue_usd", "conversion"]
NICE_NAME = {"visits": "visits", "orders": "orders",
             "revenue_usd": "revenue", "conversion": "conversion"}


# ---------------------------------------------------------------------------
# Part 1: check the data
# ---------------------------------------------------------------------------
def check_data(raw):
    """Look for problems in the raw file.

    Returns (data, issues):
      data   - one row per shop per day. Extra columns:
               'usable'   False means "don't use this row to work out normal"
               'problem'  text saying what is wrong with the row (empty if fine)
               'all_zero' True if visits, orders and revenue are all 0
      issues - a dictionary: check name -> list of things found (empty list = fine)
    """
    missing_columns = [c for c in COLUMNS if c not in raw.columns]
    if missing_columns:
        raise ValueError("The file is missing these columns: " + ", ".join(missing_columns))

    # Work on text first, so empty cells and typos are visible instead of silently guessed.
    df = raw[COLUMNS].astype(str).apply(lambda col: col.str.strip())
    issues = {}

    # Check: dates that can't be read, or rows with no shop name. These rows are dropped.
    dates = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    unreadable = dates.isna() | (df["item"] == "")
    issues["Unreadable dates or missing shop name (rows dropped)"] = [
        f"row {i + 2}: date='{df.at[i, 'date']}', shop='{df.at[i, 'item']}'"
        for i in df.index[unreadable]]
    df["date"] = dates
    df = df[~unreadable].copy()

    # Check: exact duplicate rows (same everything). Keep one copy, drop the extras.
    exact_dupes = df.duplicated(keep="first")
    issues["Exact duplicate rows (extra copies dropped)"] = [
        f"{r.item} on {r.date:%d %b}" for r in df[exact_dupes].itertuples()]
    df = df[~exact_dupes].copy()

    # Check: same shop + same day twice, but with different numbers.
    # We can't know which one is right, so we keep the first and mark it as not usable.
    clash = df.duplicated(["date", "item"], keep=False)
    issues["Same shop and day with different numbers (not used)"] = [
        f"{shop} on {day:%d %b}"
        for (day, shop) in df[clash][["date", "item"]].drop_duplicates().itertuples(index=False)]
    df["problem"] = ""
    df.loc[clash, "problem"] += "conflicting duplicate rows; "
    df = df.drop_duplicates(["date", "item"], keep="first").copy()

    # Check: empty cells and values that are not numbers.
    empty_found, text_found = [], []
    for col in NUMBERS:
        is_empty = df[col] == ""
        number = pd.to_numeric(df[col], errors="coerce")
        is_text = number.isna() & ~is_empty
        for r in df[is_empty].itertuples():
            empty_found.append(f"{r.item} on {r.date:%d %b}: {col} is empty")
        for i in df.index[is_text]:
            text_found.append(f"{df.at[i, 'item']} on {df.at[i, 'date']:%d %b}: "
                              f"{col} = '{df.at[i, col]}'")
        df.loc[is_empty, "problem"] += f"{col} empty; "
        df.loc[is_text, "problem"] += f"{col} not a number; "
        df[col] = number
    issues["Empty values (not used)"] = empty_found
    issues["Values that are not numbers (not used)"] = text_found

    # The remaining checks compare numbers with each other.
    def check(name, mask, text):
        mask = mask.fillna(False)
        issues[name] = [f"{r.item} on {r.date:%d %b}: {text}" for r in df[mask].itertuples()]
        df.loc[mask, "problem"] += text + "; "

    check("Negative values (not used)",
          (df[NUMBERS] < 0).any(axis=1), "negative value")
    check("More orders than visits (not used)",
          df["orders"] > df["visits"], "more orders than visits")
    check("Orders without revenue, or revenue without orders (not used)",
          ((df["orders"] > 0) & (df["revenue_usd"] == 0)) |
          ((df["orders"] == 0) & (df["revenue_usd"] > 0)),
          "orders and revenue don't match")
    df["all_zero"] = (df[NUMBERS] == 0).all(axis=1)
    check("Days where every number is zero (likely a missing day; not used for 'normal')",
          df["all_zero"], "visits, orders and revenue are all zero")

    # Check: missing days. Every shop should have one row for every date in the file.
    all_days = pd.date_range(df["date"].min(), df["date"].max())
    shops = sorted(df["item"].unique())
    expected = pd.MultiIndex.from_product([all_days, shops])
    present = pd.MultiIndex.from_frame(df[["date", "item"]])
    issues["Missing days (no row at all)"] = [
        f"{shop} on {day:%d %b}" for day, shop in expected.difference(present)]

    df["usable"] = df["problem"] == ""
    df = df.sort_values(["item", "date"]).reset_index(drop=True)
    return df, issues


# ---------------------------------------------------------------------------
# Part 2: compare the report day with each shop's normal level
# ---------------------------------------------------------------------------
def find_flags(data, report_date):
    """Return (flags, info).

    flags - a table with one row per flagged number
    info  - extra facts the summary needs (per-shop normal/actual, small shops, totals)
    """
    flags = []
    info = {"shops": {}, "small": [], "no_history": [], "all_shops": sorted(data["item"].unique())}

    for shop in info["all_shops"]:
        rows = data[data["item"] == shop]
        history = rows[(rows["date"] < report_date) & rows["usable"]]
        last_7 = history[history["date"] >= report_date - pd.Timedelta(days=BASELINE_DAYS)]
        last_28 = history[history["date"] >= report_date - pd.Timedelta(days=WOBBLE_DAYS)]
        today = rows[rows["date"] == report_date]

        if len(last_7) < MIN_HISTORY:
            info["no_history"].append(shop)
            continue

        # Normal level = average of the last 7 good days. Conversion = orders / visits.
        normal = last_7[NUMBERS].mean()
        normal["conversion"] = normal["orders"] / normal["visits"]

        # Usual wobble = how much the shop normally moves around from day to day
        # (the "standard deviation" over the last 28 good days).
        wobble = last_28[NUMBERS].std()
        wobble["conversion"] = (last_28["orders"] / last_28["visits"]).std()

        def add(metric, kind, actual, note=""):
            change = actual / normal[metric] - 1 if normal[metric] else float("nan")
            wobbles_away = abs(actual - normal[metric]) / wobble[metric] if wobble[metric] else float("inf")
            flags.append({"shop": shop, "metric": NICE_NAME[metric], "kind": kind,
                          "normal_7day_avg": round(normal[metric], 4),
                          "actual": round(actual, 4),
                          "change_pct": round(change * 100, 1),
                          "usual_wobble": round(wobble[metric], 4),
                          "wobbles_away": round(wobbles_away, 1),
                          "note": note})

        # No row at all for the report day.
        if today.empty:
            info["shops"][shop] = {"normal": normal, "actual": None, "status": "missing"}
            add("visits", "missing row", 0, "no data for this shop on the report day")
            continue

        t = today.iloc[0]
        actual = t[NUMBERS].astype(float)

        # Every number is zero: almost certainly an outage or a broken data feed.
        if t["all_zero"]:
            info["shops"][shop] = {"normal": normal, "actual": actual, "status": "all zero"}
            for metric in NUMBERS:
                add(metric, "all zero", 0.0, "every number is zero on the report day")
            continue

        # Row exists but is broken (empty, negative, conflicting...): we can't judge it.
        if not t["usable"]:
            info["shops"][shop] = {"normal": normal, "actual": None, "status": "broken"}
            add("visits", "broken row", float("nan"), t["problem"].strip("; "))
            continue

        actual["conversion"] = actual["orders"] / actual["visits"] if actual["visits"] else float("nan")
        info["shops"][shop] = {"normal": normal, "actual": actual, "status": "ok"}

        too_small = normal["orders"] < MIN_ORDERS
        if too_small:
            info["small"].append(shop)

        # The three tests.
        for metric in METRICS:
            if too_small and metric != "visits":
                continue                                   # Test 3: too small to judge
            if pd.isna(actual[metric]) or not normal[metric]:
                continue
            change = actual[metric] / normal[metric] - 1
            distance = abs(actual[metric] - normal[metric])
            big = abs(change) >= BIG_CHANGE                # Test 1: 30%+
            unusual = distance >= WOBBLES * wobble[metric]  # Test 2: 3x usual wobble
            if big and unusual:
                add(metric, "unusual", actual[metric])

    columns = ["shop", "metric", "kind", "normal_7day_avg", "actual", "change_pct",
               "usual_wobble", "wobbles_away", "note"]
    return pd.DataFrame(flags, columns=columns), info


# ---------------------------------------------------------------------------
# Part 3: write the 5-line summary
# ---------------------------------------------------------------------------
def show(metric, value):
    """Format a number for people: 1,234 / $1,234 / 2.1%."""
    if metric == "revenue":
        return f"${value:,.0f}"
    if metric == "conversion":
        return f"{value * 100:.1f}%"
    return f"{value:,.0f}"


def describe_shop(shop, shop_flags, facts, day_label):
    """Return (priority, money_lost, size_of_change, sentence) for one flagged shop.
    Priority 1 = shop reported nothing / broken data, 2 = losing money, 3 = other unusual."""
    normal = facts["normal"]
    kinds = set(shop_flags["kind"])

    if "missing row" in kinds:
        return (1, normal["revenue_usd"], 100,
                f"{shop} has no data at all for {day_label} (normally ~{show('revenue', normal['revenue_usd'])} a day) "
                f"- check the data export.")
    if "broken row" in kinds:
        return (1, normal["revenue_usd"], 100,
                f"{shop}'s numbers for {day_label} are broken ({shop_flags['note'].iloc[0]}) "
                f"- fix the export before trusting them.")
    if "all zero" in kinds:
        return (1, normal["revenue_usd"], 100,
                f"{shop} reported zero visits, orders and revenue on {day_label} "
                f"(normally ~{show('visits', normal['visits'])} visits and ~{show('revenue', normal['revenue_usd'])} a day) "
                f"- check whether the shop or its data feed is down.")

    actual = facts["actual"]
    flagged = set(shop_flags["metric"])
    change = {NICE_NAME[m]: actual[m] / normal[m] - 1 for m in METRICS if normal[m]}

    # A simple best guess at the cause, from which numbers moved.
    if "visits" in flagged and change["visits"] > 0 and "conversion" in flagged and change["conversion"] < 0:
        hint = "lots of extra visitors who didn't buy - check for bot traffic or a campaign sending the wrong people"
    elif "visits" in flagged and change["visits"] < 0:
        hint = "traffic dropped - check the site is up and ads/marketing are running"
    elif ("orders" in flagged or "conversion" in flagged) and change["orders"] < 0:
        hint = "visits were normal, so this looks like a checkout or payment problem"
    elif "revenue" in flagged and change["revenue"] < 0:
        hint = "orders held up, so check prices, discounts or refunds"
    elif all(change[m] > 0 for m in flagged):
        hint = "an unusually strong day - check whether a promotion is running or the numbers are wrong"
    else:
        hint = "worth a closer look"

    parts = []
    for row in shop_flags.itertuples():
        parts.append(f"{row.metric} {row.change_pct:+.0f}% ({show(row.metric, row.actual)} vs "
                     f"~{show(row.metric, row.normal_7day_avg)} normal)")
    sentence = f"{shop}: " + ", ".join(parts) + f" - {hint}"

    money_lost = normal["revenue_usd"] - actual["revenue_usd"]
    went_down = any(change[m] < 0 for m in flagged)
    if went_down and money_lost >= 1:
        sentence += f"; about {show('revenue', money_lost)} below a normal day."
        priority = 2
    else:
        sentence += "."
        priority, money_lost = 3, 0.0
    size = shop_flags["change_pct"].abs().max()
    return priority, money_lost, size, sentence


def make_summary(flags, info, issues, report_date):
    """Build exactly 5 lines, most important first."""
    day_label = f"{report_date:%d %b}"

    # One line per flagged shop, sorted: no data first, then most money lost, then biggest change.
    described = [describe_shop(shop, f, info["shops"][shop], day_label) + (shop,)
                 for shop, f in flags.groupby("shop")]
    described.sort(key=lambda d: (d[0], -d[1], -d[2]))
    shop_lines = [d[3] for d in described]
    flagged_shops = [d[4] for d in described]

    # More than 4 flagged shops: keep the top 3 and list the rest on one line.
    if len(shop_lines) > 4:
        rest = flagged_shops[3:]
        shop_lines = shop_lines[:3] + [f"Also unusual today: {', '.join(rest)} (see flags.csv)."]

    # Data-quality line.
    found = [f"{item}" for findings in issues.values() for item in findings]
    if found:
        examples = "; ".join(found[:2]) + ("; and more" if len(found) > 2 else "")
        dq_line = (f"Data checks: {len(found)} problem(s) found ({examples}) - "
                   f"bad rows were left out of 'normal'; see data_quality.txt.")
    else:
        dq_line = "Data checks: no problems found in the file."

    # Overall line.
    others = len(info["all_shops"]) - len(flagged_shops) - len(info["no_history"])
    normal_total = sum(f["normal"]["revenue_usd"] for f in info["shops"].values())
    actual_total = sum(f["actual"]["revenue_usd"] for f in info["shops"].values()
                       if f["actual"] is not None)
    overall = f"The other {others} shops were within their normal range"
    small_ok = [s for s in info["small"] if s not in flagged_shops]
    if small_ok:
        overall += f" ({', '.join(small_ok)} too small to judge orders/revenue)"
    if info["no_history"]:
        overall += f"; not enough history to judge {', '.join(info['no_history'])}"
    if normal_total:
        overall += (f". Total revenue {day_label}: {show('revenue', actual_total)} vs "
                    f"~{show('revenue', normal_total)} on a normal day "
                    f"({(actual_total / normal_total - 1) * 100:+.0f}%).")

    lines = ([] if shop_lines else ["No shop needs urgent action today."]) + shop_lines
    lines += [dq_line, overall, "Details: output/flags.csv and output/data_quality.txt.",
              "No other problems found."]
    return lines[:5]


# ---------------------------------------------------------------------------
# Part 4: save everything
# ---------------------------------------------------------------------------
def quality_report(issues, data, report_date, rows_read):
    text = ["DATA QUALITY REPORT",
            f"Rows read: {rows_read}   Shops: {data['item'].nunique()}   "
            f"Dates: {data['date'].min():%d %b %Y} to {data['date'].max():%d %b %Y}   "
            f"Report day: {report_date:%d %b %Y}",
            ""]
    for name, findings in issues.items():
        if findings:
            text.append(f"[FOUND {len(findings)}] {name}")
            text += [f"    - {f}" for f in findings]
        else:
            text.append(f"[OK] {name}: none")
    text += ["", "Rows marked 'not used' are left out when working out each shop's normal level,",
             "so a bad day doesn't make an ordinary day look unusual."]
    return "\n".join(text) + "\n"


def run(csv_path, out_dir="output"):
    raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    if raw.empty:
        raise ValueError("The file has no data rows.")
    data, issues = check_data(raw)
    report_date = data["date"].max()
    flags, info = find_flags(data, report_date)
    lines = make_summary(flags, info, issues, report_date)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    flags.to_csv(os.path.join(out_dir, "flags.csv"), index=False)
    with open(os.path.join(out_dir, "data_quality.txt"), "w") as f:
        f.write(quality_report(issues, data, report_date, len(raw)))
    return lines


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "daily_metrics.csv"
    try:
        summary = run(path)
    except (FileNotFoundError, ValueError) as error:
        print(f"Could not run the digest: {error}")
        sys.exit(1)
    print("\n".join(summary))
