#!/usr/bin/env python3
"""
dq_audit.py - deep data quality audit of one table.

Reads one table, runs a fixed catalog of data quality rules on every column, on
every row, and across columns, and writes three files to the output folder:

  dq_report.json     every profile, finding, signal, link and pattern
  dq_findings.csv    one row per finding, for Excel or SQL follow-up
  dq_dashboard.html  a self-contained analytical dashboard (works offline)

The same input and config always produce the same report: rules, thresholds,
ordering and dashboard layout are fixed. Only the generated_at timestamp moves
(pass --fixed-time to pin it).

Inputs: one .csv .tsv .txt (optionally .gz), .xlsx .xlsm .xls (use --sheet),
.parquet, .json .jsonl, .db .sqlite .sqlite3 (use --table), or a database
table or query through --db-url (needs SQLAlchemy and the driver).

Requires pandas and numpy (openpyxl for Excel, pyarrow for Parquet).
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter

import numpy as np
import pandas as pd

TOOL_VERSION = "2.1.0"
RULES_VERSION = "2026.10-single-table"

# --------------------------------------------------------------------------- #
# Thresholds. Override through the "thresholds" key of the config.
# --------------------------------------------------------------------------- #
THRESHOLDS = {
    "outlier_iqr_k": 3.0,
    "outlier_min_values": 20,
    "negative_max_share": 0.05,
    "zero_max_share": 0.20,
    "fd_min_strength": 0.97,
    "fd_max_columns": 25,
    "fd_max_distinct": 500,
    "fd_max_dominant_share": 0.8,
    "date_order_min_share": 0.95,
    "date_order_min_rows": 20,
    "calendar_pair_tolerance_days": 3,
    "calendar_pair_min_share": 0.9,
    "calc_min_share": 0.95,
    "calc_tolerance": 0.01,
    "calc_max_columns": 12,
    "packed_min_share": 0.8,
    "packed_match_min_share": 0.9,
    "typo_freq_ratio": 5,
    "typo_max_distinct": 300,
    "shape_dominant_min": 0.8,
    "identifier_unique_ratio": 0.98,
    "key_dup_min_unique_ratio": 0.9,
    "categorical_max_distinct": 50,
    "near_constant_share": 0.99,
    "script_min_share": 0.01,
    "segment_max_distinct": 30,
    "concentration_min_lift": 2.0,
    "concentration_min_rate": 0.10,
    "concentration_min_affected": 5,
    "correlation_min_phi": 0.3,
    "correlation_min_both": 5,
    "correlation_max_flags": 40,
    "truncation_min_share": 0.01,
    "variant_max_distinct": 5000,
    "max_rows": 2000000,
    "sample_values": 5,
    "sample_rows": 10,
}

SEVERITIES = ["critical", "high", "medium", "low", "info"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CATEGORIES = ["structure", "completeness", "validity", "uniqueness",
              "consistency", "plausibility"]
COLUMN_CATEGORIES = CATEGORIES[1:]
ROW = "(row)"
DEFAULT_SCALE = [(20, "high"), (5, "medium"), (0, "low")]

# --------------------------------------------------------------------------- #
# Rule catalog. IDs never change meaning; a new check gets a new ID.
# (id, name, category, severity, what it checks, why it matters)
# --------------------------------------------------------------------------- #
RULES = [
    ("DQ-T01", "Empty table", "structure", "critical",
     "The table has a header but no data rows.",
     "Nothing downstream can use it."),
    ("DQ-T02", "Malformed rows", "structure", "high",
     "Rows whose field count differs from the header (broken quoting or delimiters). They are left out of every other check.",
     "Loaders reject or shift these rows, so values land in the wrong columns."),
    ("DQ-T03", "Problem column names", "structure", "low to medium",
     "Blank, duplicated or auto-generated names, names with leading or trailing spaces, or names that need quoting in SQL.",
     "Queries break or silently hit the wrong column."),
    ("DQ-T04", "Empty rows", "structure", "medium",
     "Rows where every value is missing or a placeholder.",
     "Row counts are inflated by records that carry nothing."),
    ("DQ-C01", "Missing values", "completeness", "scale: <5% low, 5-20% medium, 20-95% high, >=95% medium (mostly-empty column); critical on keys",
     "True nulls (empty cells).",
     "Aggregates skip nulls and filters on the column miss these rows."),
    ("DQ-C02", "Empty column", "completeness", "high",
     "Every value in the column is missing or a placeholder.",
     "The column carries no information."),
    ("DQ-C03", "Placeholder missing values", "completeness", "scale: <20% medium, >=20% high",
     "Text that stands for missing or failed values: 'NULL', 'N/A', '-', 'unknown', 'ERROR', '#VALUE!', 'غير متوفر', whitespace only, and similar.",
     "IS NULL does not catch them, so completeness looks better than it is."),
    ("DQ-C04", "Catch-all values", "completeness", "scale: <5% low, >=5% medium",
     "'Other', 'أخرى', 'بدون' and similar, alone or after a prefix such as 'حي/أخرى' or 'الرياض/أخرى'.",
     "The real value was not captured; these rows cannot be grouped or matched."),
    ("DQ-V01", "Type mismatch", "validity", "scale: <5% medium, >=5% high",
     "Values whose type differs from the column's main type (text in a numeric column, numbers in a date column).",
     "CAST fails or returns NULL for these rows."),
    ("DQ-V02", "Numbers stored as formatted text", "validity", "medium",
     "Numbers written with thousand separators, currency, percent signs or accounting brackets.",
     "They do not sum or cast to numeric without cleaning."),
    ("DQ-V03", "Mixed date formats", "validity", "high",
     "More than one date format in one column, including DD/MM and MM/DD in the same column.",
     "Parsing with one format misreads or drops the others."),
    ("DQ-V04", "Invalid dates", "validity", "high",
     "Values shaped like dates that do not exist on their calendar (month 13, 30 February, Hijri day 31).",
     "Date casts fail for these rows."),
    ("DQ-V05", "Mixed calendars", "validity", "high",
     "Hijri dates inside a Gregorian column, or Gregorian dates inside a Hijri column.",
     "Durations and ranges are off by about 580 years for these rows."),
    ("DQ-V06", "Arabic-Indic digits", "validity", "medium",
     "Digits such as ١٢٣ instead of 123.",
     "Numeric casts and equality checks against Latin digits fail."),
    ("DQ-V07", "Hidden characters", "validity", "medium",
     "Control characters, zero-width marks, direction marks or non-breaking spaces.",
     "Values that look equal do not compare equal."),
    ("DQ-V08", "Encoding errors", "validity", "high",
     "Replacement characters or mojibake such as 'Ø§Ù„' (UTF-8 read with the wrong encoding).",
     "The original text is damaged."),
    ("DQ-V09", "Leading or trailing spaces", "validity", "scale: <5% low, >=5% medium; medium on identifiers",
     "Values with spaces before or after the text.",
     "Equality filters, GROUP BY and joins treat them as different values."),
    ("DQ-V10", "Inconsistent format", "validity", "medium",
     "Identifier-like values whose shape differs from the main shape (length, letters, separators).",
     "Lookups on these values fail or match the wrong record."),
    ("DQ-V11", "Invalid email", "validity", "medium",
     "Values in an email column that are not shaped like an email address.",
     "Messages and identity matching fail."),
    ("DQ-V12", "Mixed storage types", "validity", "medium",
     "Excel or SQLite cells where numbers or dates are stored as text next to real numbers or dates.",
     "Sorting, sums and casts treat these cells differently."),
    ("DQ-V13", "Mixed scripts", "validity", "scale: <5% low, >=5% medium",
     "A category column where some values are written in Arabic and others in Latin letters.",
     "The same category appears twice, once per language."),
    ("DQ-V14", "Packed or prefixed values", "validity", "low",
     "Values that combine two fields with a separator ('الرياض/النرجس') or repeat the same prefix ('حي/...').",
     "Filtering and grouping need string splitting first."),
    ("DQ-U01", "Duplicate rows", "uniqueness", "scale: <1% medium, >=1% high",
     "Rows identical to an earlier row in every column.",
     "Counts and sums are inflated."),
    ("DQ-U02", "Near-duplicate rows", "uniqueness", "medium",
     "Rows identical to an earlier row after trimming spaces and ignoring case and Arabic letter forms.",
     "The same record is counted twice under different spellings."),
    ("DQ-U03", "Duplicate keys", "uniqueness", "critical on declared keys, high on inferred keys",
     "Repeated values in a primary key or identifier column.",
     "The key does not identify one row."),
    ("DQ-U04", "No unique key", "uniqueness", "low",
     "No declared key, no column with complete unique values, and no pair of columns that is unique together.",
     "Rows cannot be addressed or deduplicated reliably."),
    ("DQ-U05", "Duplicates except the ID", "uniqueness", "scale: <5% medium, >=5% high",
     "Rows identical to an earlier row in every column except identifier columns.",
     "The same event may be stored twice under different reference numbers."),
    ("DQ-S01", "Spelling variants", "consistency", "scale: <5% low, >=5% medium",
     "The same value written with different case, spacing or Arabic letter forms (أ/ا, ة/ه, ى/ي).",
     "GROUP BY splits one category into several."),
    ("DQ-S02", "Probable typos", "consistency", "low",
     "Rare values one or two edits away from a much more common value in the same column.",
     "Categories are split and filters miss rows."),
    ("DQ-S03", "Constant column", "consistency", "low",
     "Only one distinct value in the whole column.",
     "Usually a default that was never set."),
    ("DQ-S04", "Dependency violations", "consistency", "medium",
     "Column A almost always determines column B (for example city to region), except for some rows.",
     "Rollups by B disagree with rollups by A."),
    ("DQ-S05", "Date order violations", "consistency", "high",
     "One date column is almost always on or before another (start before end), except for some rows.",
     "Durations come out negative."),
    ("DQ-S06", "Near-constant column", "consistency", "low",
     "One value covers 99% or more of the column; the remaining rows are listed.",
     "The rare values are often entry mistakes, or the column is a default."),
    ("DQ-S07", "Calculation mismatches", "consistency", "high",
     "A numeric column that equals another two combined (A × B, A ÷ B, A + B, A − B) in most rows, but not in some.",
     "Totals and rates computed from the parts disagree with the stored values."),
    ("DQ-S08", "Packed value disagrees", "consistency", "medium",
     "A packed column whose first part repeats another column ('الرياض/النرجس' next to city 'الرياض'), but with a different value in some rows.",
     "Two columns that should agree give different answers."),
    ("DQ-S09", "Hijri and Gregorian dates disagree", "consistency", "high",
     "A Hijri date column and a Gregorian date column that describe the same day in most rows, but not in some.",
     "Reports by Hijri and by Gregorian date count different rows."),
    ("DQ-P01", "Outliers", "plausibility", "scale: <1% low, >=1% medium",
     "Numbers beyond 3 interquartile ranges from the quartiles.",
     "Averages and totals are pulled by a few rows."),
    ("DQ-P02", "Unexpected negatives", "plausibility", "medium",
     "Negative numbers in a column that is otherwise positive.",
     "Totals are understated."),
    ("DQ-P03", "Sentinel values", "plausibility", "medium",
     "Stand-in values such as -1, 9999, 1900-01-01 or 9999-12-31.",
     "They count as real values in sums, averages and date ranges."),
    ("DQ-P04", "Out-of-range dates", "plausibility", "medium",
     "Gregorian dates after the as-of date or before 1900.",
     "Time series and ages are distorted."),
    ("DQ-P05", "Possible truncation", "plausibility", "medium",
     "Many text values stop at exactly the same common length (50, 100, 255).",
     "The end of the text was cut off by a field limit."),
    ("DQ-P06", "Unexpected zeros", "plausibility", "medium",
     "Zeros in a measure column that is otherwise positive (price, area, amount, year), up to 20% of values.",
     "A zero is often a missing value in disguise and pulls averages down."),
]
RULE_INFO = {r[0]: {"id": r[0], "name": r[1], "category": r[2], "severity": r[3],
                    "description": r[4], "impact": r[5]} for r in RULES}

# --------------------------------------------------------------------------- #
# Value patterns
# --------------------------------------------------------------------------- #
DISGUISED_TOKENS = {
    "null", "none", "nan", "n/a", "na", "#n/a", "nil", "-", "--", "---", "?", "??",
    "undefined", "missing", "unknown", "not available", "not applicable", "tbd",
    "empty", "blank", "(null)", "<null>", "[null]", "0000-00-00", "00/00/0000",
    "غير متوفر", "غير معروف", "لا يوجد", "لايوجد", "غير محدد",
    "error", "#error", "#value!", "#ref!", "#div/0!", "#name?", "#num!", "#null!", "#n/a!",
}
CATCHALL_TOKENS = {
    "other", "others", "misc", "miscellaneous", "various", "general",
    "أخرى", "اخرى", "أخري", "اخري", "آخر", "اخر", "غير ذلك", "متفرقة", "متفرقه", "متنوع", "بدون",
}
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
RE_INT = r"[+-]?[0-9]+"
RE_DEC = r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?[0-9]+[eE][+-]?[0-9]+"
RE_NUMFMT = (r"(?i)\(?[+-]?(?:[$€£]|sar|sr|usd|ر\.س)?\s?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
             r"(?:\.[0-9]+)?\s?(?:%|sar|sr|usd|ر\.س|﷼)?\)?")
RE_BOOL = r"(?i)true|false|yes|no"
RE_TIME = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:\s?[AaPp][Mm])?"
RE_EMAIL = r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}"
RE_HIDDEN = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00a0\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
RE_MOJIBAKE = r"\ufffd|Ã[\u0080-\u00bf]|[ØÙ][\u0080-\u02ff\u2010-\u203a]"
RE_ARABIC_LETTER = r"[\u0621-\u064A]"
RE_LATIN_LETTER = r"[A-Za-z]"
RE_YMD = (r"^(?P<y>[0-9]{4})(?P<sep>[-/.])(?P<a>[0-9]{1,2})(?P=sep)(?P<b>[0-9]{1,2})"
          r"(?P<t>[T ][0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:\s?(?:Z|[+-][0-9]{2}:?[0-9]{2}))?)?$")
RE_NNY = (r"^(?P<a>[0-9]{1,2})(?P<sep>[-/.])(?P<b>[0-9]{1,2})(?P=sep)(?P<y>[0-9]{4}|[0-9]{2})"
          r"(?P<t>[T ][0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:\s?[AaPp][Mm])?)?$")
RE_DMON = r"^(?P<a>[0-9]{1,2})[ \-/](?P<mon>[A-Za-z]{3,9})\.?[ \-/,]+(?P<y>[0-9]{4})$"
RE_MOND = r"^(?P<mon>[A-Za-z]{3,9})\.? (?P<a>[0-9]{1,2}),? (?P<y>[0-9]{4})$"
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
SENTINEL_NUMBERS = [-99999, -9999, -999, -99, -9, -1, 99, 999, 9999, 99999,
                    999999, 9999999, 99999999]
SENTINEL_DATES = ["0001-01-01", "1899-12-30", "1899-12-31", "1900-01-01", "1970-01-01",
                  "2099-12-31", "2999-12-31", "9999-12-31"]
TRUNCATION_LENGTHS = {10, 16, 20, 25, 30, 32, 40, 50, 60, 64, 80, 100, 120, 128, 150,
                      200, 250, 255, 256, 500, 512, 1000, 1024, 2000, 4000, 8000}
RE_ID_NAME = re.compile(r"(^id$|_id$|^id_|[a-z]Id$|ID$|_no$|_num$|^key$|_key$|code$|number$|^رقم|رقم$|مرجع|_uuid$|^uuid$)")
RE_SHAPE_NAME = re.compile(r"(?i)(phone|mobile|tel|iban|zip|postal|national|iqama|passport|plate|sku|جوال|هاتف|هوية)")
RE_EMAIL_NAME = re.compile(r"(?i)(e-?mail|بريد)")
RE_FUTURE_OK_NAME = re.compile(r"(?i)(expir|due|end|until|valid_to|valid_until|maturity|renewal|planned|scheduled|forecast|target|deadline|انتهاء|استحقاق)")
ARABIC_DIACRITICS = r"[\u064B-\u0652\u0670\u0640]"
PACK_SEPARATORS = ["/", "|", " - ", ":", "،"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ucall(s, func):
    """Apply a string function once per distinct value and map the result back to every row."""
    codes, uniq = pd.factorize(s, sort=False)
    res = np.asarray(func(pd.Series(uniq, dtype=object)))
    out = np.empty(len(s), dtype=res.dtype if len(res) else object)
    if len(res):
        out[:] = res[codes]
    return pd.Series(out, index=s.index)


def norm_text(s: pd.Series) -> pd.Series:
    """Normalize text: case, spaces, hidden chars, Arabic diacritics and letter forms, digits."""
    out = s.str.replace(RE_HIDDEN, "", regex=True).str.strip().str.casefold()
    out = out.str.replace(r"\s+", " ", regex=True)
    out = out.str.replace(ARABIC_DIACRITICS, "", regex=True)
    out = out.str.replace(r"[أإآٱ]", "ا", regex=True).str.replace("ى", "ي", regex=False)
    out = out.str.replace("ة", "ه", regex=False).str.translate(ARABIC_DIGITS)
    return out


NORM_DISGUISED = set(norm_text(pd.Series(sorted(DISGUISED_TOKENS))).tolist())
NORM_CATCHALL = set(norm_text(pd.Series(sorted(CATCHALL_TOKENS))).tolist())


def shape_of(s: pd.Series) -> pd.Series:
    out = s.str.replace(r"[0-9]", "9", regex=True)
    out = out.str.replace(r"[A-Za-z]", "A", regex=True)
    out = out.str.replace(RE_ARABIC_LETTER, "ع", regex=True)
    return out.str.slice(0, 32)


def osa_distance(a, b, limit=2):
    """Optimal string alignment distance (adjacent swaps count as one edit), stops past limit."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2, prev = None, list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def q(name):
    return '"' + str(name).replace('"', '""') + '"'


