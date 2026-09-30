# Conference Scraper

Collects computer science and technology conferences **starting 1 Oct – 30 Nov 2026** from several
sources. For each one it records name, dates, location, description, deadlines and **registration fees**.

- **Sources** → `raw_listings` (one row per source per conference)
- **Filter** → mark which listings fall in the date window and which are CS/tech
- **Merge** → combine duplicates across sources into one row in `conferences`
- **Enrich** → crawl each homepage; Gemini extracts fees, deadlines and a description into `fees` and `conferences`
- **Export** → CSV or XLSX

## Setup

```powershell
uv sync                              # dependencies (.venv)
uv run playwright install chromium   # browser for EDAS login and JS-rendered pages
copy .env.example .env               # then set GEMINI_API_KEY
uv run alembic upgrade head          # create/upgrade data/scraper.db
uv run uvicorn app.main:app --reload --reload-include ".env" --port 8080
```

Open http://localhost:8080/docs. Every step has its own endpoint, so you can test each part separately.

### EDAS login (once, and again when the session expires)

```powershell
uv run python -m scripts.edas_login
```

A browser opens. Log in, then press Enter in the terminal. Only the session cookies are saved (to
`data/edas_state.json`); your password is not stored.

## Suggested order for testing

| Step | Endpoint | Notes |
|---|---|---|
| Check config | `GET /health` | Window, Gemini key, EDAS session |
| Browse names from listing links | `GET /browse/{source}?page=1&pages=3&category=...` | Names + links only, nothing saved |
| Inspect a site's markup | `POST /tools/fetch`, `POST /tools/render` | Saves HTML to `data/recon/` |
| Try an adapter without saving | `POST /sources/{name}/preview?limit=10` | |
| Collect into the database | `POST /sources/{name}/collect` → `GET /runs/{id}` | Runs in the background |
| Filter | `POST /pipeline/filter` (`?use_llm=true` for unclear listings) | |
| Merge | `POST /pipeline/merge` | |
| Try one homepage | `POST /tools/discover-pages`, `POST /tools/extract` | Nothing is saved |
| Enrich | `POST /pipeline/enrich?limit=5` → `GET /runs/{id}` | Try a few first |
| Review | `GET /summary`, `GET /conferences?status=needs_review` | |
| Export | `GET /conferences/export?fmt=xlsx` | Saved to `data/exports/` |

Small helpers for single parsers: `/tools/parse-dates`, `/tools/parse-location`, `/tools/parse-money`,
`/tools/acronym-key`, `/tools/classify-topic`, `/tools/fees-from-html`.

## Source status

| Source | Status |
|---|---|
| WikiCFP | Built and tested against live pages. Walks each category in `WIKICFP_CATEGORIES`. |
| EDAS | Built from the screenshot layout. **Needs checking against real HTML** after login (`/tools/fetch` with `use_edas_session: true`). |
| IEEE, ACM, EasyChair, Springer, ConferenceIndex | Placeholders. Inspect their pages first, then write `collect()` in `app/scrapers/`. |

Adding a source means one file in `app/scrapers/` that subclasses `SourceAdapter`, plus a line in
`registry.py`.

## Layout

```
app/
  main.py            FastAPI app
  config.py          settings (.env)
  db.py, models.py   SQLAlchemy + SQLite
  schemas.py         adapter output, Gemini output schema, API models
  api/               sources, pipeline, data, tools routers
  scrapers/          http.py (rate limit, robots.txt, cache), html_clean.py, one adapter per source
  extract/           Extractor interface + Gemini implementation
  pipeline/          collect, filter, merge, enrich, export, normalize, fees
alembic/             migrations
scripts/edas_login.py
tests/               unit tests; fixtures are real saved WikiCFP pages
data/                SQLite db, HTTP cache, recon pages, exports, EDAS session (not for sharing)
```

## Migrations

```powershell
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```

`render_as_batch` is enabled, so column changes work on SQLite.

## Notes

- **Politeness:** one request per `REQUEST_DELAY_SECONDS` per domain (EDAS: `EDAS_DELAY_SECONDS`), robots.txt
  respected, pages cached for `CACHE_TTL_HOURS`, so reruns are cheap.
- **Old editions:** if the dates Gemini finds on a homepage are more than 45 days from the listing's
  dates, the conference is marked `needs_review` rather than overwritten. The homepage is probably
  showing a previous year's edition.
- **Field provenance:** `conferences.field_sources` records which source and URL each value came from.
