"""
Tests for digest.py. Run with:
    python3 -m unittest -v test_digest.py

Each test builds a small fake data file with ONE known problem, then checks
that the tool reacts the right way. The last tests run on the real file.
"""

import os
import tempfile
import unittest

import pandas as pd

import digest

REPORT_DAY = "2026-10-14"


def fake_data(shops=("Shop A", "Shop B"), visits=1000, orders=50, revenue=1500.0, wiggle=0.05):
    """30 days (15 Sep - 14 Oct) of steady numbers. Each day goes up or down by
    `wiggle` (5% by default) so there is some normal day-to-day noise."""
    rows = []
    for shop in shops:
        for i, day in enumerate(pd.date_range("2026-09-15", "2026-10-14")):
            w = 1 + (wiggle if i % 2 else -wiggle)
            rows.append({"date": day.strftime("%Y-%m-%d"), "item": shop,
                         "visits": str(round(visits * w)), "orders": str(round(orders * w)),
                         "revenue_usd": f"{revenue * w:.2f}"})
    return pd.DataFrame(rows)


def set_value(df, shop, day, column, value):
    df.loc[(df["item"] == shop) & (df["date"] == day), column] = str(value)


def analyse(df):
    """Run the whole tool on a table; return everything it produces."""
    data, issues = digest.check_data(df)
    report_date = data["date"].max()
    flags, info = digest.find_flags(data, report_date)
    lines = digest.make_summary(flags, info, issues, report_date)
    return data, issues, flags, lines


def found(issues, words):
    """Everything found by the check whose name contains `words`."""
    return next(v for k, v in issues.items() if words in k)


class DataChecks(unittest.TestCase):

    def test_clean_file_has_no_problems(self):
        _, issues, flags, _ = analyse(fake_data())
        self.assertEqual(sum(len(v) for v in issues.values()), 0)
        self.assertTrue(flags.empty)

    def test_exact_duplicate_is_found_and_removed(self):
        df = fake_data()
        df = pd.concat([df, df.iloc[[5]]], ignore_index=True)
        data, issues, _, _ = analyse(df)
        self.assertEqual(len(found(issues, "Exact duplicate")), 1)
        self.assertEqual(len(data), 60)  # back to one row per shop per day

    def test_same_day_with_different_numbers_is_found(self):
        df = fake_data()
        extra = df.iloc[[5]].copy()
        extra["visits"] = "9999"
        _, issues, _, _ = analyse(pd.concat([df, extra], ignore_index=True))
        self.assertEqual(len(found(issues, "different numbers")), 1)

    def test_missing_day_is_found(self):
        df = fake_data()
        df = df[~((df["item"] == "Shop A") & (df["date"] == "2026-10-01"))]
        _, issues, _, _ = analyse(df)
        self.assertEqual(found(issues, "Missing days"), ["Shop A on 01 Oct"])

    def test_empty_value_is_found(self):
        df = fake_data()
        set_value(df, "Shop A", "2026-10-01", "orders", "")
        _, issues, _, _ = analyse(df)
        self.assertEqual(len(found(issues, "Empty")), 1)

    def test_text_instead_of_number_is_found(self):
        df = fake_data()
        set_value(df, "Shop A", "2026-10-01", "revenue_usd", "abc")
        _, issues, _, _ = analyse(df)
        self.assertEqual(len(found(issues, "not numbers")), 1)

    def test_negative_value_is_found(self):
        df = fake_data()
        set_value(df, "Shop A", "2026-10-01", "visits", -5)
        _, issues, _, _ = analyse(df)
        self.assertEqual(len(found(issues, "Negative")), 1)

    def test_bad_date_is_found(self):
        df = fake_data()
        df.loc[0, "date"] = "15/09/2026"
        _, issues, _, _ = analyse(df)
        self.assertEqual(len(found(issues, "Unreadable")), 1)

    def test_zero_day_is_left_out_of_normal(self):
        df = fake_data()
        for col in ["visits", "orders", "revenue_usd"]:
            set_value(df, "Shop A", "2026-10-10", col, 0)
        data, issues, flags, _ = analyse(df)
        self.assertEqual(len(found(issues, "every number is zero")), 1)
        self.assertFalse(data[(data["item"] == "Shop A") & (data["date"] == "2026-10-10")]["usable"].iloc[0])
        self.assertTrue(flags.empty)  # the zero day didn't make 14 Oct look unusual

    def test_missing_column_gives_clear_error(self):
        with self.assertRaises(ValueError):
            digest.check_data(fake_data().drop(columns=["orders"]))


