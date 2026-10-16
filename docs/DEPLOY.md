# Deploying the API

Scraping stays on the laptop. The data goes to Supabase (Postgres, free plan), and the public
API on DCISM reads it from there with `READ_ONLY=true`: only the data endpoints in
`docs/API.md`, no scraping routes.

```
laptop (scraper, SQLite) ──copy──▶ Supabase (schema `scraper`) ◀── API on DCISM ◀── frontends
```

## 1. Supabase (once)

1. Create a project at <https://supabase.com/dashboard> (Free plan). Pick the region closest to
   your users. Save the database password somewhere safe.
2. Copy the **connection string** from **Connect** (top bar), then **Session pooler**. It looks
   like `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`.
   Put your password in it and add it to the scraper's `.env` (never commit it):

   ```
   SUPABASE_DB_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
   ```

3. Copy the whole scraper database into the private schema `scraper`:

   ```powershell
   uv run python -m scripts.copy_to_supabase
   ```

   Each run replaces the copy in one transaction (about 35,000 rows, under a minute) and checks
   that every row arrived and that the EDAS account's details were removed. The schema has no
   grants for Supabase's public API roles; only the database password reads it.

## 2. Keeping it up to date

**Update everything** (dashboard button, or `POST /pipeline/update`) ends with a **publish**
step. When `SUPABASE_DB_URL` is set, it refreshes the `scraper` copy, so the API serves the new
data on its next request. No restart is needed. By hand:

```powershell
uv run python -m scripts.copy_to_supabase
```

## 3. Public API on DCISM

`https://conference-scraper-api.dcism.org` runs `app.main:app` with uvicorn.

- **Login:** `ssh -p 22072 s22102802@web.dcism.org` (password). The code is in
  `~/conference-scraper-api.dcism.org` (the subdomain points at that folder).
- **Port:** DCISM's Apache proxies the subdomain to `127.0.0.1:20280`. The proxy's errors are in
  `~/logs/error-conference-scraper-api.log`; a 503 page means nothing listens on that port.
- **`.env` on the server** (chmod 600):

  ```
  READ_ONLY=true
  DATABASE_URL=postgresql+psycopg://USER:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
  DATABASE_SCHEMA=scraper
  API_KEYS='["KEY"]'
  ```

- **API keys:** every endpoint except `/health` needs the header `X-API-Key` with one of the
  `API_KEYS`. To replace a key without cutting anyone off: add the new one
  (`'["OLD", "NEW"]'`), `pm2 restart conference-api`, hand out the new key, then remove the old
  one and restart again. Make a key with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

- **Process:** pm2 keeps uvicorn running and `pm2 save` brings it back after a reboot.

  ```bash
  cd ~/conference-scraper-api.dcism.org
  pm2 start .venv/bin/uvicorn --name conference-api --interpreter none -- \
      app.main:app --host 127.0.0.1 --port 20280 --proxy-headers --forwarded-allow-ips 127.0.0.1
  pm2 save
  pm2 logs conference-api      # or: pm2 restart conference-api / pm2 ls
  ```

- **Updating the code:** copy a commit over from the laptop (Git Bash) and restart:

  ```bash
  git archive HEAD app alembic alembic.ini pyproject.toml uv.lock scripts docs | \
    ssh -p 22072 s22102802@web.dcism.org \
    'cd ~/conference-scraper-api.dcism.org && tar xf - && ~/.local/bin/uv sync --no-dev && pm2 restart conference-api'
  ```

## 4. Is it working?

```powershell
curl.exe https://conference-scraper-api.dcism.org/health        # {"ok":true,"read_only":true,...}
curl.exe -H "X-API-Key: KEY" https://conference-scraper-api.dcism.org/summary   # counts per continent
curl.exe https://conference-scraper-api.dcism.org/summary       # 401 without the key
curl.exe -X POST -o NUL -w "%{http_code}\n" https://conference-scraper-api.dcism.org/pipeline/update   # 404
```

- **503 page:** uvicorn isn't running. On the server: `pm2 ls`, `pm2 logs conference-api`.
- **500 error:** the API is up but can't reach Supabase (wrong `.env`, or the project paused).
- `curl -s 127.0.0.1:20280/health` on the server skips DCISM's proxy: if that works and the
  public address doesn't, the problem is the proxy.

## Notes

- **Free Supabase projects pause** after a week with no requests at all. The API's traffic
  counts. If it pauses, restore it with one click in the dashboard. The data is kept.
- **Converted prices** credit ExchangeRate-API wherever they are shown, as their free terms
  require.
