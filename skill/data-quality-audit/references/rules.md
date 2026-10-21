# Rule catalog

Rules version 2026.10-single-table. Every run applies all 42 rules to one table, in this order. IDs never change meaning; a new check gets a new ID.

Severity scales use the share of the table's rows affected.

## Structure

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-T01 | Empty table | critical | The table has a header but no data rows. | Nothing downstream can use it. |
| DQ-T02 | Malformed rows | high | Rows whose field count differs from the header (broken quoting or delimiters). They are left out of every other check. | Loaders reject or shift these rows, so values land in the wrong columns. |
| DQ-T03 | Problem column names | low to medium | Blank, duplicated or auto-generated names, names with leading or trailing spaces, or names that need quoting in SQL. | Queries break or silently hit the wrong column. |
| DQ-T04 | Empty rows | medium | Rows where every value is missing or a placeholder. | Row counts are inflated by records that carry nothing. |

## Completeness

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-C01 | Missing values | scale: <5% low, 5-20% medium, 20-95% high, >=95% medium (mostly-empty column); critical on keys | True nulls (empty cells). | Aggregates skip nulls and filters on the column miss these rows. |
| DQ-C02 | Empty column | high | Every value in the column is missing or a placeholder. | The column carries no information. |
| DQ-C03 | Placeholder missing values | scale: <20% medium, >=20% high | Text that stands for missing: 'NULL', 'N/A', '-', 'unknown', 'غير متوفر', whitespace only, and similar. | IS NULL does not catch them, so completeness looks better than it is. |
| DQ-C04 | Catch-all values | scale: <5% low, >=5% medium | 'Other', 'أخرى', 'بدون' and similar, alone or after a prefix such as 'حي/أخرى' or 'الرياض/أخرى'. | The real value was not captured; these rows cannot be grouped or matched. |

## Validity

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-V01 | Type mismatch | scale: <5% medium, >=5% high | Values whose type differs from the column's main type (text in a numeric column, numbers in a date column). | CAST fails or returns NULL for these rows. |
| DQ-V02 | Numbers stored as formatted text | medium | Numbers written with thousand separators, currency, percent signs or accounting brackets. | They do not sum or cast to numeric without cleaning. |
| DQ-V03 | Mixed date formats | high | More than one date format in one column, including DD/MM and MM/DD in the same column. | Parsing with one format misreads or drops the others. |
| DQ-V04 | Invalid dates | high | Values shaped like dates that do not exist on their calendar (month 13, 30 February, Hijri day 31). | Date casts fail for these rows. |
| DQ-V05 | Mixed calendars | high | Hijri dates inside a Gregorian column, or Gregorian dates inside a Hijri column. | Durations and ranges are off by about 580 years for these rows. |
| DQ-V06 | Arabic-Indic digits | medium | Digits such as ١٢٣ instead of 123. | Numeric casts and equality checks against Latin digits fail. |
| DQ-V07 | Hidden characters | medium | Control characters, zero-width marks, direction marks or non-breaking spaces. | Values that look equal do not compare equal. |
| DQ-V08 | Encoding errors | high | Replacement characters or mojibake such as 'Ø§Ù„' (UTF-8 read with the wrong encoding). | The original text is damaged. |
| DQ-V09 | Leading or trailing spaces | scale: <5% low, >=5% medium; medium on identifiers | Values with spaces before or after the text. | Equality filters, GROUP BY and joins treat them as different values. |
| DQ-V10 | Inconsistent format | medium | Identifier-like values whose shape differs from the main shape (length, letters, separators). | Lookups on these values fail or match the wrong record. |
| DQ-V11 | Invalid email | medium | Values in an email column that are not shaped like an email address. | Messages and identity matching fail. |
| DQ-V12 | Mixed storage types | medium | Excel or SQLite cells where numbers or dates are stored as text next to real numbers or dates. | Sorting, sums and casts treat these cells differently. |
| DQ-V13 | Mixed scripts | scale: <5% low, >=5% medium | A category column where some values are written in Arabic and others in Latin letters. | The same category appears twice, once per language. |
| DQ-V14 | Packed or prefixed values | low | Values that combine two fields with a separator ('الرياض/النرجس') or repeat the same prefix ('حي/...'). | Filtering and grouping need string splitting first. |

