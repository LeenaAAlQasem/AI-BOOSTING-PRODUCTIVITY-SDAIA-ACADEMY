---
name: data-quality-audit
description: Deep data quality audit of one table (CSV, TSV, Excel sheet, Parquet, JSON, SQLite table, or a database table or query) with a fixed one-page dashboard that looks the same on every run. Use whenever the user shares a table or dataset and wants to know what is wrong with it, even without saying "data quality". Covers nulls and placeholder values, catch-all values like "Other" or "أخرى", data types and type mismatches, formatted numbers stored as text, mixed date formats and Hijri/Gregorian calendars, distinct values and distributions per column, duplicates, spelling variants, outliers, sentinel values, cross-column checks (dependencies, date order, calculations, packed columns), issue correlation, and where problems concentrate. Locates problems only; it does not clean or fix data.
---

# Data quality audit (one table)

This skill gives a deep, repeatable picture of data quality problems in one table: every column, every row, and the links between columns. It shows where the problems are so the user can decide what to fix. Fixing data is out of scope, and so is analysis of the content itself (trends, models, business insight).

All checks run in `scripts/dq_audit.py`, which applies a fixed catalog of 42 rules (`DQ-T01` to `DQ-P06`) with fixed thresholds and writes a dashboard from `assets/dashboard_template.html`. Because rules, thresholds, ordering and layout are fixed, the same table always produces the same report, and any two tables produce dashboards with the same sections in the same order. Keep it that way: do not write ad-hoc analysis code, do not hand-edit the generated dashboard, and do not change the template or thresholds for one run.

## Workflow

1. **Find the table.** One table per run. If the user gives a workbook, use `--sheet`; a SQLite file, `--table`; a database, `--db-url` with `--table` or `--query` (needs SQLAlchemy and the driver). If the user gives several tables, run the script once per table and keep the outputs in separate folders.

2. **Check data handling before running.** If the table holds personal data (names, national IDs, phone numbers, emails, health or financial details), run with `--redact`. Sample values in the dashboard then show only their shape (`9999999999`, `AAAA`), so it can be shared without exposing people. Redaction also hides category labels, so skip it when the table has no personal fields. The script runs fully offline.

3. **Write a config only when it adds information.** Write `dq_config.json` next to the data when the user names the primary key, columns that may be empty, columns to skip, extra placeholder words, or when the same table will be audited again (the config keeps runs comparable). Schema and example: `references/config.md`. Never use the config to hide problems the user did not ask to hide.

4. **Run the audit.**

   ```bash
   python3 scripts/dq_audit.py <table file> --out dq_output \
       [--config dq_config.json] [--name "Dataset name"] [--sheet NAME] [--table NAME] \
       [--as-of YYYY-MM-DD] [--redact]
   ```

   Requires pandas and numpy (openpyxl for Excel, pyarrow for Parquet); install a missing package with pip. `.csv.gz` files are read directly. The script prints the totals and the top 15 findings, and writes:

   | File | Content |
   |---|---|
   | `dq_dashboard.html` | The one-page dashboard. Opens in any browser without internet. |
   | `dq_findings.csv` | One row per finding, with sample values, sample row numbers and a SQL query that returns the affected rows. |
   | `dq_report.json` | Every profile and finding, and the pattern data (issue links, concentrations, missing-value patterns) that the dashboard does not show. Read this to answer follow-up questions instead of recomputing. |

   Tables above 2,000,000 rows are checked on a fixed sample (seed 42), and the dashboard says so. Change the limit with `--max-rows`.

5. **Deliver.** Send `dq_dashboard.html` and `dq_findings.csv`. If an artifact tool is available and the user wants a shareable page, run again with `--fragment` and publish that file. Then reply in the format below.

## What the dashboard shows

The dashboard is one page with three parts, in this order. It shows facts only. It has no summaries, conclusions or advice.

| Part | Content |
|---|---|
| Metrics | Rows, columns, duplicate rows (%), empty cells (% of all cells, with the null and placeholder counts), columns with findings, and findings by severity. |
| Columns | One row per column: type, missing %, type mismatches, unique values, a distribution sparkline, and the finding count. Select a row to see the column details: counts, statistics or date range, a distribution chart, the top 10 values, and the findings for that column. |
| Findings | One row per finding: severity, column, issue, rows affected, and one short detail line. Filter by severity or column. Select a row to see example values and a SQL query that returns the affected rows. |

## Reply format

Write in the user's language. Keep column names exactly as they appear. Use the same structure every time so replies stay comparable:

1. One line with the totals: rows, columns, duplicate rows %, empty cells %, findings by severity.
2. The 3 to 5 most severe findings: column, issue, rows affected.

Do not add conclusions, cleaning steps or fix recommendations unless the user asks. To explain a finding, use `dq_report.json` and the rule text in `references/rules.md`.

## Reading the results

- **Severity** comes from the rule. Some rules scale it by the share of rows affected.
- **Missing** counts nulls and placeholders (`-`, `N/A`, `UNKNOWN`, `ERROR`, cells with only spaces).
- **Catch-all values** are values such as `Other`, `أخرى` or `بدون`, alone or after a prefix (`حي/أخرى`). They are a finding, because the real value is not in the table.
- **Duplicate rows** counts exact copies, near copies, and copies that differ only in the ID column.
- **Hijri dates** are found from the year range. The script checks a Hijri column against the Hijri calendar. It checks a Hijri column next to a Gregorian column for agreement (±3 days, because the tabular and Umm al-Qura calendars can differ by a day or two).
- **Issue links and concentrations** are in `dq_report.json` only. Use them when the user asks which problems occur together or where a problem is.

## Changing the rules

When the user wants a check that does not exist, add it to `scripts/dq_audit.py` with the next free ID in its category, add it to `RULES`, change `RULES_VERSION`, and update `references/rules.md`. The new rule then runs on every future table. Do not bolt one-off checks onto a single report.

## Troubleshooting

- **Wrong delimiter or garbled Arabic:** the loader tries UTF-16 (by BOM), UTF-8, Windows-1256, then Latin-1, and guesses the delimiter from the first 50 lines. Malformed rows are reported as `DQ-T02` and left out.
- **Dates read in the wrong order:** the script reads slash dates as DD/MM unless a value proves MM/DD. `date_order` in the column record of `dq_report.json` gives the order used.
- **A column empty by design floods the findings:** add it to `optional_columns`.
- **Wrong sheet or table:** pass `--sheet` or `--table`. `meta.notes` in `dq_report.json` gives the sheet or table that the script used.

## Files

- `scripts/dq_audit.py`: the audit engine and dashboard writer.
- `assets/dashboard_template.html`: the fixed dashboard template.
- `assets/example_config.json`: a config to copy.
- `references/rules.md`: the full rule catalog and thresholds. Read it when explaining a finding.
- `references/config.md`: config schema. Read it before writing a config.
