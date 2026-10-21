# Config file

Optional JSON file passed with `--config`. Keep it next to the table so every run uses the same settings. Copy `assets/example_config.json` as a starting point. Command-line options override the config.

| Key | Type | Meaning |
|---|---|---|
| `dataset_name` | text | Name shown on the dashboard. Same as `--name`. |
| `input` | path | The table file, used when no file is given on the command line. |
| `sheet` | text | Excel sheet to audit. Same as `--sheet`. Default: the first sheet. |
| `table` | text | Table in a SQLite file or database. Same as `--table`. |
| `as_of` | `YYYY-MM-DD` | Reference date for future-date checks (`DQ-P04`). Default: today. Pin it for repeatable runs. |
| `redact` | true/false | Same as `--redact`: show value shapes instead of real values. |
| `primary_key` | list of columns | Declared key. Duplicates and nulls in it are critical (`DQ-U03`, `DQ-C01`). Composite keys are allowed. |
| `optional_columns` | list of columns | Columns allowed to be empty. Their completeness findings become `info` and they are left out of the clean scores. |
| `ignore_columns` | list of columns | Columns left out of the audit (technical or load columns). |
| `placeholder_tokens` | list of text | Extra words that mean "missing" in this table. Matched after trimming, case folding and Arabic letter normalization. |
| `future_dates_allowed` | list of columns | Date columns where future dates are expected. Names with expiry, due, end, valid_to, planned, target, deadline, انتهاء or استحقاق are allowed automatically. |
| `thresholds` | object | Overrides for values in `references/rules.md`. Use sparingly; changed thresholds make runs less comparable. |
