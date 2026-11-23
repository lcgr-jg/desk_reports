# desk_reports

Scheduled desk emails that **read** landing tables from other repos (e.g. `oil_data_ingest`).
No scraping here — keep ingest and narrative separate.

## Layout

```
desk_reports/
  sender.py                 # shared Kafka email helper
  china_fuels/              # SunSirs China fuel spots daily
    report.py
    run_daily.bat
    scratch/                # --save-only HTML copies
```

## China fuels (daily)

Reads from `oil_data_ingest`:
- `data/processed/sunsirs/prices.parquet` (SunSirs spots)
- `data/processed/jlc/cracks.parquet` (JLC indie cracks via Bloomberg)

```powershell
cd desk_reports\china_fuels
python report.py --save-only   # write HTML only
python report.py               # build + email (you only while testing)
```

**Suggested schedule (local PC time):**

| Job | Time | Where |
|-----|------|--------|
| SunSirs scrape | 09:20 weekdays | `oil_data_ingest` |
| This report | 09:30 weekdays | `china_fuels\run_daily.bat` |

Task name example: `desk_reports\china_fuels_weekdays`.

## Setup

```powershell
cd desk_reports
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Or point Task Scheduler at the shared `Coding\.venv` if it already has plotly/kaleido/confluent-kafka.