class Flagging(unittest.TestCase):

    def test_big_drop_is_flagged(self):
        df = fake_data()
        set_value(df, "Shop A", REPORT_DAY, "orders", 25)       # orders halved
        set_value(df, "Shop A", REPORT_DAY, "revenue_usd", 750)
        _, _, flags, lines = analyse(df)
        self.assertIn("orders", set(flags["metric"]))
        self.assertEqual(set(flags["shop"]), {"Shop A"})
        self.assertTrue(lines[0].startswith("Shop A"))

    def test_normal_noise_is_not_flagged(self):
        df = fake_data()
        set_value(df, "Shop A", REPORT_DAY, "orders", 56)        # about +10%
        _, _, flags, _ = analyse(df)
        self.assertTrue(flags.empty)

    def test_noisy_shop_needs_a_bigger_change(self):
        # This shop normally swings 25% a day, so a 35% change is not unusual for it.
        df = fake_data(wiggle=0.25)
        set_value(df, "Shop A", REPORT_DAY, "visits", 1350)
        _, _, flags, _ = analyse(df)
        self.assertTrue(flags.empty)

    def test_tiny_shop_with_zero_orders_is_not_flagged(self):
        df = fake_data(visits=60, orders=2, revenue=50.0)
        set_value(df, "Shop A", REPORT_DAY, "orders", 0)
        set_value(df, "Shop A", REPORT_DAY, "revenue_usd", 0)
        _, _, flags, lines = analyse(df)
        self.assertTrue(flags.empty)
        self.assertIn("too small to judge", " ".join(lines))

    def test_all_zero_report_day_is_flagged_first(self):
        df = fake_data(shops=("Shop A", "Shop B", "Shop C"))
        set_value(df, "Shop B", REPORT_DAY, "orders", 20)        # a smaller problem
        set_value(df, "Shop B", REPORT_DAY, "revenue_usd", 600)
        for col in ["visits", "orders", "revenue_usd"]:
            set_value(df, "Shop C", REPORT_DAY, col, 0)          # the big problem
        _, _, flags, lines = analyse(df)
        self.assertEqual(set(flags["shop"]), {"Shop B", "Shop C"})
        self.assertTrue(lines[0].startswith("Shop C reported zero"))
        self.assertTrue(lines[1].startswith("Shop B"))

    def test_missing_report_day_row_is_flagged(self):
        df = fake_data()
        df = df[~((df["item"] == "Shop B") & (df["date"] == REPORT_DAY))]
        _, _, flags, lines = analyse(df)
        self.assertEqual(list(flags["kind"]), ["missing row"])
        self.assertIn("no data", lines[0])

    def test_summary_is_always_five_lines(self):
        quiet = fake_data()
        busy = fake_data(shops=[f"Shop {i}" for i in range(8)])
        for i in range(8):                                        # 8 shops all crash
            set_value(busy, f"Shop {i}", REPORT_DAY, "orders", 5)
        for df in (quiet, busy):
            _, _, _, lines = analyse(df)
            self.assertEqual(len(lines), 5)


class RealFile(unittest.TestCase):

    def test_real_file(self):
        with tempfile.TemporaryDirectory() as out:
            lines = digest.run("daily_metrics.csv", out)
            self.assertEqual(len(lines), 5)
            self.assertTrue(lines[0].startswith("Shop 18"))   # zero day: most urgent
            self.assertTrue(lines[1].startswith("Shop 07"))   # orders halved
            self.assertTrue(lines[2].startswith("Shop 13"))   # traffic spike, no sales
            flags = pd.read_csv(os.path.join(out, "flags.csv"))
            self.assertEqual(set(flags["shop"]), {"Shop 07", "Shop 13", "Shop 18"})
            with open(os.path.join(out, "data_quality.txt")) as f:
                report = f.read()
            self.assertIn("Shop 04 on 02 Oct", report)


if __name__ == "__main__":
    unittest.main()