## Uniqueness

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-U01 | Duplicate rows | scale: <1% medium, >=1% high | Rows identical to an earlier row in every column. | Counts and sums are inflated. |
| DQ-U02 | Near-duplicate rows | medium | Rows identical to an earlier row after trimming spaces and ignoring case and Arabic letter forms. | The same record is counted twice under different spellings. |
| DQ-U03 | Duplicate keys | critical on declared keys, high on inferred keys | Repeated values in a primary key or identifier column. | The key does not identify one row. |
| DQ-U04 | No unique key | low | No declared key, no column with complete unique values, and no pair of columns that is unique together. | Rows cannot be addressed or deduplicated reliably. |
| DQ-U05 | Duplicates except the ID | scale: <5% medium, >=5% high | Rows identical to an earlier row in every column except identifier columns. | The same event may be stored twice under different reference numbers. |

## Consistency

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-S01 | Spelling variants | scale: <5% low, >=5% medium | The same value written with different case, spacing or Arabic letter forms (أ/ا, ة/ه, ى/ي). | GROUP BY splits one category into several. |
| DQ-S02 | Probable typos | low | Rare values one or two edits away from a much more common value in the same column. | Categories are split and filters miss rows. |
| DQ-S03 | Constant column | low | Only one distinct value in the whole column. | Usually a default that was never set. |
| DQ-S04 | Dependency violations | medium | Column A almost always determines column B (for example city to region), except for some rows. | Rollups by B disagree with rollups by A. |
| DQ-S05 | Date order violations | high | One date column is almost always on or before another (start before end), except for some rows. | Durations come out negative. |
| DQ-S06 | Near-constant column | low | One value covers 99% or more of the column; the remaining rows are listed. | The rare values are often entry mistakes, or the column is a default. |
| DQ-S07 | Calculation mismatches | high | A numeric column that equals another two combined (A × B, A ÷ B, A + B, A − B) in most rows, but not in some. | Totals and rates computed from the parts disagree with the stored values. |
| DQ-S08 | Packed value disagrees | medium | A packed column whose first part repeats another column ('الرياض/النرجس' next to city 'الرياض'), but with a different value in some rows. | Two columns that should agree give different answers. |
| DQ-S09 | Hijri and Gregorian dates disagree | high | A Hijri date column and a Gregorian date column that describe the same day in most rows, but not in some. | Reports by Hijri and by Gregorian date count different rows. |

## Plausibility

| ID | Rule | Severity | What it checks | Why it matters |
|---|---|---|---|---|
| DQ-P01 | Outliers | scale: <1% low, >=1% medium | Numbers beyond 3 interquartile ranges from the quartiles. | Averages and totals are pulled by a few rows. |
| DQ-P02 | Unexpected negatives | medium | Negative numbers in a column that is otherwise positive. | Totals are understated. |
| DQ-P03 | Sentinel values | medium | Stand-in values such as -1, 9999, 1900-01-01 or 9999-12-31. | They count as real values in sums, averages and date ranges. |
| DQ-P04 | Out-of-range dates | medium | Gregorian dates after the as-of date or before 1900. | Time series and ages are distorted. |
| DQ-P05 | Possible truncation | medium | Many text values stop at exactly the same common length (50, 100, 255). | The end of the text was cut off by a field limit. |
| DQ-P06 | Unexpected zeros | medium | Zeros in a measure column that is otherwise positive (price, area, amount). | A zero is often a missing value in disguise and pulls averages down. |

## Thresholds

Override any of these under `thresholds` in the config. Changing them makes reports less comparable with earlier runs.