def fmt_int(n):
    return f"{int(n):,}"


def fmt_pct(p):
    if p == 0:
        return "0%"
    if p < 0.1:
        return "<0.1%"
    if 99.9 < p < 100:
        return ">99.9%"
    return f"{p:.1f}%"


def clip_text(v, n=80):
    v = str(v)
    return v if len(v) <= n else v[: n - 1] + "…"


VERB_3RD = {"have": "has", "are": "is", "look": "looks", "use": "uses", "contain": "contains",
            "show": "shows", "fall": "falls", "stop": "stops", "carry": "carries", "hold": "holds",
            "differ": "differs", "match": "matches", "repeat": "repeats", "share": "shares",
            "break": "breaks", "disagree": "disagrees", "store": "stores", "need": "needs", "fail": "fails"}


def singular_one(text):
    """'1 rows have' -> '1 row has' in generated summaries."""
    def fix(m):
        noun, paren, verb = m.group(1), m.group(2) or "", m.group(3)
        return f"1 {noun}{paren}" + (f" {VERB_3RD.get(verb, verb)}" if verb else "")
    return re.sub(r"(?<![\d,.])\b1 ((?:rare |stand-in |distinct |other )?(?:row|value|date|number|cell|shape|key|spelling|column|cross-column check|high finding|critical finding))s\b"
                  r"((?: \([^)]*\))?)(?: (\w+))?", fix, text)


def scale_severity(pct, cuts):
    for threshold, sev in cuts:
        if pct >= threshold:
            return sev
    return cuts[-1][1]


def sorted_counts(values):
    c = Counter(values)
    return sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))


def json_safe(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, dict):
        return {str(k): json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_safe(v) for v in o]
    if isinstance(o, np.ndarray):
        return [json_safe(v) for v in o.tolist()]
    return o


# --------------------------------------------------------------------------- #
# Loading (one table)
# --------------------------------------------------------------------------- #
def cell_to_str(v):
    """Convert a typed cell to (text, storage tag)."""
    if v is None:
        return None, "null"
    if isinstance(v, (bool, np.bool_)):
        return ("true" if v else "false"), "bool"
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if math.isnan(f):
            return None, "null"
        if f.is_integer() and abs(f) < 1e15:
            return str(int(f)), "number"
        return repr(f), "number"
    if isinstance(v, (int, np.integer)):
        return str(int(v)), "number"
    if v is pd.NaT:
        return None, "null"
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
            return v.strftime("%Y-%m-%d"), "date"
        return v.strftime("%Y-%m-%d %H:%M:%S"), "date"
    if isinstance(v, dt.date):
        return v.isoformat(), "date"
    if isinstance(v, bytes):
        return v.hex()[:64], "blob"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True), "text"
    s = str(v)
    if s == "":
        return None, "null"
    return s, "text"


def typed_frame(df):
    out, tags = {}, {}
    for c in df.columns:
        pairs = [cell_to_str(v) for v in df[c].tolist()]
        out[c] = [p[0] for p in pairs]
        tags[c] = np.array([p[1] for p in pairs], dtype=object)
    return pd.DataFrame(out, columns=list(df.columns), dtype=object), tags


def unique_headers(raw_names):
    names, issues, seen = [], [], Counter()
    for i, h in enumerate(raw_names):
        h0 = "" if h is None else str(h)
        name = h0.strip().lstrip("\ufeff")
        if name == "" or re.fullmatch(r"Unnamed: \d+", name):
            issues.append({"column": f"column_{i + 1}", "problem": "blank or auto-generated name", "raw": h0})
            name = f"column_{i + 1}"
        elif h0.lstrip("\ufeff") != name:
            issues.append({"column": name, "problem": "leading or trailing spaces in name", "raw": h0})
        seen[name] += 1
        if seen[name] > 1:
            issues.append({"column": name, "problem": "duplicated name", "raw": h0})
            name = f"{name}__{seen[name]}"
        elif re.search(r"[^\w]", name, flags=re.UNICODE):
            issues.append({"column": name, "problem": "spaces or special characters need quoting in SQL", "raw": h0})
        names.append(name)
    return names, issues


def decode_bytes(raw):
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def read_delimited(path):
    opener = gzip.open if path.lower().endswith(".gz") else open
    with opener(path, "rb") as fh:
        raw = fh.read()
    text, encoding = decode_bytes(raw)
    ext = os.path.splitext(path[:-3] if path.lower().endswith(".gz") else path)[1].lower()
    first_lines = text.splitlines()[:50]
    if ext == ".tsv":
        delim = "\t"
    else:
        best, delim = -1, ","
        for d in [",", ";", "\t", "|"]:
            counts = [ln.count(d) for ln in first_lines if ln.strip()]
            if not counts:
                continue
            score = min(counts) if len(set(counts)) <= 2 else 0
            if counts[0] > 0 and score > best:
                best, delim = score, d
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    rows = [r for r in rows if not (len(r) == 0 or (len(r) == 1 and r[0].strip() == ""))]
    meta = {"encoding": encoding, "delimiter": {"\t": "tab"}.get(delim, delim), "malformed": [], "header_issues": []}
    if not rows:
        return pd.DataFrame(), {}, meta
    header, data = rows[0], rows[1:]
    names, header_issues = unique_headers(header)
    width = len(names)
    good, malformed = [], []
    for i, r in enumerate(data):
        if len(r) == width:
            good.append(r)
        else:
            malformed.append({"row": i + 1, "fields": len(r), "expected": width,
                              "preview": clip_text(delim.join(r), 120)})
    frame = pd.DataFrame(good, columns=names, dtype=object)
    frame = frame.where(frame != "", None)
    meta.update({"malformed": malformed, "header_issues": header_issues})
    return frame, {}, meta


def load_table(path, sheet, table, notes):
    """Return (table_name, frame, storage_tags, meta) for one table."""
    base = os.path.basename(path)
    stem = base[:-3] if base.lower().endswith(".gz") else base
    stem, ext = os.path.splitext(stem)
    ext = ext.lower()
    name = re.sub(r"[^\w]+", "_", stem, flags=re.UNICODE).strip("_") or "table"
    if ext in (".csv", ".tsv", ".txt"):
        frame, tags, meta = read_delimited(path)
        meta["source"] = base
        return name, frame, tags, meta
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.ExcelFile(path).sheet_names
        use = sheet if sheet is not None else sheets[0]
        if use not in sheets:
            sys.exit(f"Sheet '{use}' not found. Sheets: {', '.join(sheets)}")
        if sheet is None and len(sheets) > 1:
            notes.append(f"The workbook has {len(sheets)} sheets; audited the first one, '{use}'. Use --sheet to pick another.")
        df = pd.read_excel(path, sheet_name=use, dtype=object, header=0)
        names, header_issues = unique_headers(list(df.columns))
        df.columns = names
        df = df.dropna(how="all")
        frame, tags = typed_frame(df)
        return name, frame, tags, {"source": f"{base} [{use}]", "malformed": [], "header_issues": header_issues}
    if ext == ".parquet":
        try:
            df = pd.read_parquet(path)
        except ImportError:
            sys.exit("Reading Parquet needs pyarrow or fastparquet.")
        frame, _ = typed_frame(df)
        return name, frame, {}, {"source": base, "malformed": [], "header_issues": []}
    if ext in (".json", ".jsonl"):
        df = pd.read_json(path, lines=(ext == ".jsonl"), dtype=False)
        frame, _ = typed_frame(df)
        return name, frame, {}, {"source": base, "malformed": [], "header_issues": []}
    if ext in (".db", ".sqlite", ".sqlite3"):
        con = sqlite3.connect(path)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            use = table or (tables[0] if tables else None)
            if use is None or use not in tables:
                sys.exit(f"Table '{use}' not found. Tables: {', '.join(tables)}")
            if table is None and len(tables) > 1:
                notes.append(f"The database has {len(tables)} tables; audited '{use}'. Use --table to pick another.")
            df = pd.read_sql_query(f"SELECT * FROM {q(use)}", con)
        finally:
            con.close()
        frame, tags = typed_frame(df)
        return use, frame, tags, {"source": f"{base} [{use}]", "malformed": [], "header_issues": []}
    sys.exit(f"Unsupported file type: {base}")


def load_database(url, table, query, name):
    try:
        import sqlalchemy
    except ImportError:
        sys.exit("--db-url needs SQLAlchemy and the database driver. Install them, or export the table to CSV.")
    engine = sqlalchemy.create_engine(url)
    sql = query or f"SELECT * FROM {table}"
    with engine.connect() as con:
        df = pd.read_sql_query(sqlalchemy.text(sql), con)
    frame, tags = typed_frame(df)
    label = name or (table or "query")
    return re.sub(r"[^\w]+", "_", label).strip("_"), frame, tags, {
        "source": f"database {'query' if query else 'table ' + str(table)}", "malformed": [], "header_issues": []}


# --------------------------------------------------------------------------- #
# Value classification
# --------------------------------------------------------------------------- #
def ymd_to_days(y, m, d):
    """Vectorized Gregorian (year, month, day) -> datetime64[D]; returns (dates, valid)."""
    y = np.asarray(y, dtype=float)
    m = np.asarray(m, dtype=float)
    d = np.asarray(d, dtype=float)
    ok = ~(np.isnan(y) | np.isnan(m) | np.isnan(d)) & (m >= 1) & (m <= 12) & (d >= 1) & (d <= 31) & (y >= 1) & (y <= 9999)
    dates = np.full(len(y), np.datetime64("NaT", "D"), dtype="datetime64[D]")
    if ok.any():
        yi, mi, di = y[ok].astype(np.int64), m[ok].astype(np.int64), d[ok].astype(np.int64)
        months = (yi - 1970) * 12 + (mi - 1)
        first = months.astype("datetime64[M]").astype("datetime64[D]")
        nxt = (months + 1).astype("datetime64[M]").astype("datetime64[D]")
        day = first + (di - 1).astype("timedelta64[D]")
        inside = day < nxt
        idx = np.flatnonzero(ok)
        dates[idx[inside]] = day[inside]
        ok[idx[~inside]] = False
    return dates, ok


def hijri_to_days(y, m, d):
    """Tabular Islamic calendar -> datetime64[D]. Umm al-Qura can differ by 1-2 days."""
    y = np.asarray(y, dtype=float)
    m = np.asarray(m, dtype=float)
    d = np.asarray(d, dtype=float)
    jd = d + np.ceil(29.5 * (m - 1)) + (y - 1) * 354 + np.floor((3 + 11 * y) / 30) + 1948439.5 - 1
    days = np.floor(jd - 2440587.5 + 0.5)
    out = np.full(len(y), np.datetime64("NaT", "D"), dtype="datetime64[D]")
    ok = ~np.isnan(days)
    out[ok] = days[ok].astype(np.int64).astype("datetime64[D]")
    return out


