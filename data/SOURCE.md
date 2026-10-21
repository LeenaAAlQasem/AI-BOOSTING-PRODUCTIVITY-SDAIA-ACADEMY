# MOJ real estate sales, Q1 2024

- Publisher: Ministry of Justice (وزارة العدل), Saudi Arabia, through the Saudi National Open Data Portal (open.data.gov.sa).
- Copy taken unmodified from the repository civillizard/Saudi-Real-Estate-Data, file `moj/sales/MOJ-Sales-2024-Q1.csv`.
- License: KSA Open Data License (administered by SDAIA through the National Data Management Office). Cite the Ministry of Justice when using the data.
- Size: 59,099 rows, 10 columns, UTF-8 with BOM, comma-separated.
- Columns: المنطقة, المدينة, المدينة / الحي, الرقم المرجعي للصفقة, تاريخ الصفقة ميلادي, تاريخ الصفقة هجري, تصنيف العقار, عدد العقارات, السعر, المساحة.

Run from the repository root:

    python3 skill/data-quality-audit/scripts/dq_audit.py data/MOJ-Sales-2024-Q1.csv --config data/dq_config.json --out dq_output