| Name | Default |
|---|---|
| `outlier_iqr_k` | 3.0 |
| `outlier_min_values` | 20 |
| `negative_max_share` | 0.05 |
| `zero_max_share` | 0.2 |
| `fd_min_strength` | 0.97 |
| `fd_max_columns` | 25 |
| `fd_max_distinct` | 500 |
| `fd_max_dominant_share` | 0.8 |
| `date_order_min_share` | 0.95 |
| `date_order_min_rows` | 20 |
| `calendar_pair_tolerance_days` | 3 |
| `calendar_pair_min_share` | 0.9 |
| `calc_min_share` | 0.95 |
| `calc_tolerance` | 0.01 |
| `calc_max_columns` | 12 |
| `packed_min_share` | 0.8 |
| `packed_match_min_share` | 0.9 |
| `typo_freq_ratio` | 5 |
| `typo_max_distinct` | 300 |
| `shape_dominant_min` | 0.8 |
| `identifier_unique_ratio` | 0.98 |
| `key_dup_min_unique_ratio` | 0.9 |
| `categorical_max_distinct` | 50 |
| `near_constant_share` | 0.99 |
| `script_min_share` | 0.01 |
| `segment_max_distinct` | 30 |
| `concentration_min_lift` | 2.0 |
| `concentration_min_rate` | 0.1 |
| `concentration_min_affected` | 5 |
| `correlation_min_phi` | 0.3 |
| `correlation_min_both` | 5 |
| `correlation_max_flags` | 40 |
| `truncation_min_share` | 0.01 |
| `variant_max_distinct` | 5000 |
| `max_rows` | 2000000 |
| `sample_values` | 5 |
| `sample_rows` | 10 |

## Detection notes

- Missing: empty cells are nulls (`DQ-C01`). Whitespace-only cells and placeholder words (`NULL`, `N/A`, `-`, `unknown`, `غير متوفر`, and others) are placeholders (`DQ-C03`). Catch-all values (`Other`, `أخرى`, `بدون`), alone or after a prefix or separator, are `DQ-C04`.
- Types: each value is classified as integer, decimal, formatted number, date, datetime, time, boolean or text after converting Arabic-Indic digits and separators. The column type is the family covering at least half of the non-missing values; the share is shown as type match.
- Dates: formats YYYY-MM-DD, YYYY/MM/DD, DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY, DD.MM.YYYY, two-digit years and month names. Slash dates are read as DD/MM unless a value proves MM/DD. Years 1300 to 1500 are Hijri; a column is Hijri when most of its dates are.
- Hijri and Gregorian pairs: Hijri dates are converted with the tabular Islamic calendar and compared with the Gregorian column. Rows more than 3 days apart are flagged.
- Identifier columns: names such as `*_id`, `id`, `*_code`, `*_no`, `رقم*`, `*مرجع*`, declared keys, or columns that are at least 98% unique with short values and no spaces.
- Column-wide format findings: `DQ-V02`, `DQ-V06`, `DQ-V09` and `DQ-V12` that cover 95% or more of a column's values are reported once and left out of the clean scores, issue links and concentrations.
- Spelling variants normalize case, spaces, hidden characters, Arabic diacritics and letter forms (أ إ آ to ا, ى to ي, ة to ه).
- Probable typos: a rare value at most 1 edit away (2 for values of 8+ characters, adjacent swaps count as 1) from a value at least 5 times more common.
- Dependencies: for category columns A and B (packed columns excluded), A determines B in at least 97% of rows while B has no dominant value above 80%.
- Calculations: up to 12 numeric columns are tested for C = A × B, A ÷ B, A + B and A − B on a fixed sample of 20,000 rows. A relationship that holds in 95% of rows (1% tolerance) is checked on every row.
- Packed columns: a separator (`/`, `|`, ` - `, `:`, `،`) appears in at least 80% of values. When the part before it matches another column in 90% of rows, the rows where it differs are `DQ-S08`.
- Duplicates except the ID: rows that match an earlier row after text normalization in every column except identifier columns that are at least 98% unique.
- Concentrations: a segment column has 2 to 30 values (or is a month or year of a date column). A segment is reported when its problem rate is at least 2 times the overall rate, at least 10%, with at least 5 affected rows, and it holds at least 10% of the problem or has a rate of 50% or more.
- Issue links: φ correlation of at least 0.3 and at least 5 rows with both problems, between problem types on different columns.