def classify_values(vals, as_of_year):
    """Classify stripped, Latin-digit strings. Returns dict of arrays aligned to vals."""
    m = len(vals)
    kind = np.full(m, "text", dtype=object)
    datefmt = np.full(m, None, dtype=object)
    Y = np.full(m, np.nan)
    M = np.full(m, np.nan)
    D = np.full(m, np.nan)
    order_note = None
    remaining = np.ones(m, dtype=bool)

    def take(regex, label):
        if not remaining.any():
            return
        sub = vals[remaining]
        hit_local = sub.str.fullmatch(regex).fillna(False).to_numpy(dtype=bool)
        idx = np.flatnonzero(remaining)[hit_local]
        kind[idx] = label
        remaining[idx] = False

    take(RE_INT, "int")
    take(RE_DEC, "decimal")
    if remaining.any():
        sub_idx = np.flatnonzero(remaining)
        ex = vals.iloc[sub_idx].str.extract(RE_YMD)
        hit = ex["y"].notna().to_numpy()
        if hit.any():
            e = ex[hit]
            idx = sub_idx[hit]
            has_t = e["t"].notna().to_numpy()
            kind[idx] = np.where(has_t, "datetime", "date")
            datefmt[idx] = [f"YYYY{s}MM{s}DD" + (" hh:mm" if t else "") for s, t in zip(e["sep"].to_numpy(), has_t)]
            Y[idx] = e["y"].astype(float).to_numpy()
            M[idx] = e["a"].astype(float).to_numpy()
            D[idx] = e["b"].astype(float).to_numpy()
            remaining[idx] = False
    if remaining.any():
        sub_idx = np.flatnonzero(remaining)
        ex = vals.iloc[sub_idx].str.extract(RE_NNY)
        hit = ex["y"].notna().to_numpy()
        if hit.any():
            e = ex[hit]
            idx = sub_idx[hit]
            a = e["a"].astype(int).to_numpy()
            b = e["b"].astype(int).to_numpy()
            yraw = e["y"].astype(int).to_numpy()
            two = e["y"].str.len().to_numpy() == 2
            pivot = as_of_year % 100 + 1
            y = np.where(two, np.where(yraw <= pivot, 2000 + yraw, 1900 + yraw), yraw)
            day_first = (a > 12).any()
            month_first = (b > 12).any()
            if day_first and month_first:
                order_note = "both DD/MM and MM/DD orders present"
                dfirst = b <= 12
            elif month_first:
                order_note = "MM/DD order (month first)"
                dfirst = np.zeros(len(a), dtype=bool)
            else:
                order_note = "DD/MM order" + ("" if day_first else " assumed (all values ambiguous)")
                dfirst = np.ones(len(a), dtype=bool)
            has_t = e["t"].notna().to_numpy()
            kind[idx] = np.where(has_t, "datetime", "date")
            datefmt[idx] = [(("DD{s}MM{s}" if df_ else "MM{s}DD{s}").format(s=s) + ("YY" if tw else "YYYY") + (" hh:mm" if t else ""))
                            for s, df_, tw, t in zip(e["sep"].to_numpy(), dfirst, two, has_t)]
            Y[idx] = y
            M[idx] = np.where(dfirst, b, a)
            D[idx] = np.where(dfirst, a, b)
            remaining[idx] = False
    for regex, label in ((RE_DMON, "DD Mon YYYY"), (RE_MOND, "Mon DD, YYYY")):
        if not remaining.any():
            break
        sub_idx = np.flatnonzero(remaining)
        ex = vals.iloc[sub_idx].str.extract(regex)
        mon = ex["mon"].str.lower().str.slice(0, 3).map(MONTHS)
        hit = (ex["y"].notna() & mon.notna()).to_numpy()
        if hit.any():
            idx = sub_idx[hit]
            kind[idx] = "date"
            datefmt[idx] = label
            Y[idx] = ex["y"][hit].astype(float).to_numpy()
            M[idx] = mon[hit].astype(float).to_numpy()
            D[idx] = ex["a"][hit].astype(float).to_numpy()
            remaining[idx] = False
    take(RE_TIME, "time")
    take(RE_BOOL, "boolean")
    take(RE_NUMFMT, "numfmt")
    return {"kind": kind, "datefmt": datefmt, "y": Y, "m": M, "d": D, "order_note": order_note}


FAMILY = {"int": "numeric", "decimal": "numeric", "numfmt": "numeric", "date": "date",
          "datetime": "date", "time": "time", "boolean": "boolean", "text": "text"}
FAMILY_ORDER = ["numeric", "date", "boolean", "time", "text"]


def parse_numfmt(s: pd.Series) -> pd.Series:
    neg = s.str.startswith("(") & s.str.endswith(")")
    clean = s.str.replace(r"(?i)[$€£﷼%,\s()]|sar|sr|usd|ر\.س", "", regex=True)
    out = pd.to_numeric(clean, errors="coerce")
    return out.where(~neg, -out.abs())


# --------------------------------------------------------------------------- #
# Findings registry
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, table, n, df, sample_rows, sample_values, redact):
        self.table = table
        self.n = n
        self.df = df
        self.findings = []
        self.masks = {}
        self.fmasks = []
        self.sample_rows = sample_rows
        self.sample_values = sample_values
        self.redact = redact

    def show(self, v):
        if v is None:
            return None
        if self.redact:
            return shape_of(pd.Series([str(v)])).iloc[0]
        return clip_text(v)

    def add(self, rule, column=None, mask=None, affected=None, summary="", values=None,
            severity=None, metric=None, sql=None, related=None, extra=None, mask_column=None):
        info = RULE_INFO[rule]
        rows = []
        if mask is not None:
            affected = int(mask.sum())
            rows = (np.flatnonzero(mask)[: self.sample_rows] + 1).tolist()
            if values is None and column is not None and column in self.df.columns:
                values = []
                for v in self.df[column].to_numpy()[mask]:
                    v = "(null)" if v is None else v
                    if v not in values:
                        values.append(v)
                    if len(values) >= self.sample_values:
                        break
        affected = int(affected or 0)
        pct = 100.0 * affected / self.n if self.n else 0.0
        shown = list(dict.fromkeys(self.show(v) if v != "(null)" else v for v in (values or [])))
        f = {
            "rule": rule, "name": info["name"], "category": info["category"],
            "severity": severity or info["severity"], "table": self.table, "column": column,
            "related": related, "affected_rows": affected, "affected_pct": round(pct, 4),
            "summary": singular_one(summary),
            "evidence": {"values": shown[: self.sample_values], "rows": rows},
            "metric": metric or {}, "sql": sql,
        }
        if extra:
            f["evidence"].update(extra)
        self.findings.append(f)
        if mask is not None:
            key = (mask_column or column or ROW, info["category"])
            prev = self.masks.get(key)
            self.masks[key] = mask.copy() if prev is None else (prev | mask)
            self.fmasks.append((f, key, mask))
        return f


# --------------------------------------------------------------------------- #
# Column analysis
# --------------------------------------------------------------------------- #
def common_prefix(vals):
    """Shared leading token ('حي/', 'قطعة ') when at least 80% of values start with it."""
    if len(vals) == 0:
        return None
    first = ucall(vals, lambda u: u.str.extract(r"^(\S+?[/\s])", expand=False))
    vc = first.value_counts()
    if len(vc) and vc.iloc[0] / len(vals) >= 0.8:
        return vc.index[0]
    return None


def analyze_column(ctx, col, series, tags, cfg, as_of, key_columns, extra_tokens):
    T = THRESHOLDS
    n = ctx.n
    isnull = series.isna().to_numpy()
    str_s = series.fillna("").astype(str)
    stripped = ucall(str_s, lambda u: u.str.strip())
    nkey_all = ucall(str_s, norm_text)
    ws_only = (~isnull) & (str_s != "").to_numpy() & (stripped == "").to_numpy()
    tokens = NORM_DISGUISED | set(norm_text(pd.Series([str(t) for t in extra_tokens], dtype=object)).tolist())
    token_hit = (~isnull) & nkey_all.isin(tokens).to_numpy() & ~ws_only
    disguised = ws_only | token_hit
    missing = isnull | disguised
    valid = ~missing
    valid_idx = np.flatnonzero(valid)
    vals = stripped[valid]
    vals_lat = ucall(vals, lambda u: u.str.translate(ARABIC_DIGITS))
    has_ar = (vals != vals_lat).to_numpy()
    vcodes, vuniq = pd.factorize(vals_lat.reset_index(drop=True), sort=False)
    cls_u = classify_values(pd.Series(vuniq, dtype=object), as_of.year)
    cls = {k: (v[vcodes] if isinstance(v, np.ndarray) else v) for k, v in cls_u.items()}
    kind = cls["kind"]
    fam = np.array([FAMILY[k] for k in kind], dtype=object) if len(kind) else np.array([], dtype=object)
    fam_counts = Counter(fam.tolist())
    nvalid = int(valid.sum())
    sql_col, sql_tab = q(col), q(ctx.table)

    def full(local_mask):
        m = np.zeros(n, dtype=bool)
        m[valid_idx[local_mask]] = True
        return m

    is_key = col in key_columns
    name_is_id = bool(RE_ID_NAME.search(col)) or col.lower() == "id"
    distinct_vals = int(vals.nunique())
    distinct_ratio = distinct_vals / nvalid if nvalid else 0.0

    if nvalid == 0:
        ctype, dominant, share = "empty", None, 0.0
    else:
        dominant = sorted(fam_counts.items(), key=lambda kv: (-kv[1], FAMILY_ORDER.index(kv[0])))[0][0]
        share = fam_counts[dominant] / nvalid
        ctype = dominant if (dominant == "text" or share >= 0.5) else "text"
        if ctype == "numeric":
            num_kinds = Counter(kind[fam == "numeric"].tolist())
            has_dot = ucall(vals_lat[fam == "numeric"], lambda u: u.str.contains(".", regex=False)).any()
            ctype = "integer" if num_kinds.get("decimal", 0) == 0 and not has_dot else "decimal"
    lens_all = ucall(vals, lambda u: u.str.len()).to_numpy(dtype=np.int64) if nvalid else np.array([], dtype=np.int64)
    looks_unique = (nvalid >= 20 and distinct_ratio >= T["identifier_unique_ratio"]
                    and ctype in ("integer", "text") and lens_all.mean() <= 40
                    and ucall(vals, lambda u: u.str.contains(" ", regex=False)).mean() <= 0.05)
    is_identifier = bool(is_key or (nvalid > 0 and name_is_id and ctype in ("integer", "text")) or looks_unique)
    is_categorical = (nvalid > 0 and not is_identifier and ctype in ("text", "integer", "boolean")
                      and (distinct_vals <= T["categorical_max_distinct"] or (distinct_ratio <= 0.05 and distinct_vals <= 500)))

    # ---------------- Completeness
    if nvalid == 0 and n > 0:
        ctx.add("DQ-C02", col, mask=missing.copy(),
                summary=f"All {fmt_int(n)} values are missing or placeholders.",
                sql=f"SELECT COUNT(*) FROM {sql_tab} WHERE {sql_col} IS NOT NULL AND TRIM({sql_col}) <> '';")
    else:
        nn = int(isnull.sum())
        if nn:
            pct = 100 * nn / n
            sev = scale_severity(pct, DEFAULT_SCALE)
            if pct >= 95:
                sev = "medium"
            if is_key or (name_is_id and looks_unique and pct >= 1):
                sev = "critical"
            ctx.add("DQ-C01", col, mask=isnull.copy(), severity=sev, values=["(null)"],
                    summary=f"{fmt_int(nn)} of {fmt_int(n)} rows ({fmt_pct(pct)}) are empty."
                    + (" Mostly-empty column, left out of the clean scores." if pct >= 95 else ""),
                    sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} IS NULL;")
        nd = int(disguised.sum())
        if nd:
            pct = 100 * nd / n
            toks = sorted_counts([("(whitespace)" if v.strip() == "" else v.strip()) for v in str_s[disguised]])
            tok_list = ", ".join(f"'{t}' ×{fmt_int(c)}" for t, c in toks[:6])
            lits = ", ".join("'" + t.replace("'", "''").lower() + "'" for t, _ in toks[:8] if t != "(whitespace)")
            where = f"TRIM({sql_col}) = ''" + (f" OR LOWER(TRIM({sql_col})) IN ({lits})" if lits else "")
            ctx.add("DQ-C03", col, mask=disguised.copy(), severity=scale_severity(pct, [(20, "high"), (0, "medium")]),
                    summary=f"{fmt_int(nd)} rows ({fmt_pct(pct)}) hold a placeholder instead of a value: {tok_list}.",
                    values=[t for t, _ in toks[:5]], metric={"tokens": dict(toks[:10])},
                    sql=f"SELECT * FROM {sql_tab} WHERE {where};")

    profile = {
        "name": col, "position": None, "type": ctype, "identifier": bool(is_identifier),
        "categorical": bool(is_categorical), "rows": n, "nulls": int(isnull.sum()),
        "placeholders": int(disguised.sum()), "valid": nvalid, "distinct": distinct_vals,
        "distinct_pct": round(100 * distinct_ratio, 3) if nvalid else 0,
        "type_confidence": round(100 * share, 3) if nvalid else 0,
        "kinds": dict(sorted_counts(kind.tolist())), "mismatches": 0, "catch_all": 0,
    }
    numbers, dates_full, calendar = None, None, None
    if nvalid == 0:
        profile["top_values"] = []
        profile["cardinality"] = "empty"
        return profile, numbers, dates_full, calendar, is_categorical, is_identifier

    # Catch-all values, alone or after a prefix
    prefix = common_prefix(vals) if ctype == "text" else None
    if ctype == "text":
        def tail_tokens(u):
            last = u.str.replace(r"^.*[/|:،]\s*", "", regex=True)
            after_prefix = u.str.slice(len(prefix)) if prefix else u
            k1, k2, k3 = norm_text(u), norm_text(last), norm_text(after_prefix)
            return k1.isin(NORM_CATCHALL) | k2.isin(NORM_CATCHALL | NORM_DISGUISED) | k3.isin(NORM_CATCHALL | NORM_DISGUISED)
        catch = ucall(vals, tail_tokens).to_numpy(dtype=bool)
        if catch.any():
            pct = 100 * catch.sum() / n
            vc_ = sorted_counts(vals[catch].tolist())
            profile["catch_all"] = int(catch.sum())
            ctx.add("DQ-C04", col, mask=full(catch), severity=scale_severity(pct, [(5, "medium"), (0, "low")]),
                    summary=f"{fmt_int(catch.sum())} rows ({fmt_pct(pct)}) hold a catch-all value instead of the real one, "
                            f"{len(vc_)} distinct forms such as " + ", ".join(f"'{ctx.show(v)}'" for v, _ in vc_[:3]) + ".",
                    values=[v for v, _ in vc_[:5]])

    # ---------------- Validity: type mismatch
    if dominant in ("numeric", "date", "boolean", "time") and ctype != "text":
        bad = fam != dominant
        if bad.any():
            cnt = int(bad.sum())
            pct = 100 * cnt / n
            other = Counter(fam[bad].tolist())
            profile["mismatches"] = cnt
            ctx.add("DQ-V01", col, mask=full(bad), severity=scale_severity(pct, [(5, "high"), (0, "medium")]),
                    summary=(f"{fmt_int(cnt)} values in {'an' if ctype[0] in 'aeiou' else 'a'} {ctype} column are not {ctype}: "
                             + ", ".join(f"{k} ×{fmt_int(v)}" for k, v in sorted(other.items(), key=lambda kv: (-kv[1], kv[0]))) + "."),
                    metric={"main_type": ctype, "other_types": dict(other)})
    elif dominant == "text" and nvalid >= 10:
        non_text = {k: v for k, v in fam_counts.items() if k != "text"}
        if non_text:
            second, cnt2 = sorted(non_text.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if cnt2 / nvalid >= 0.2:
                bad = fam == second
                profile["mismatches"] = int(bad.sum())
                ctx.add("DQ-V01", col, mask=full(bad), severity="high",
                        summary=f"Text column where {fmt_pct(100 * cnt2 / nvalid)} of values are {second}. The column mixes two types.",
                        metric={"main_type": "text", "other_types": non_text})

    if dominant == "numeric":
        fm = kind == "numfmt"
        profile["formatted_pct"] = round(100 * fm.sum() / nvalid, 3)
        if fm.any():
            ctx.add("DQ-V02", col, mask=full(fm),
                    summary=f"{fmt_int(fm.sum())} numbers ({fmt_pct(100 * fm.sum() / n)} of rows) are text with separators, currency or percent signs.",
                    sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} LIKE '%,%';")

    # ---------------- Dates
    if dominant == "date" and ctype == "date":
        is_date = fam == "date"
        Y, Mo, Da = cls["y"], cls["m"], cls["d"]
        hijri_like = is_date & (Y >= 1300) & (Y <= 1500)
        greg_like = is_date & (Y >= 1800)
        hijri_column = hijri_like.sum() > greg_like.sum()
        calendar = "hijri" if hijri_column else "gregorian"
        profile["calendar"] = "Hijri" if hijri_column else "Gregorian"
        fmts = Counter(cls["datefmt"][is_date].tolist())
        fmts.pop(None, None)
        fmt_sorted = sorted(fmts.items(), key=lambda kv: (-kv[1], kv[0]))
        profile["date_formats"] = dict(fmt_sorted)
        if cls["order_note"]:
            profile["date_order"] = cls["order_note"]
        if len(fmt_sorted) > 1 or (cls["order_note"] or "").startswith("both"):
            main = fmt_sorted[0][0]
            bad = is_date & (cls["datefmt"] != main)
            ctx.add("DQ-V03", col, mask=full(bad),
                    summary=f"{len(fmt_sorted)} date formats in one column; the main format {main} covers {fmt_pct(100 * fmt_sorted[0][1] / nvalid)} of values.",
                    metric={"formats": dict(fmt_sorted)})
        if hijri_column:
            minority = greg_like
            valid_local = hijri_like & (Mo >= 1) & (Mo <= 12) & (Da >= 1) & (Da <= 30)
            pseudo, _ = ymd_to_days(Y, Mo, np.minimum(Da, 28))
            cal_dates = np.where(valid_local, pseudo, np.datetime64("NaT", "D"))
            cmp_dates = np.where(valid_local, hijri_to_days(Y, Mo, Da), np.datetime64("NaT", "D"))
        else:
            minority = hijri_like
            g_dates, g_ok = ymd_to_days(Y, Mo, Da)
            valid_local = is_date & g_ok & ~minority
            cal_dates = np.where(valid_local, g_dates, np.datetime64("NaT", "D"))
            cmp_dates = cal_dates
        if minority.any():
            ctx.add("DQ-V05", col, mask=full(minority),
                    summary=f"{fmt_int(minority.sum())} dates use the {'Gregorian' if hijri_column else 'Hijri'} calendar in a {profile['calendar']} column.")
        invalid = is_date & ~valid_local & ~minority
        if invalid.any():
            ctx.add("DQ-V04", col, mask=full(invalid),
                    summary=f"{fmt_int(invalid.sum())} values look like dates but do not exist on the {profile['calendar']} calendar.")
        if not hijri_column:
            yr = np.where(valid_local, Y, 0)
            sentinel = valid_local & np.isin(cal_dates, np.array(SENTINEL_DATES, dtype="datetime64[D]"))
            if sentinel.any():
                sv = sorted_counts([str(x) for x in cal_dates[sentinel]])
                ctx.add("DQ-P03", col, mask=full(sentinel),
                        summary=f"{fmt_int(sentinel.sum())} stand-in dates: " + ", ".join(f"{v} ×{fmt_int(c)}" for v, c in sv[:4]) + ".",
                        sql=f"SELECT * FROM {sql_tab} WHERE CAST({sql_col} AS DATE) IN ({', '.join(chr(39) + v + chr(39) for v, _ in sv[:6])});")
            as_of64 = np.datetime64(as_of.isoformat(), "D")
            future_ok = bool(RE_FUTURE_OK_NAME.search(col)) or col in set(cfg.get("future_dates_allowed", []))
            future = valid_local & ~sentinel & (cal_dates > as_of64) if not future_ok else np.zeros(len(kind), bool)
            ancient = valid_local & ~sentinel & (yr < 1900)
            oor = future | ancient
            if oor.any():
                parts = []
                if future.any():
                    parts.append(f"{fmt_int(future.sum())} after {as_of.isoformat()}")
                if ancient.any():
                    parts.append(f"{fmt_int(ancient.sum())} before 1900")
                ctx.add("DQ-P04", col, mask=full(oor), summary="Dates out of range: " + ", ".join(parts) + ".",
                        metric={"future": int(future.sum()), "before_1900": int(ancient.sum()), "as_of": as_of.isoformat()},
                        sql=f"SELECT * FROM {sql_tab} WHERE CAST({sql_col} AS DATE) > DATE '{as_of.isoformat()}' OR CAST({sql_col} AS DATE) < DATE '1900-01-01';")
        good_dates = cal_dates[valid_local]
        if len(good_dates):
            if hijri_column:
                yy, mm = Y[valid_local].astype(int), Mo[valid_local].astype(int)
                profile["date_min"] = f"{int(yy.min()):04d}-{int(Mo[valid_local][yy == yy.min()].min()):02d}"
                profile["date_max"] = f"{int(yy.max()):04d}-{int(Mo[valid_local][yy == yy.max()].max()):02d}"
            else:
                profile["date_min"], profile["date_max"] = str(good_dates.min()), str(good_dates.max())
            months = good_dates.astype("datetime64[M]")
            span = int((months.max() - months.min()).astype(int)) + 1
            if span <= 240:
                labels = np.arange(months.min(), months.max() + np.timedelta64(1, "M"))
                counts = pd.Series(months).value_counts()
                profile["date_hist"] = {"unit": "month", "labels": [str(x) for x in labels],
                                        "counts": [int(counts.get(x, 0)) for x in labels]}
            else:
                yrs = good_dates.astype("datetime64[Y]")
                labels = np.arange(yrs.min(), yrs.max() + np.timedelta64(1, "Y"))
                counts = pd.Series(yrs).value_counts()
                profile["date_hist"] = {"unit": "year", "labels": [str(x) for x in labels],
                                        "counts": [int(counts.get(x, 0)) for x in labels]}
            if not hijri_column:
                wd = pd.Series(good_dates.astype("datetime64[D]").view("int64")).add(3).mod(7)  # 0 = Monday
                wc = wd.value_counts()
                profile["weekdays"] = {d: int(wc.get(i, 0)) for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])}
        dates_full = np.full(n, np.datetime64("NaT", "D"), dtype="datetime64[D]")
        dates_full[valid_idx[valid_local]] = cmp_dates[valid_local]

    if has_ar.any():
        latin_too = (~has_ar & ucall(vals_lat, lambda u: u.str.contains(r"[0-9]", regex=True)).to_numpy(dtype=bool)).any()
        ctx.add("DQ-V06", col, mask=full(has_ar),
                summary=f"{fmt_int(has_ar.sum())} values use Arabic-Indic digits" + (" next to values with Latin digits." if latin_too else "."))

    hidden = (~isnull) & ucall(str_s, lambda u: u.str.contains(RE_HIDDEN, regex=True)).to_numpy(dtype=bool)
    if hidden.any():
        found = Counter()
        for v in str_s[hidden].head(2000):
            for ch in re.findall(RE_HIDDEN, v):
                found[f"U+{ord(ch):04X}"] += 1
        ctx.add("DQ-V07", col, mask=hidden,
                summary=f"{fmt_int(hidden.sum())} values contain hidden characters ({', '.join(k for k, _ in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[:4])}).",
                metric={"characters": dict(found)})
    moj = (~isnull) & ucall(str_s, lambda u: u.str.contains(RE_MOJIBAKE, regex=True)).to_numpy(dtype=bool)
    if moj.any():
        ctx.add("DQ-V08", col, mask=moj, summary=f"{fmt_int(moj.sum())} values show encoding damage.")

    ws = (~isnull) & ~disguised & (str_s != stripped).to_numpy()
    if ws.any():
        pct = 100 * ws.sum() / n
        lead = int((ws & (str_s.str.len() != str_s.str.lstrip().str.len()).to_numpy()).sum())
        sev = "medium" if (is_identifier or pct >= 5) else "low"
        ctx.add("DQ-V09", col, mask=ws, severity=sev,
                summary=f"{fmt_int(ws.sum())} values ({fmt_pct(pct)}) have extra spaces at the start or end" + (f" ({fmt_int(lead)} at the start)." if lead else "."),
                values=[repr(v) for v in str_s[ws].drop_duplicates().head(5)],
                sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} <> TRIM({sql_col});")

    if (is_identifier or RE_SHAPE_NAME.search(col)) and ctype in ("integer", "text") and nvalid >= 10:
        shapes = ucall(vals_lat, shape_of)
        sc = sorted_counts(shapes.tolist())
        main, main_cnt = sc[0]
        if len(sc) > 1 and main_cnt / nvalid >= T["shape_dominant_min"]:
            bad = (shapes != main).to_numpy()
            ctx.add("DQ-V10", col, mask=full(bad),
                    summary=f"{fmt_int(bad.sum())} values differ from the main shape '{main}' ({fmt_pct(100 * main_cnt / nvalid)} of values); {len(sc) - 1} other shapes.",
                    metric={"main_shape": main, "shapes": dict(sc[:8])})

    if RE_EMAIL_NAME.search(col) or ucall(vals, lambda u: u.str.contains("@", regex=False)).mean() >= 0.5:
        bad = ~ucall(vals, lambda u: u.str.fullmatch(RE_EMAIL).fillna(False)).to_numpy(dtype=bool)
        if bad.any() and (RE_EMAIL_NAME.search(col) or bad.mean() < 0.5):
            ctx.add("DQ-V11", col, mask=full(bad), summary=f"{fmt_int(bad.sum())} values are not shaped like an email address.")

    if tags is not None and len(tags) == n:
        tag_valid = tags[valid_idx]
        typed = Counter(tag_valid.tolist())
        if (typed.get("number", 0) + typed.get("date", 0)) > 0 and typed.get("text", 0) > 0:
            text_cells = (tag_valid == "text") & np.isin(fam, ["numeric", "date"])
            if text_cells.any():
                ctx.add("DQ-V12", col, mask=full(text_cells),
                        summary=f"{fmt_int(text_cells.sum())} cells hold numbers or dates stored as text next to {fmt_int(typed.get('number', 0) + typed.get('date', 0))} typed cells.",
                        metric={"storage": dict(typed)})
        profile["storage"] = dict(sorted_counts(tag_valid.tolist()))

    if is_categorical and ctype == "text":
        ar = ucall(vals, lambda u: u.str.contains(RE_ARABIC_LETTER, regex=True)).to_numpy(dtype=bool)
        la = ucall(vals, lambda u: u.str.contains(RE_LATIN_LETTER, regex=True)).to_numpy(dtype=bool)
        only_ar, only_la = ar & ~la, la & ~ar
        need = max(3, T["script_min_share"] * nvalid)
        if only_ar.sum() >= need and only_la.sum() >= need:
            minor = only_la if only_ar.sum() >= only_la.sum() else only_ar
            pct = 100 * minor.sum() / n
            ctx.add("DQ-V13", col, mask=full(minor), severity=scale_severity(pct, [(5, "medium"), (0, "low")]),
                    summary=f"{fmt_int(only_ar.sum())} values are in Arabic and {fmt_int(only_la.sum())} in Latin letters; the smaller group is flagged.")

    if ctype == "text" and nvalid >= 20:
        packed = None
        for sep in PACK_SEPARATORS:
            has = ucall(vals, lambda u, s=sep: u.str.contains(s, regex=False)).to_numpy(dtype=bool)
            if has.mean() >= T["packed_min_share"]:
                packed = sep
                break
        if packed or prefix:
            if packed:
                first = ucall(vals, lambda u, s=packed: u.str.split(s, n=1, regex=False).str[0].str.strip())
                n_first = first.nunique()
                if n_first == 1:
                    desc = f"Every value starts with the same prefix '{ctx.show(first.iloc[0])}{packed.strip()}'."
                else:
                    desc = f"{fmt_pct(100 * has.mean())} of values pack two fields separated by '{packed.strip()}' ({fmt_int(n_first)} distinct first parts)."
                profile["packed"] = {"separator": packed, "first_part_distinct": int(n_first)}
            else:
                desc = f"Most values start with the same prefix '{ctx.show(prefix)}'."
            ctx.add("DQ-V14", col, affected=0, summary=desc, values=vals.drop_duplicates().head(3).tolist())

    # ---------------- Numeric profile and plausibility
    if ctype in ("integer", "decimal"):
        num = pd.Series(np.nan, index=range(nvalid))
        nk = np.isin(kind, ["int", "decimal"])
        num[nk] = pd.to_numeric(vals_lat[nk], errors="coerce").to_numpy()
        fk = kind == "numfmt"
        if fk.any():
            num[fk] = parse_numfmt(vals_lat[fk]).to_numpy()
        numv = num.to_numpy(dtype=float)
        okn = ~np.isnan(numv)
        x = numv[okn]
        numbers = np.full(n, np.nan)
        numbers[valid_idx[okn]] = x
        if len(x):
            qs = np.percentile(x, [0, 1, 5, 25, 50, 75, 95, 99, 100])
            sd = float(x.std(ddof=0))
            skew = float(((x - x.mean()) ** 3).mean() / sd ** 3) if sd > 0 else 0.0
            profile["stats"] = {"min": qs[0], "p1": qs[1], "p5": qs[2], "q1": qs[3], "median": qs[4],
                                "q3": qs[5], "p95": qs[6], "p99": qs[7], "max": qs[8],
                                "mean": float(x.mean()), "std": sd, "skew": skew, "sum": float(x.sum()),
                                "zeros": int((x == 0).sum()), "negatives": int((x < 0).sum())}
            lo, hi = np.percentile(x, [0.5, 99.5])
            if hi > lo:
                inside = (x >= lo) & (x <= hi)
                counts, edges = np.histogram(x[inside], bins=20, range=(lo, hi))
                profile["hist"] = {"edges": edges.tolist(), "counts": counts.tolist(),
                                   "below": int((x < lo).sum()), "above": int((x > hi).sum())}
            if not is_identifier:
                q1, q3 = qs[3], qs[5]
                iqr = q3 - q1
                if len(x) >= T["outlier_min_values"] and iqr > 0:
                    k = T["outlier_iqr_k"]
                    lo_f, hi_f = q1 - k * iqr, q3 + k * iqr
                    out_local = okn.copy()
                    out_local[okn] = (x < lo_f) | (x > hi_f)
                    if out_local.any():
                        pct = 100 * out_local.sum() / n
                        n_hi = int((x > hi_f).sum())
                        ctx.add("DQ-P01", col, mask=full(out_local), severity=scale_severity(pct, [(1, "medium"), (0, "low")]),
                                summary=f"{fmt_int(out_local.sum())} values fall outside [{lo_f:,.4g}, {hi_f:,.4g}] ({fmt_int(n_hi)} above, {fmt_int(out_local.sum() - n_hi)} below); the largest is {qs[8]:,.6g}.",
                                metric={"lower_fence": lo_f, "upper_fence": hi_f})
                negs = x < 0
                if negs.any() and negs.mean() <= T["negative_max_share"]:
                    neg_local = okn.copy()
                    neg_local[okn] = negs
                    ctx.add("DQ-P02", col, mask=full(neg_local),
                            summary=f"{fmt_int(negs.sum())} negative values in a column that is {fmt_pct(100 * (1 - negs.mean()))} zero or positive.",
                            sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} < 0;")
                zeros = x == 0
                if zeros.any() and zeros.mean() <= T["zero_max_share"] and (x < 0).mean() <= T["negative_max_share"] and len(x) >= 20:
                    z_local = okn.copy()
                    z_local[okn] = zeros
                    ctx.add("DQ-P06", col, mask=full(z_local),
                            summary=f"{fmt_int(zeros.sum())} zeros in a column whose other values are positive.",
                            sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} = 0;")
                vc = pd.Series(x).value_counts()
                mode_v = sorted(vc.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                typical = float(np.median(vc.to_numpy()))
                hits = [s for s in SENTINEL_NUMBERS if vc.get(float(s), 0) >= max(3, 5 * typical) and (s > qs[7] or s < qs[1] or s == mode_v)]
                if hits:
                    sent_local = okn.copy()
                    sent_local[okn] = np.isin(x, hits)
                    ctx.add("DQ-P03", col, mask=full(sent_local),
                            summary=f"{fmt_int(sent_local.sum())} stand-in numbers: " + ", ".join(f"{h} ×{fmt_int(vc.get(float(h), 0))}" for h in hits) + ".",
                            sql=f"SELECT * FROM {sql_tab} WHERE {sql_col} IN ({', '.join(str(h) for h in hits)});")

    # ---------------- Text length profile and truncation
    lens = lens_all
    profile["length"] = {"min": int(lens.min()), "mean": float(lens.mean()), "max": int(lens.max())}
    if ctype == "text":
        lc = pd.Series(lens).value_counts().sort_index()
        if len(lc) > 24:
            edges = np.linspace(lens.min(), lens.max() + 1, 21)
            cnt, edges = np.histogram(lens, bins=edges)
            profile["length_hist"] = {"labels": [f"{int(edges[i])}–{int(math.ceil(edges[i + 1]) - 1)}" for i in range(20)], "counts": cnt.tolist()}
        else:
            profile["length_hist"] = {"labels": [str(int(k)) for k in lc.index], "counts": [int(v) for v in lc.values]}
    if ctype == "text" and nvalid >= 20:
        L = int(lens.max())
        at = lens == L
        below = np.array([(lens == L - k).sum() for k in range(1, 6)])
        spike = at.sum() >= 3 * max(1, below[0]) and at.sum() >= 3 * max(1.0, below.mean())
        varied = len(np.unique(lens)) >= 5 and at.mean() <= 0.8
        if (L >= 10 and L in TRUNCATION_LENGTHS and at.sum() >= 5 and at.mean() >= T["truncation_min_share"]
                and distinct_vals > 5 and spike and varied and not is_identifier):
            ctx.add("DQ-P05", col, mask=full(at),
                    summary=f"{fmt_int(at.sum())} values stop at exactly {L} characters, a common field limit.",
                    sql=f"SELECT * FROM {sql_tab} WHERE LENGTH({sql_col}) = {L};")

    # ---------------- Consistency within the column
    vcounts = vals.value_counts()
    top_share = vcounts.iloc[0] / nvalid
    if distinct_vals == 1 and n >= 2:
        ctx.add("DQ-S03", col, affected=0, summary=f"Every non-missing value is '{ctx.show(vals.iloc[0])}'.", values=[vals.iloc[0]])
    elif distinct_vals >= 2 and nvalid >= 100 and top_share >= T["near_constant_share"]:
        topv = sorted(vcounts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rare = (vals != topv).to_numpy()
        ctx.add("DQ-S06", col, mask=full(rare),
                summary=f"'{ctx.show(topv)}' covers {fmt_pct(100 * top_share)} of values; {fmt_int(rare.sum())} rows hold one of {distinct_vals - 1} other values.")
    if ctype in ("text", "boolean") and not is_identifier and 1 < distinct_vals <= T["variant_max_distinct"]:
        nk_ = ucall(vals, norm_text)
        groups = pd.DataFrame({"raw": vals.to_numpy(), "key": nk_.to_numpy()})
        per_key = groups.groupby("key")["raw"].nunique()
        multi = per_key[per_key > 1]
        if len(multi):
            sub = groups[groups["key"].isin(multi.index)]
            vc = sub.groupby(["key", "raw"]).size().reset_index(name="c")
            vc = vc.sort_values(["key", "c", "raw"], ascending=[True, False, True], kind="mergesort")
            canonical = vc.drop_duplicates("key").set_index("key")["raw"]
            bad_local = groups["key"].isin(multi.index).to_numpy() & (groups["raw"].to_numpy() != groups["key"].map(canonical).to_numpy())
            sizes = groups[groups["key"].isin(multi.index)].groupby("key").size()
            examples = []
            for key in sorted(multi.index, key=lambda k: (-int(sizes[k]), k))[:6]:
                variants = vc[vc["key"] == key]
                examples.append(" / ".join(f"{ctx.show(r)} ×{fmt_int(c)}" for r, c in zip(variants["raw"], variants["c"])))
            pct = 100 * bad_local.sum() / n
            ctx.add("DQ-S01", col, mask=full(bad_local), severity=scale_severity(pct, [(5, "medium"), (0, "low")]),
                    summary=f"{fmt_int(len(multi))} values are written in more than one way; {fmt_int(bad_local.sum())} rows use a minority spelling.",
                    extra={"variants": examples},
                    sql=f"SELECT LOWER(TRIM({sql_col})) AS value_key, COUNT(DISTINCT {sql_col}) AS spellings FROM {sql_tab} GROUP BY LOWER(TRIM({sql_col})) HAVING COUNT(DISTINCT {sql_col}) > 1;")
    if ctype == "text" and not is_identifier and 3 <= distinct_vals <= T["typo_max_distinct"] and nvalid >= 20:
        nk_ = ucall(vals, norm_text)
        counts = sorted_counts(nk_.tolist())
        cnt = dict(counts)
        frequent = [k for k, c in counts if len(k) >= 3]
        pairs = []
        for v, c in counts:
            if len(v) < 4 or len(v) > 40:
                continue
            best, best_d = None, 99
            limit = 1 if len(v) <= 7 else 2
            for u in frequent:
                if cnt[u] < T["typo_freq_ratio"] * c or u == v or abs(len(u) - len(v)) > limit:
                    continue
                d = osa_distance(v, u, limit)
                if d < best_d or (d == best_d and cnt[u] > cnt.get(best, 0)):
                    best, best_d = u, d
            if best is not None and best_d <= limit:
                pairs.append((v, best, c, cnt[best], best_d))
        if pairs:
            bad_local = nk_.isin({p[0] for p in pairs}).to_numpy()
            ctx.add("DQ-S02", col, mask=full(bad_local),
                    summary=f"{len(pairs)} rare values look like misspellings of common ones.",
                    extra={"variants": [f"{ctx.show(a)} ×{fmt_int(ca)} → {ctx.show(b)} ×{fmt_int(cb)}" for a, b, ca, cb, _ in pairs[:8]]})

    # ---------------- Distribution summaries
    top_items = sorted(vcounts.items(), key=lambda kv: (-kv[1], kv[0]))
    if ctx.redact:
        top_items = sorted_counts(ucall(vals, shape_of).tolist())
    top15 = top_items[:15]
    other_cnt = nvalid - sum(c for _, c in top15)
    profile["top_values"] = [{"value": clip_text(v, 60), "count": int(c), "pct": round(100 * c / n, 3)} for v, c in top15]
    profile["other_count"] = int(other_cnt)
    p = vcounts.to_numpy() / nvalid
    profile["balance"] = round(float(-(p * np.log(p)).sum() / math.log(distinct_vals)), 4) if distinct_vals > 1 else 0.0
    profile["shapes"] = dict(sorted_counts(ucall(vals_lat, shape_of).tolist())[:8])
    if distinct_vals == 1:
        card = "constant"
    elif distinct_vals == 2:
        card = "binary"
    elif distinct_ratio >= 0.999 and distinct_vals == nvalid:
        card = "unique"
    elif distinct_vals <= 20:
        card = "low"
    elif distinct_ratio <= 0.05:
        card = "medium"
    else:
        card = "high"
    profile["cardinality"] = card
    profile["duplicate_values"] = int(nvalid - distinct_vals)
    return profile, numbers, dates_full, calendar, is_categorical, is_identifier


# --------------------------------------------------------------------------- #
# Table analysis
# --------------------------------------------------------------------------- #
def candidate_keys(df, max_cols=12):
    n = len(df)
    singles = [c for c in df.columns if n and df[c].notna().all() and df[c].is_unique]
    pairs = []
    if not singles and n:
        complete = [c for c in df.columns if df[c].notna().all()]
        ranked = sorted(complete, key=lambda c: (-df[c].nunique(), list(df.columns).index(c)))[:max_cols]
        for i, a in enumerate(ranked):
            for b in ranked[i + 1:]:
                if not df[[a, b]].duplicated().any():
                    pairs.append([a, b])
                if len(pairs) >= 5:
                    break
            if len(pairs) >= 5:
                break
    return singles, pairs


def analyze_table(name, df, tags, meta, cfg, as_of, redact):
    T = THRESHOLDS
    ignore = set(cfg.get("ignore_columns", []))
    df = df[[c for c in df.columns if c not in ignore]]
    rows_total = len(df)
    sampled = False
    if len(df) > T["max_rows"]:
        df = df.sample(n=T["max_rows"], random_state=42).sort_index()
        sampled = True
    df = df.reset_index(drop=True)
    n = len(df)
    ctx = Ctx(name, n, df, T["sample_rows"], T["sample_values"], redact)
    pk = [c for c in cfg.get("primary_key", []) if c in df.columns]
    optional = set(cfg.get("optional_columns", []))

    if n == 0:
        ctx.add("DQ-T01", None, affected=0, summary="The table has no data rows.")
    if meta.get("malformed"):
        mal = meta["malformed"]
        ctx.add("DQ-T02", None, affected=len(mal),
                summary=f"{fmt_int(len(mal))} rows have the wrong number of fields and were left out.",
                extra={"rows": [m["row"] for m in mal[:T["sample_rows"]]],
                       "values": [f"row {m['row']}: {m['fields']} fields, expected {m['expected']}" for m in mal[:T["sample_values"]]]})
        ctx.findings[-1]["affected_pct"] = round(100 * len(mal) / (n + len(mal)), 4)
    quoting = [h for h in meta.get("header_issues", []) if "quoting" in h["problem"]]
    for h in meta.get("header_issues", []):
        if "quoting" in h["problem"]:
            continue
        sev = "low" if "leading" in h["problem"] else "medium"
        ctx.add("DQ-T03", h["column"], affected=0, severity=sev,
                summary=f"Column name problem: {h['problem']} (raw name '{h['raw']}').", values=[h["raw"]])
    if quoting:
        ctx.add("DQ-T03", None, affected=0, severity="low",
                summary=f"{len(quoting)} column names contain spaces or special characters and need quoting in SQL.",
                values=[h["raw"] for h in quoting])

    profiles, numbers, dates, calendars, categorical, identifiers = [], {}, {}, {}, [], []
    missing_cols = {}
    for pos, col in enumerate(df.columns, 1):
        prof, nums, dts, cal, is_cat, is_id = analyze_column(
            ctx, col, df[col], tags.get(col) if tags else None, cfg, as_of, set(pk), cfg.get("placeholder_tokens", []))
        prof["position"] = pos
        if col in optional:
            for f in ctx.findings:
                if f["column"] == col and f["category"] == "completeness" and f["severity"] != "info":
                    f["severity"] = "info"
                    f["summary"] += " Column is declared optional."
            prof["optional"] = True
        profiles.append(prof)
        if nums is not None:
            numbers[col] = nums
        if dts is not None:
            dates[col], calendars[col] = dts, cal
        if is_cat:
            categorical.append(col)
        if is_id:
            identifiers.append(col)
        s = df[col]
        mc = s.isna().to_numpy()
        missing_cols[col] = mc | (ctx.masks.get((col, "completeness"), np.zeros(n, bool)) & ~mc)

    singles, pairs = candidate_keys(df) if n else ([], [])
    if n:
        # Empty rows
        all_missing = np.ones(n, dtype=bool)
        for col in df.columns:
            all_missing &= missing_cols[col]
        if all_missing.any():
            ctx.add("DQ-T04", None, mask=all_missing, summary=f"{fmt_int(all_missing.sum())} rows have no value in any column.",
                    values=[], mask_column=ROW)
        # Duplicates
        filled = df.fillna("\u0000")
        dup = filled.duplicated(keep="first").to_numpy()
        if dup.any():
            pct = 100 * dup.sum() / n
            cols_sql = ", ".join(q(c) for c in df.columns[:30]) + (", ..." if len(df.columns) > 30 else "")
            ctx.add("DQ-U01", None, mask=dup, severity=scale_severity(pct, [(1, "high"), (0, "medium")]),
                    summary=f"{fmt_int(dup.sum())} rows ({fmt_pct(pct)}) are exact copies of an earlier row.",
                    sql=f"SELECT {cols_sql}, COUNT(*) AS copies FROM {q(name)} GROUP BY {cols_sql} HAVING COUNT(*) > 1;",
                    values=[], mask_column=ROW)
        normed = df.apply(lambda s: ucall(s.fillna("").astype(str), norm_text))
        ndup = normed.duplicated(keep="first").to_numpy() & ~dup
        if ndup.any():
            ctx.add("DQ-U02", None, mask=ndup,
                    summary=f"{fmt_int(ndup.sum())} rows repeat an earlier row once spaces, case and Arabic letter forms are ignored.",
                    values=[], mask_column=ROW)
        id_cols = [c for c in identifiers if df[c].nunique(dropna=True) >= T["identifier_unique_ratio"] * max(1, df[c].notna().sum())]
        rest = [c for c in df.columns if c not in id_cols]
        if id_cols and len(rest) >= 2:
            idup = normed[rest].duplicated(keep="first").to_numpy() & ~dup & ~ndup
            if idup.any():
                pct = 100 * idup.sum() / n
                rest_sql = ", ".join(q(c) for c in rest[:30])
                ctx.add("DQ-U05", None, mask=idup, severity=scale_severity(pct, [(5, "high"), (0, "medium")]),
                        summary=f"{fmt_int(idup.sum())} rows ({fmt_pct(pct)}) match an earlier row in every column except {', '.join(id_cols)}.",
                        related=", ".join(id_cols), values=[], mask_column=ROW,
                        sql=f"SELECT {rest_sql}, COUNT(*) AS copies FROM {q(name)} GROUP BY {rest_sql} HAVING COUNT(*) > 1;")
        if pk:
            key = df[pk].fillna("\u0000")
            kd = key.duplicated(keep=False).to_numpy()
            if kd.any():
                kcols = ", ".join(q(c) for c in pk)
                ctx.add("DQ-U03", ", ".join(pk), mask=kd, severity="critical",
                        summary=f"Declared key ({', '.join(pk)}) repeats: {fmt_int(kd.sum())} rows share a key value with another row.",
                        sql=f"SELECT {kcols}, COUNT(*) FROM {q(name)} GROUP BY {kcols} HAVING COUNT(*) > 1;",
                        values=[" | ".join("(null)" if v is None else str(v) for v in r) for r in df.loc[kd, pk].head(5).itertuples(index=False)],
                        mask_column=pk[0])
        for col in identifiers:
            if col in pk:
                continue
            s = df[col]
            nonnull = s.dropna()
            if len(nonnull) < 10 or not (RE_ID_NAME.search(col) or col.lower() == "id"):
                continue
            ratio = nonnull.nunique() / len(nonnull)
            if T["key_dup_min_unique_ratio"] <= ratio < 1:
                kd = s.duplicated(keep=False).to_numpy() & s.notna().to_numpy()
                ctx.add("DQ-U03", col, mask=kd, severity="high",
                        summary=f"Identifier column is {fmt_pct(100 * ratio)} unique; {fmt_int(kd.sum())} rows share a value.",
                        sql=f"SELECT {q(col)}, COUNT(*) FROM {q(name)} WHERE {q(col)} IS NOT NULL GROUP BY {q(col)} HAVING COUNT(*) > 1;")
        if not pk and not singles and not pairs:
            ctx.add("DQ-U04", None, affected=0, summary="No declared key, no column with complete unique values, and no unique pair of columns.")

        # Dependencies between categorical columns
        packed_cols = {p["name"] for p in profiles if p.get("packed")}
        cands = [c for c in categorical if c not in packed_cols and 2 <= df[c].nunique() <= T["fd_max_distinct"] and df[c].notna().mean() >= 0.5][: T["fd_max_columns"]]
        codes = {c: pd.factorize(ucall(df[c].fillna("").astype(str), norm_text).where(df[c].notna(), None), sort=True)[0] for c in cands}
        fd_found = []
        for a in cands:
            for b in cands:
                if a == b:
                    continue
                ca, cb = codes[a], codes[b]
                both = (ca >= 0) & (cb >= 0)
                if both.sum() < 20:
                    continue
                na_, nb_ = ca.max() + 1, cb.max() + 1
                if na_ < nb_ or na_ < 3:
                    continue
                bcounts = np.bincount(cb[both], minlength=nb_)
                if bcounts.max() / both.sum() > T["fd_max_dominant_share"]:
                    continue
                combo = ca[both].astype(np.int64) * nb_ + cb[both]
                cc = np.bincount(combo, minlength=na_ * nb_).reshape(na_, nb_)
                arow = cc.sum(axis=1)
                strength = cc.max(axis=1).sum() / both.sum()
                if not (T["fd_min_strength"] <= strength < 1) or (arow >= 3).sum() < 3:
                    continue
                mode_b = cc.argmax(axis=1)
                eligible = arow >= 3
                viol = np.zeros(n, dtype=bool)
                idx = np.flatnonzero(both)
                viol[idx[eligible[ca[both]] & (cb[both] != mode_b[ca[both]])]] = True
                if viol.any():
                    fd_found.append((int(viol.sum()), a, b, strength, viol))
        fd_found.sort(key=lambda t: (-t[0], t[1], t[2]))
        seen_pairs = set()
        for cnt, a, b, strength, viol in fd_found[:30]:
            if (b, a) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            ex = df.loc[viol, [a, b]].head(5)
            ctx.add("DQ-S04", b, mask=viol, related=a,
                    summary=f"'{a}' determines '{b}' in {fmt_pct(100 * strength)} of rows; {fmt_int(cnt)} rows break the pattern.",
                    values=[f"{ctx.show(r[0])} → {ctx.show(r[1])}" for r in ex.itertuples(index=False)],
                    metric={"strength": strength},
                    sql=f"SELECT {q(a)}, COUNT(DISTINCT {q(b)}) AS {q(b + '_values')} FROM {q(name)} GROUP BY {q(a)} HAVING COUNT(DISTINCT {q(b)}) > 1;")

        # Date order within the same calendar, and Hijri-Gregorian agreement
        dcols = sorted(dates.keys(), key=list(df.columns).index)
        for i, a in enumerate(dcols):
            for b in dcols[i + 1:]:
                da, db = dates[a], dates[b]
                both = ~np.isnat(da) & ~np.isnat(db)
                m = int(both.sum())
                if m < T["date_order_min_rows"]:
                    continue
                if calendars[a] != calendars[b]:
                    diff = np.abs((da[both] - db[both]).astype(np.int64))
                    tol = T["calendar_pair_tolerance_days"]
                    agree = (diff <= tol).mean()
                    if agree >= T["calendar_pair_min_share"]:
                        viol = np.zeros(n, dtype=bool)
                        viol[np.flatnonzero(both)[diff > tol]] = True
                        hcol, gcol = (a, b) if calendars[a] == "hijri" else (b, a)
                        if viol.any():
                            ctx.add("DQ-S09", hcol, mask=viol, related=gcol,
                                    summary=f"'{hcol}' and '{gcol}' give the same day (±{tol} days) in {fmt_pct(100 * agree)} of rows; {fmt_int(viol.sum())} rows differ by more.",
                                    values=[f"{ctx.show(x)} ≠ {ctx.show(y)}" for x, y in df.loc[viol, [hcol, gcol]].head(5).itertuples(index=False)],
                                    metric={"agreement": agree})
                    continue
                le = (da[both] <= db[both]).mean()
                ge = (da[both] >= db[both]).mean()
                if (da[both] == db[both]).all():
                    continue
                for first, second, sh in ((a, b, le), (b, a, ge)):
                    if T["date_order_min_share"] <= sh < 1:
                        viol = both & (dates[first] > dates[second])
                        ctx.add("DQ-S05", second, mask=viol, related=first,
                                summary=f"'{first}' is on or before '{second}' in {fmt_pct(100 * sh)} of rows; {fmt_int(viol.sum())} rows have it after.",
                                values=[f"{ctx.show(x)} > {ctx.show(y)}" for x, y in df.loc[viol, [first, second]].head(5).itertuples(index=False)],
                                sql=f"SELECT * FROM {q(name)} WHERE CAST({q(first)} AS DATE) > CAST({q(second)} AS DATE);")
                        break

        # Calculations between numeric columns
        calcs = []
        ncols = [c for c in df.columns if c in numbers and c not in identifiers][: T["calc_max_columns"]]
        if len(ncols) >= 3:
            rng = np.random.default_rng(42)
            sample = np.sort(rng.choice(n, size=min(n, 20000), replace=False))
            used = set()
            ops = [("×", lambda a, b: a * b, True), ("÷", lambda a, b: np.divide(a, b, out=np.full_like(a, np.nan), where=b != 0), False),
                   ("+", lambda a, b: a + b, True), ("−", lambda a, b: a - b, False)]
            # Pass 1 tries C = A × B and C = A + B, pass 2 tries ÷ and −, so the
            # natural form (total = quantity × price) wins over its rearrangements.
            for op_group in (ops[0::2], ops[1::2]):
              for c in ncols:
                vc_ = numbers[c]
                if np.nanstd(vc_) == 0:
                    continue
                for ia, a in enumerate(ncols):
                    for b in ncols:
                        if len({a, b, c}) < 3 or frozenset((a, b, c)) in used:
                            continue
                        for sym, fn, comm in op_group:
                            if comm and list(ncols).index(a) > list(ncols).index(b):
                                continue
                            va, vb = numbers[a][sample], numbers[b][sample]
                            if np.nanstd(va) == 0 or np.nanstd(vb) == 0:
                                continue
                            with np.errstate(all="ignore"):
                                pred = fn(va, vb)
                                tgt = vc_[sample]
                                okm = ~np.isnan(pred) & ~np.isnan(tgt)
                                if okm.sum() < 50:
                                    continue
                                close = np.abs(pred[okm] - tgt[okm]) <= T["calc_tolerance"] * np.maximum(np.abs(tgt[okm]), 1e-9) + 0.01
                            if close.mean() >= T["calc_min_share"]:
                                with np.errstate(all="ignore"):
                                    pf = fn(numbers[a], numbers[b])
                                    okf = ~np.isnan(pf) & ~np.isnan(vc_)
                                    bad = np.zeros(n, dtype=bool)
                                    idx = np.flatnonzero(okf)
                                    far = np.abs(pf[okf] - vc_[okf]) > T["calc_tolerance"] * np.maximum(np.abs(vc_[okf]), 1e-9) + 0.01
                                    bad[idx[far]] = True
                                share_full = 1 - far.mean() if len(far) else 1.0
                                expr = f"{c} = {a} {sym} {b}"
                                calcs.append({"expression": expr, "rows": int(okf.sum()), "holds_pct": round(100 * share_full, 3), "mismatches": int(bad.sum())})
                                used.add(frozenset((a, b, c)))
                                if bad.any():
                                    ctx.add("DQ-S07", c, mask=bad, related=f"{a} {sym} {b}",
                                            summary=f"'{expr}' holds in {fmt_pct(100 * share_full)} of rows; {fmt_int(bad.sum())} rows disagree by more than {T['calc_tolerance'] * 100:g}%.",
                                            values=[f"{ctx.show(r[0])} vs {ctx.show(r[1])} {sym} {ctx.show(r[2])}" for r in df.loc[bad, [c, a, b]].head(5).itertuples(index=False)])
                                break
        # Packed column first part vs another column
        packs = []
        text_cols = [p["name"] for p in profiles if p["type"] == "text"]
        for p in profiles:
            info = p.get("packed")
            if not info or info["first_part_distinct"] < 2:
                continue
            col, sep = p["name"], info["separator"]
            first = ucall(df[col].fillna("").astype(str), lambda u, s=sep: norm_text(u.str.split(s, n=1, regex=False).str[0]))
            has = ucall(df[col].fillna("").astype(str), lambda u, s=sep: u.str.contains(s, regex=False)).to_numpy(dtype=bool)
            for other in text_cols:
                if other == col:
                    continue
                o = ucall(df[other].fillna("").astype(str), norm_text)
                okm = has & df[other].notna().to_numpy()
                if okm.sum() < 20:
                    continue
                eq = (first.to_numpy() == o.to_numpy())
                share = eq[okm].mean()
                if share >= T["packed_match_min_share"]:
                    bad = okm & ~eq
                    packs.append({"column": col, "repeats": other, "match_pct": round(100 * share, 3), "mismatches": int(bad.sum())})
                    if bad.any():
                        ctx.add("DQ-S08", col, mask=bad, related=other,
                                summary=f"The part before '{sep.strip()}' repeats '{other}' in {fmt_pct(100 * share)} of rows; {fmt_int(bad.sum())} rows disagree.",
                                values=[f"{ctx.show(x)} vs {ctx.show(y)}" for x, y in df.loc[bad, [col, other]].head(5).itertuples(index=False)])
                    break
    else:
        calcs, packs = [], []

    return {"name": name, "ctx": ctx, "df": df, "profiles": profiles, "numbers": numbers, "dates": dates,
            "categorical": categorical, "identifiers": identifiers, "pk": pk, "rows_total": rows_total,
            "sampled": sampled, "meta": meta, "missing_cols": missing_cols, "candidate_keys": {"single": singles, "pairs": pairs},
            "calculations": calcs if n else [], "packed": packs if n else []}


COLUMN_WIDE_RULES = {"DQ-V02", "DQ-V06", "DQ-V09", "DQ-V12"}


def finalize_masks(t):
    """Masks used for scores, links and concentrations.

    Left out: completeness on mostly-empty or optional columns, and format findings that
    cover 95% or more of a column (a column-wide format choice, reported once as a finding).
    """
    ctx = t["ctx"]
    n = ctx.n
    valid = {p["name"]: p["valid"] for p in t["profiles"]}
    sparse = {p["name"] for p in t["profiles"] if p.get("optional") or (n and (p["nulls"] + p["placeholders"]) / n >= 0.95)}
    score = {}
    for f, key, m in ctx.fmasks:
        col = f["column"]
        if f["rule"] in COLUMN_WIDE_RULES and col in valid and valid[col] and f["affected_rows"] >= 0.95 * valid[col]:
            if f.get("scope") != "column":
                f["scope"] = "column"
                f["summary"] += " It covers the whole column, so it counts once here and not in the clean scores."
            continue
        if key[0] in sparse and key[1] == "completeness":
            continue
        score[key] = m.copy() if key not in score else (score[key] | m)
    t["score_masks"], t["sparse"] = score, sparse


# --------------------------------------------------------------------------- #
# Issue links, concentrations, missing patterns
# --------------------------------------------------------------------------- #
def flag_label(col, cat):
    return f"{col} · {cat}" if col != ROW else f"row · {cat}"


def correlations(t):
    T = THRESHOLDS
    ctx = t["ctx"]
    n = ctx.n
    flags = [(k, m) for k, m in t["score_masks"].items() if 5 <= m.sum() < n]
    flags.sort(key=lambda km: (-int(km[1].sum()), km[0][0], km[0][1]))
    flags = flags[: T["correlation_max_flags"]]
    labels = [flag_label(*k) for k, _ in flags]
    counts = [int(m.sum()) for _, m in flags]
    if len(flags) < 2:
        return {"flags": [{"label": l, "count": c} for l, c in zip(labels, counts)], "matrix_labels": [], "matrix": [], "pairs": []}
    X = np.column_stack([m for _, m in flags]).astype(np.float32)
    p = X.mean(axis=0)
    both = X.T @ X
    cov = both / n - np.outer(p, p)
    sd = np.sqrt(p * (1 - p))
    phi = cov / np.outer(sd, sd)
    np.fill_diagonal(phi, 1.0)
    pairs = []
    for i in range(len(flags)):
        for j in range(i + 1, len(flags)):
            if flags[i][0][0] == flags[j][0][0] and flags[i][0][0] != ROW:
                continue
            b = int(both[i, j])
            if phi[i, j] >= T["correlation_min_phi"] and b >= T["correlation_min_both"]:
                pairs.append({"a": labels[i], "b": labels[j], "phi": round(float(phi[i, j]), 4), "both": b,
                              "count_a": counts[i], "count_b": counts[j],
                              "p_b_given_a": round(b / counts[i], 4), "p_a_given_b": round(b / counts[j], 4),
                              "lift": round((b / n) / (p[i] * p[j]), 3) if p[i] * p[j] > 0 else None})
    pairs.sort(key=lambda d: (-d["phi"], -d["both"], d["a"], d["b"]))
    keep = list(range(min(len(labels), 20)))
    return {"flags": [{"label": l, "count": c} for l, c in zip(labels, counts)],
            "matrix_labels": [labels[i] for i in keep],
            "matrix": [[round(float(phi[i, j]), 3) for j in keep] for i in keep],
            "pairs": pairs[:40]}


def concentrations(t):
    T = THRESHOLDS
    ctx, df = t["ctx"], t["df"]
    n = ctx.n
    if n < 20:
        return []
    segs = []
    for c in t["categorical"]:
        k = df[c].nunique(dropna=True)
        if 2 <= k <= T["segment_max_distinct"]:
            segs.append((c, df[c].fillna("(missing)").astype(str).str.strip()))
    for c, dts in t["dates"].items():
        months = pd.Series(dts.astype("datetime64[M]").astype(str)).where(~np.isnat(dts), "(missing)")
        if 2 <= months.nunique() <= 60:
            segs.append((f"month({c})", months))
        elif months.nunique() > 60:
            yrs = pd.Series(dts.astype("datetime64[Y]").astype(str)).where(~np.isnat(dts), "(missing)")
            if 2 <= yrs.nunique() <= 60:
                segs.append((f"year({c})", yrs))
    min_size = max(20, int(0.01 * n))
    out = []
    for (col, cat), mask in sorted(t["score_masks"].items(), key=lambda kv: (kv[0][0], kv[0][1])):
        total = int(mask.sum())
        if total < T["concentration_min_affected"] or total == n:
            continue
        overall = total / n
        for seg_col, seg_vals in segs:
            base = seg_col.split("(")[-1].rstrip(")") if "(" in seg_col else seg_col
            if base == col:
                continue
            codes, uniques = pd.factorize(seg_vals, sort=True)
            size = np.bincount(codes, minlength=len(uniques))
            hit = np.bincount(codes[mask], minlength=len(uniques))
            impurity = np.minimum(hit, size - hit).sum() / n
            high = [i for i in range(len(uniques)) if size[i] >= 5 and hit[i] / size[i] >= 0.95]
            low = [i for i in range(len(uniques)) if size[i] >= 5 and hit[i] / size[i] <= 0.05]
            if impurity <= 0.02 and high and low and overall >= 0.05:
                hs, hh = int(size[high].sum()), int(hit[high].sum())
                out.append({"pattern": "determined", "issue": flag_label(col, cat), "segment_column": seg_col,
                            "segment_value": ", ".join(str(ctx.show(uniques[i]) if uniques[i] != "(missing)" else uniques[i]) for i in high[:6]),
                            "rows_in_segment": hs, "affected_in_segment": hh,
                            "rate_in_segment": round(100 * hh / hs, 3), "rate_overall": round(100 * overall, 3),
                            "lift": round(float((hh / hs) / overall), 3), "share_of_issue": round(100 * hh / total, 3)})
                continue
            for i, u in enumerate(uniques):
                if size[i] < min_size or hit[i] < T["concentration_min_affected"]:
                    continue
                rate = hit[i] / size[i]
                lift = rate / overall
                share = hit[i] / total
                if lift >= T["concentration_min_lift"] and rate >= T["concentration_min_rate"] and (share >= 0.10 or rate >= 0.5):
                    out.append({"pattern": "concentrated", "issue": flag_label(col, cat), "segment_column": seg_col,
                                "segment_value": ctx.show(u) if u != "(missing)" else u,
                                "rows_in_segment": int(size[i]), "affected_in_segment": int(hit[i]),
                                "rate_in_segment": round(100 * rate, 3), "rate_overall": round(100 * overall, 3),
                                "lift": round(float(lift), 3), "share_of_issue": round(100 * share, 3)})
    out.sort(key=lambda d: (-d["share_of_issue"], -d["lift"], d["issue"], d["segment_column"], str(d["segment_value"])))
    return out[:60]


def missing_patterns(t):
    n = t["ctx"].n
    cols = [(c, m) for c, m in t["missing_cols"].items() if m.any()]
    cols.sort(key=lambda cm: (-int(cm[1].sum()), cm[0]))
    cols = cols[:30]
    if not cols:
        return []
    M = np.column_stack([m for _, m in cols])
    anym = M.any(axis=1)
    keys = pd.Series([tuple(r) for r in M[anym]])
    vc = keys.value_counts()
    items = sorted(vc.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    names = [c for c, _ in cols]
    return [{"columns": [names[i] for i, v in enumerate(k) if v], "rows": int(c), "pct": round(100 * c / n, 3)} for k, c in items]


# --------------------------------------------------------------------------- #
# Signals: fixed list of headline statements, each included when it applies
# --------------------------------------------------------------------------- #
def build_signals(summary, columns, findings, corr, conc, keys, calcs, packs):
    sig = []
    by_rule = Counter(f["rule"] for f in findings)
    cr = summary["clean_row_pct"]
    sig.append({"level": "good" if cr >= 95 else ("medium" if cr >= 80 else "high"), "title": "Health",
                "text": f"{fmt_pct(summary['clean_cell_pct'])} of cells and {fmt_pct(cr)} of rows have no flagged problem."})
    sev = summary["findings_by_severity"]
    if sev["critical"] + sev["high"]:
        top = findings[0]
        sig.append({"level": "critical" if sev["critical"] else "high", "title": "Severity",
                    "text": (f"{sev['critical']} critical and {sev['high']} high findings" if sev["critical"] else f"{sev['high']} high findings")
                            + f". The first is on '{top['column'] or 'the table'}': {top['summary']}",
                    "column": top["column"]})
    scored = [c for c in columns if not c.get("sparse")]
    worst = sorted(scored, key=lambda c: (-c["flagged_pct"], c["position"]))
    if worst and worst[0]["flagged_pct"] > 0:
        w = worst[0]
        cats = [k for k in COLUMN_CATEGORIES if w["heat"].get(k, 0) > 0]
        sig.append({"level": "high" if w["flagged_pct"] >= 20 else "medium", "title": "Weakest column",
                    "text": f"'{w['name']}' has a problem in {fmt_pct(w['flagged_pct'])} of rows ({', '.join(cats)}).", "column": w["name"]})
    miss = sorted([c for c in columns if c["missing_pct"] > 0], key=lambda c: (-c["missing_pct"], c["position"]))
    if miss:
        heavy = [c for c in miss if c["missing_pct"] >= 50]
        sig.append({"level": "high" if heavy else "medium", "title": "Completeness",
                    "text": f"{len(miss)} of {len(columns)} columns have missing or placeholder values; most: "
                            + ", ".join(f"'{c['name']}' {fmt_pct(c['missing_pct'])}" for c in miss[:3]) + ".", "column": miss[0]["name"]})
    catch = sorted([c for c in columns if c.get("catch_all", 0) > 0], key=lambda c: (-c["catch_all"], c["position"]))
    if catch:
        sig.append({"level": "medium", "title": "Catch-all values",
                    "text": ", ".join(f"'{c['name']}' {fmt_int(c['catch_all'])} rows" for c in catch[:3]) + " hold 'other' or 'none' style values instead of a real value.",
                    "column": catch[0]["name"]})
    type_rules = ["DQ-V01", "DQ-V02", "DQ-V03", "DQ-V04", "DQ-V05", "DQ-V06", "DQ-V12"]
    tcols = sorted({f["column"] for f in findings if f["rule"] in type_rules and f["column"]})
    if tcols:
        hard = any(by_rule.get(r) for r in ["DQ-V01", "DQ-V03", "DQ-V04", "DQ-V05"])
        details = []
        for c in tcols[:4]:
            names = sorted({f["name"].lower() for f in findings if f["column"] == c and f["rule"] in type_rules})
            details.append(f"'{c}' ({', '.join(names)})")
        sig.append({"level": "high" if hard else "medium", "title": "Types and formats",
                    "text": f"{len(tcols)} columns need casting or format cleanup before SQL use: " + "; ".join(details) + ".", "column": tcols[0]})
    dup_rows = {r: sum(f["affected_rows"] for f in findings if f["rule"] == r) for r in ("DQ-U01", "DQ-U02", "DQ-U05")}
    if any(dup_rows.values()):
        parts = []
        if dup_rows["DQ-U01"]:
            parts.append(f"{fmt_int(dup_rows['DQ-U01'])} exact copies")
        if dup_rows["DQ-U02"]:
            parts.append(f"{fmt_int(dup_rows['DQ-U02'])} copies after normalizing text")
        if dup_rows["DQ-U05"]:
            idf = next(f for f in findings if f["rule"] == "DQ-U05")
            parts.append(f"{fmt_int(dup_rows['DQ-U05'])} copies that differ only in {idf['related']}")
        total = sum(dup_rows.values())
        sig.append({"level": "high" if 100 * total / max(1, summary["rows"]) >= 1 else "medium", "title": "Duplicates",
                    "text": "Repeated records: " + ", ".join(parts) + "."})
    cons_rules = ["DQ-S01", "DQ-S02", "DQ-V09", "DQ-V13"]
    ccols = sorted({f["column"] for f in findings if f["rule"] in cons_rules and f["column"]})
    if ccols:
        sig.append({"level": "medium", "title": "Same value, different text",
                    "text": f"{len(ccols)} columns store the same value in different ways (spaces, spelling, letter forms, script): "
                            + ", ".join(f"'{c}'" for c in ccols[:4]) + ".", "column": ccols[0]})
    xrules = [f for f in findings if f["rule"] in ("DQ-S04", "DQ-S05", "DQ-S07", "DQ-S08", "DQ-S09")]
    if xrules:
        f = xrules[0]
        sig.append({"level": "high" if f["severity"] in ("critical", "high") else "medium", "title": "Columns disagree",
                    "text": f"{len(xrules)} cross-column checks fail. Largest: {f['summary']}", "column": f["column"]})
    if conc:
        c = conc[0]
        txt = (f"'{c['issue']}' happens exactly when {c['segment_column']} is {c['segment_value']}."
               if c["pattern"] == "determined" else
               f"{fmt_pct(c['share_of_issue'])} of '{c['issue']}' sits where {c['segment_column']} = {c['segment_value']} ({fmt_pct(c['rate_in_segment'])} there vs {fmt_pct(c['rate_overall'])} overall).")
        sig.append({"level": "info", "title": "Where it concentrates", "text": txt})
    if corr.get("pairs"):
        p = corr["pairs"][0]
        sig.append({"level": "info", "title": "Problems that travel together",
                    "text": f"When '{p['a']}' has a problem, '{p['b']}' also has one in {fmt_pct(100 * p['p_b_given_a'])} of rows ({fmt_int(p['both'])} rows)."})
    if keys["single"]:
        sig.append({"level": "good", "title": "Key", "text": f"'{keys['single'][0]}' identifies every row (complete and unique)."})
    elif keys["pairs"]:
        sig.append({"level": "medium", "title": "Key", "text": f"No single column identifies a row; the pair ({', '.join(keys['pairs'][0])}) does."})
    else:
        sig.append({"level": "medium", "title": "Key", "text": "No column or pair of columns identifies a row."})
    for c in calcs[:1]:
        sig.append({"level": "good" if c["mismatches"] == 0 else "high", "title": "Calculation",
                    "text": f"'{c['expression']}' holds in {fmt_pct(c['holds_pct'])} of rows ({fmt_int(c['mismatches'])} mismatches)."})
    for p in packs[:1]:
        sig.append({"level": "good" if p["mismatches"] == 0 else "medium", "title": "Packed column",
                    "text": f"'{p['column']}' repeats '{p['repeats']}' before its separator in {fmt_pct(p['match_pct'])} of rows ({fmt_int(p['mismatches'])} disagree)."})
    for g in sig:
        g["text"] = singular_one(g["text"])
    return sig


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def fingerprint(df):
    h = hashlib.sha256()
    h.update(json.dumps(list(map(str, df.columns)), ensure_ascii=False).encode())
    if len(df):
        h.update(pd.util.hash_pandas_object(df.fillna("\u0000").astype(str), index=False).to_numpy().tobytes())
    return h.hexdigest()[:16]


def hist_counts(values, bins):
    if not values:
        return []
    arr = np.asarray(values, dtype=float)
    if len(arr) <= bins:
        return arr.tolist()
    edges = np.linspace(0, len(arr), bins + 1).astype(int)
    return [float(arr[edges[i]:edges[i + 1]].sum()) for i in range(bins)]


def build_report(t, cfg, as_of, dataset_name, notes, generated_at, redact):
    ctx = t["ctx"]
    n = ctx.n
    ncols = len(t["df"].columns)
    finalize_masks(t)
    sparse, score_masks = t["sparse"], t["score_masks"]
    col_masks = {}
    for (col, cat), m in score_masks.items():
        if col == ROW:
            continue
        for c in [x.strip() for x in col.split(",")]:
            col_masks[c] = m if c not in col_masks else (col_masks[c] | m)
    flagged_cells = sum(int(m.sum()) for m in col_masks.values())
    cells = n * ncols
    any_row = np.zeros(n, dtype=bool)
    issue_count = np.zeros(n, dtype=np.int64)
    for c, m in col_masks.items():
        issue_count += m
    for (c, cat), m in score_masks.items():
        if c == ROW:
            issue_count += m
    any_row = issue_count > 0
    miss_count = np.zeros(n, dtype=np.int64)
    for c, m in t["missing_cols"].items():
        miss_count += m
    def bucket(arr, top=5):
        return [int((arr == i).sum()) for i in range(top)] + [int((arr >= top).sum())]
    cat_row_pct = {}
    for cat in COLUMN_CATEGORIES:
        m = np.zeros(n, dtype=bool)
        for (c, k), mm in score_masks.items():
            if k == cat:
                m |= mm
        cat_row_pct[cat] = round(100 * m.sum() / n, 3) if n else 0
    malformed = len(t["meta"].get("malformed", []))
    cat_row_pct["structure"] = round(100 * malformed / (n + malformed), 3) if (n + malformed) else 0

    findings = sorted(ctx.findings, key=lambda f: (SEV_RANK[f["severity"]], -f["affected_rows"], f["column"] or "", f["rule"], f["summary"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F-{i:04d}"
        f["detail"] = detail_for(json_safe(f))
    columns = []
    for p in t["profiles"]:
        name = p["name"]
        heat = {}
        for cat in COLUMN_CATEGORIES:
            m = score_masks.get((name, cat))
            heat[cat] = round(100 * m.sum() / n, 3) if (m is not None and n) else 0
        fm = col_masks.get(name)
        fs = [f for f in findings if name in [x.strip() for x in (f["column"] or "").split(",")]]
        if "hist" in p:
            spark = {"kind": "hist", "values": p["hist"]["counts"]}
        elif "date_hist" in p:
            spark = {"kind": "hist", "values": hist_counts(p["date_hist"]["counts"], 24)}
        else:
            spark = {"kind": "bars", "values": [v["count"] for v in p.get("top_values", [])[:8]]}
        columns.append(dict(p, heat=heat, sparse=name in sparse,
                            column_wide=[f["name"] for f in fs if f.get("scope") == "column"],
                            flagged_pct=round(100 * fm.sum() / n, 3) if (fm is not None and n) else 0,
                            missing_pct=round(100 * (p["nulls"] + p["placeholders"]) / n, 3) if n else 0,
                            mismatch_pct=round(100 * p.get("mismatches", 0) / n, 3) if n else 0,
                            findings=len(fs), worst=(min((SEV_RANK[f["severity"]] for f in fs), default=None)),
                            spark=spark))
    for c in columns:
        c["worst"] = SEVERITIES[c["worst"]] if c["worst"] is not None else None
    sev = Counter(f["severity"] for f in findings)
    cats = Counter(f["category"] for f in findings)
    rules_hit = Counter(f["rule"] for f in findings)
    corr = correlations(t)
    conc = concentrations(t)
    summary = {
        "table": t["name"], "source": t["meta"].get("source"), "rows": n, "rows_total": t["rows_total"],
        "sampled": t["sampled"], "columns": ncols, "cells": cells, "encoding": t["meta"].get("encoding"),
        "delimiter": t["meta"].get("delimiter"), "primary_key": t["pk"],
        "clean_cell_pct": round(100 * (1 - flagged_cells / cells), 3) if cells else 100.0,
        "clean_row_pct": round(100 * (1 - any_row.sum() / n), 3) if n else 100.0,
        "flagged_cells": flagged_cells, "flagged_rows": int(any_row.sum()),
        "findings": len(findings),
        "findings_by_severity": {s: sev.get(s, 0) for s in SEVERITIES},
        "findings_by_category": {c: cats.get(c, 0) for c in CATEGORIES},
        "category_row_pct": cat_row_pct,
        "columns_with_findings": sum(1 for c in columns if c["findings"]),
        "columns_with_missing": sum(1 for c in columns if c["missing_pct"] > 0),
        "columns_with_type_issues": len({f["column"] for f in findings if f["rule"] in ("DQ-V01", "DQ-V02", "DQ-V03", "DQ-V04", "DQ-V05", "DQ-V06", "DQ-V12")}),
        "duplicate_rows": sum(f["affected_rows"] for f in findings if f["rule"] in ("DQ-U01", "DQ-U02", "DQ-U05")),
        "sparse_columns": sorted(sparse),
        "types": dict(sorted_counts([c["type"] for c in columns])),
        "row_missing_hist": bucket(miss_count),
        "row_issue_hist": bucket(issue_count),
        "candidate_keys": t["candidate_keys"],
        "calculations": t["calculations"], "packed": t["packed"],
        "rules_triggered": len(rules_hit), "fingerprint": fingerprint(t["df"]),
    }
    report = {
        "meta": {"dataset": dataset_name, "tool_version": TOOL_VERSION, "rules_version": RULES_VERSION,
                 "generated_at": generated_at, "as_of": as_of.isoformat(), "redacted": bool(redact),
                 "thresholds": THRESHOLDS, "notes": notes},
        "summary": summary,
        "signals": build_signals(summary, columns, findings, corr, conc, t["candidate_keys"], t["calculations"], t["packed"]),
        "columns": columns,
        "findings": findings,
        "correlation": corr,
        "concentrations": conc,
        "missing_patterns": missing_patterns(t),
        "rules": [dict(RULE_INFO[r[0]], findings=rules_hit.get(r[0], 0)) for r in RULES],
    }
    return json_safe(report)


def _q(v):
    return "'" + str(v) + "'"


def detail_for(f):
    """One short line for the dashboard. It does not repeat the row count or the column name."""
    r, m, ev = f["rule"], f.get("metric") or {}, f.get("evidence") or {}
    vals = ev.get("values") or []
    rel = f.get("related")
    if r == "DQ-T01":
        return "The table has no rows."
    if r == "DQ-T02":
        return "These rows have the wrong number of fields. The other checks skip them."
    if r == "DQ-T03":
        if f["column"] is None:
            return "These column names need quotes in SQL."
        raw = (ev.get("values") or [""])[0]
        texts = {"blank or auto-generated name": "The column has no name in the file." if not str(raw).strip() else f"Auto-generated name '{raw}'.",
                 "leading or trailing spaces in name": f"Spaces at the start or end of the name '{raw}'.",
                 "duplicated name": f"Another column has the same name '{raw}'."}
        for k, v in texts.items():
            if k in f["summary"]:
                return v
        return "This column name needs quotes in SQL."
    if r == "DQ-T04":
        return "All values in the row are empty."
    if r == "DQ-C01":
        return "Empty cells." + (" The column is almost all empty." if f["affected_pct"] >= 95 else "")
    if r == "DQ-C02":
        return "All values are empty or placeholders."
    if r == "DQ-C03":
        toks = m.get("tokens") or {}
        if len(toks) == 1:
            k = next(iter(toks))
            return "Cells with only spaces." if k == "(whitespace)" else f"Placeholder value {_q(k)}."
        return "Placeholders: " + ", ".join(("spaces only" if k == "(whitespace)" else _q(k)) + f" ({v:,})" for k, v in list(toks.items())[:4]) + "."
    if r == "DQ-C04":
        return "Catch-all values, for example " + ", ".join(_q(v) for v in vals[:3]) + "."
    if r == "DQ-V01":
        other = m.get("other_types") or {}
        kinds = ", ".join(f"{k} ({v:,})" for k, v in sorted(other.items(), key=lambda kv: -kv[1]))
        return f"Values that are not {m.get('main_type', 'the main type')}: {kinds}."
    if r == "DQ-V02":
        return "Numbers stored as text with separators, currency or %."
    if r == "DQ-V03":
        fm = list((m.get("formats") or {}).keys())
        return f"{len(fm)} date formats. The main format is {fm[0]}." if fm else "More than one date format."
    if r == "DQ-V04":
        return "Dates that do not exist on the calendar."
    if r == "DQ-V05":
        mm = re.search(r"use the (\w+) calendar", f["summary"])
        return f"Dates in the {mm.group(1)} calendar." if mm else "Dates in the other calendar."
    if r == "DQ-V06":
        return "Arabic-Indic digits."
    if r == "DQ-V07":
        return "Hidden characters: " + ", ".join(list((m.get("characters") or {}).keys())[:4]) + "."
    if r == "DQ-V08":
        return "Damaged text encoding."
    if r == "DQ-V09":
        return "Spaces at the start or end of the value."
    if r == "DQ-V10":
        return f"The shape is not the main shape {_q(m.get('main_shape', ''))}."
    if r == "DQ-V11":
        return "Not a valid email format."
    if r == "DQ-V12":
        return "Numbers or dates stored in text cells."
    if r == "DQ-V13":
        return "The same category in Arabic and in Latin letters."
    if r == "DQ-V14":
        return f["summary"]
    if r == "DQ-U01":
        return "Exact copy of an earlier row."
    if r == "DQ-U02":
        return "Copy of an earlier row when case and spaces are ignored."
    if r == "DQ-U03":
        return "Key values repeat." if f["severity"] == "critical" else "Identifier values repeat."
    if r == "DQ-U04":
        return "No column or pair of columns identifies a row."
    if r == "DQ-U05":
        return f"Copy of an earlier row in all columns except {rel}."
    if r == "DQ-S01":
        return "The same value with different spelling, case or spaces."
    if r == "DQ-S02":
        return "Rare values that look like typos of common values."
    if r == "DQ-S03":
        return f"The column has one value: {_q(vals[0]) if vals else ''}."
    if r == "DQ-S04":
        return f"{_q(rel)} sets {_q(f['column'])} in most rows. These rows do not follow it."
    if r == "DQ-S05":
        return f"{_q(rel)} is after {_q(f['column'])}."
    if r == "DQ-S06":
        return "One value is 99% or more of the column. These rows have other values."
    if r == "DQ-S07":
        return f"{_q(f['column'])} is not equal to {rel}."
    if r == "DQ-S08":
        return f"The first part does not match {_q(rel)}."
    if r == "DQ-S09":
        return f"The date is more than 3 days from {_q(rel)}."
    if r == "DQ-P01":
        lo, hi = m.get("lower_fence"), m.get("upper_fence")
        if lo is None:
            return "Outliers."
        nf = lambda x: f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.4g}"
        mm = re.search(r"\(([\d,]+) above, ([\d,]+) below\)", f["summary"])
        above, below = (int(mm.group(1).replace(",", "")), int(mm.group(2).replace(",", ""))) if mm else (1, 1)
        if below == 0:
            return f"Above {nf(hi)}."
        if above == 0:
            return f"Below {nf(lo)}."
        return f"Below {nf(lo)} or above {nf(hi)}."
    if r == "DQ-P02":
        return "Negative values."
    if r == "DQ-P03":
        tail = f["summary"].split(": ", 1)[-1]
        return "Placeholder values: " + tail
    if r == "DQ-P04":
        parts = []
        if m.get("future"):
            parts.append(f"after {m.get('as_of')}")
        if m.get("before_1900"):
            parts.append("before 1900")
        return "Dates " + " or ".join(parts) + "."
    if r == "DQ-P05":
        mm = re.search(r"exactly (\d+) characters", f["summary"])
        return f"Values end at exactly {mm.group(1)} characters." if mm else "Possible truncation."
    if r == "DQ-P06":
        return "Zero values."
    return f["summary"]


def write_findings_csv(report, path):
    cols = ["id", "severity", "rule", "name", "category", "column", "related",
            "affected_rows", "affected_pct", "summary", "sample_values", "sample_rows", "sql"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in report["findings"]:
            w.writerow([f["id"], f["severity"], f["rule"], f["name"], f["category"], f["column"] or "",
                        f["related"] or "", f["affected_rows"], f["affected_pct"], f["summary"],
                        " | ".join(str(v) for v in f["evidence"].get("values", [])),
                        " ".join(str(r) for r in f["evidence"].get("rows", [])), f["sql"] or ""])


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_dashboard(report, template_path, out_path, fragment=False):
    """Fill the fixed template with the report. The layout never depends on the data."""
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = template.replace("/*__REPORT_JSON__*/null", data)
    page = page.replace("__PAGE_TITLE__", html_escape(f"{report['meta']['dataset']} data quality"))
    if fragment:
        head = re.search(r"<!--HEAD-->(.*?)<!--/HEAD-->", page, flags=re.S).group(1)
        body = re.search(r"<!--BODY-->(.*?)<!--/BODY-->", page, flags=re.S).group(1)
        page = head.strip() + "\n" + body.strip() + "\n"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deep data quality audit of one table, with a fixed rule catalog and dashboard.")
    ap.add_argument("input", nargs="?", help="One table file: csv, tsv, txt (or .gz), xlsx, parquet, json, jsonl, sqlite.")
    ap.add_argument("--out", default="dq_output", help="Output folder (default: dq_output).")
    ap.add_argument("--config", help="Optional JSON config: key, optional and ignored columns, placeholders, thresholds.")
    ap.add_argument("--name", help="Dataset name shown on the dashboard.")
    ap.add_argument("--sheet", help="Excel sheet to audit (default: the first sheet).")
    ap.add_argument("--table", help="Table to audit in a SQLite file or with --db-url.")
    ap.add_argument("--as-of", help="Reference date for future-date checks, YYYY-MM-DD (default: today).")
    ap.add_argument("--redact", action="store_true", help="Show value shapes (9999/AAAA) instead of real values.")
    ap.add_argument("--db-url", help="SQLAlchemy URL to read the table from a database.")
    ap.add_argument("--query", help="SQL query whose result is audited (with --db-url).")
    ap.add_argument("--max-rows", type=int, help="Sample tables above this many rows (fixed seed).")
    ap.add_argument("--fixed-time", help="Pin generated_at for repeatable output.")
    ap.add_argument("--fragment", action="store_true", help="Write the dashboard without the <html> wrapper.")
    args = ap.parse_args(argv)

    cfg = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    THRESHOLDS.update(cfg.get("thresholds", {}))
    if args.max_rows:
        THRESHOLDS["max_rows"] = args.max_rows
    as_of = dt.date.fromisoformat(args.as_of or cfg.get("as_of") or dt.date.today().isoformat())
    redact = args.redact or bool(cfg.get("redact", False))
    notes = []
    path = args.input or cfg.get("input")
    if args.db_url:
        name, frame, tags, meta = load_database(args.db_url, args.table or cfg.get("table"), args.query, args.name)
    elif path:
        if not os.path.exists(path):
            sys.exit(f"Input not found: {path}")
        if os.path.isdir(path):
            sys.exit("Give one table file, not a folder. This skill audits one table per run.")
        name, frame, tags, meta = load_table(path, args.sheet or cfg.get("sheet"), args.table or cfg.get("table"), notes)
    else:
        ap.error("give one input file, or --db-url with --table or --query")

    t = analyze_table(name, frame, tags, meta, cfg, as_of, redact)
    if t["sampled"]:
        notes.append(f"{fmt_int(t['rows_total'])} rows; checks ran on a fixed sample of {fmt_int(THRESHOLDS['max_rows'])}.")
    dataset_name = args.name or cfg.get("dataset_name") or name
    generated_at = args.fixed_time or dt.datetime.now().replace(microsecond=0).isoformat()
    report = build_report(t, cfg, as_of, dataset_name, notes, generated_at, redact)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "dq_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    write_findings_csv(report, os.path.join(args.out, "dq_findings.csv"))
    here = os.path.dirname(os.path.abspath(__file__))
    render_dashboard(report, os.path.join(here, "..", "assets", "dashboard_template.html"),
                     os.path.join(args.out, "dq_dashboard.html"), fragment=args.fragment)

    s = report["summary"]
    print(f"Dataset: {dataset_name} ({s['source']})")
    print(f"Rows {fmt_int(s['rows'])} · columns {s['columns']} · findings {s['findings']} "
          f"(critical {s['findings_by_severity']['critical']}, high {s['findings_by_severity']['high']}, "
          f"medium {s['findings_by_severity']['medium']}, low {s['findings_by_severity']['low']})")
    empty = sum(c["nulls"] + c["placeholders"] for c in report["columns"])
    print(f"Duplicate rows {100 * min(s['rows'], s['duplicate_rows']) / max(s['rows'], 1):.2f}% · "
          f"empty cells {100 * empty / max(s['cells'], 1):.2f}%")
    print("Top findings:")
    for f in report["findings"][:15]:
        print(f"  {f['id']} [{f['severity']}] {f['rule']} {f['column'] or '(table)'}: {f['summary']}")
    for n_ in notes:
        print("Note:", n_)
    print(f"Wrote {os.path.join(args.out, 'dq_report.json')}, dq_findings.csv, dq_dashboard.html")


if __name__ == "__main__":
    main()
