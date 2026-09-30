# Conference API: guide for the frontend

A read-only API over the conferences we collected: CS and tech conferences **starting 1 Oct to
30 Nov 2026**, focused on **Asia**, with registration fees, deadlines, online attendance and
publication history.

- **Base URL (local):** `http://127.0.0.1:8080`
- **Interactive reference:** `http://127.0.0.1:8080/docs` lists every endpoint and field, and
  lets you try requests in the browser.
- **Format:** JSON. Dates are `YYYY-MM-DD` strings. Missing values are `null`, never left out.
- **No authentication.**
- **CORS:** you can call the API straight from the browser when your app runs on `localhost` or
  `127.0.0.1` (any port). For a deployed frontend, send us its address (e.g.
  `https://conferences.example.com`) and we will add it. Only `GET` requests are allowed.

You only need the endpoints in this guide. The others (`/pipeline/*`, `/sources/*`, `/tools/*`)
run the scraper and are for us.

---

## Endpoints at a glance

| What you want | Request |
|---|---|
| The list of conferences | `GET /conferences?continent=Asia&compact=true&limit=1000` |
| One conference, with every fee | `GET /conferences/{id}` |
| Counts for filter chips and headers | `GET /summary` |
| A spreadsheet download | `GET /conferences/export?fmt=xlsx` |
| Is the API up? | `GET /health` |

---

## 1. List conferences: `GET /conferences`

Returns an array of conferences, sorted by start date.

**Always send `compact=true` for lists.** It leaves out the two long fields (`description` and
`field_sources`), which cuts the response from about 2.4 MB to about 50 KB.

### Filters (all optional, combine freely)

| Parameter | Example | Meaning |
|---|---|---|
| `continent` | `Asia` | One of `Asia`, `Europe`, `North America`, `South America`, `Africa`, `Oceania`, or `Unknown` |
| `has_fees` | `true` | Only conferences with fees (`false`: only those without) |
| `source` | `ieee` | Only conferences found on this site: `ieee`, `wikicfp`, `easychair`, `conferenceindex`, `edas`, `ieeecs` |
| `starts_from`, `starts_to` | `2026-11-01` | Conference start date in this range (inclusive) |
| `abstract_from`, `abstract_to` | `2026-09-30` | Abstract deadline in this range (inclusive) |
| `has_abstract` | `true` | Only conferences that list an abstract deadline |
| `limit`, `offset` | `1000`, `0` | Paging. `limit` is at most 1000; all of Asia fits in one call |

Search, sorting by fee, and the history and online filters are not API parameters. Load the list
once (it is small with `compact=true`) and filter it in the browser. That is what our dashboard
does.

### Examples

```text
# All Asian conferences
GET /conferences?continent=Asia&compact=true&limit=1000

# Abstract deadline not passed yet (use today's date)
GET /conferences?continent=Asia&compact=true&limit=1000&abstract_from=2026-09-30

# Conferences starting in the first half of November, with fees
GET /conferences?continent=Asia&compact=true&has_fees=true&starts_from=2026-11-01&starts_to=2026-11-15
```

### One row, real example

```json
{
  "id": 658,
  "acronym": "TSSA",
  "name": "2026 20th International Conference on Telecommunication Systems, Services, and Applications (TSSA)",
  "start_date": "2026-10-01",
  "end_date": "2026-10-02",
  "city": "Yogyakarta",
  "country": "Indonesia",
  "continent": "Asia",

  "abstract_deadline": null,
  "paper_deadline": "2026-08-21",
  "notification_date": "2026-08-31",
  "camera_ready_deadline": "2026-09-14",
  "registration_deadline": "2026-09-14",

  "homepage": "https://tssa-conference.org/2026/",
  "registration_url": "https://tssa-conference.org/2026/registration/",

  "standard_fee": {
    "amount": 600.0, "currency": "USD",
    "category": "International Participant Non IEEE Member", "tier": null,
    "amount_usd": 600.0, "amount_php": 37499.88
  },
  "student_fee": {
    "amount": 350.0, "currency": "USD",
    "category": "International Participant Student IEEE Member", "tier": null,
    "amount_usd": 350.0, "amount_php": 21874.93
  },
  "fee_min": 350.0, "fee_max": 600.0, "currency": "USD",
  "fee_min_usd": 350.0, "fee_max_usd": 600.0,
  "fee_min_php": 21874.93, "fee_max_php": 37499.88,

  "online": "online option",
  "history_label": "has history",
  "history_years": [2025, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2014],
  "organizer_flag": null,

  "status": "enriched",
  "review_note": null,
  "sources": ["easychair", "ieee"]
}
```

---

## 2. What the fields mean

### Fees: what to show

| Field | Show it as |
|---|---|
| `standard_fee` | **The headline price.** What a regular, non-member, international author pays to attend in person. `null` when no fee fits. |
| `student_fee` | The lowest student author rate open to international participants. `null` when none is listed. |
| `fee_min`, `fee_max`, `currency` | The range of all verified fees, in the original currency. |
| `..._usd`, `..._php` | The same amounts converted to US dollars and Philippine pesos at today's rate. `null` when the currency can't be converted. |

- Every headline fee has `amount_usd` and `amount_php`. Use `standard_fee.amount_usd` to sort by
  price across currencies.
- `category` and `tier` say which rate was picked. They make a good tooltip.
- **Required:** wherever you show converted prices (USD or PHP), link to the rate provider:
  "Rates by [ExchangeRate-API](https://www.exchangerate-api.com)". Their free terms require it.

### Online attendance: `online`

| Value | Meaning |
|---|---|
| `"online option"` | Hybrid, or online presentation is allowed |
| `"online only"` | Fully virtual |
| `"in person only"` | The site says presenting in person is required |
| `null` | The site doesn't say |

The detail view adds `attendance.evidence`: the quote or fact the answer is based on.

### Publication history: `history_label`, `history_years`

| Value | Meaning |
|---|---|
| `"has history"` | Earlier editions published proceedings (IEEE, ACM, Springer, ...). `history_years` lists them, newest first. |
| `"no history found"` | Nothing found. **Not a warning:** first editions and smaller publishers land here too. |
| `null` | Not checked (conferences with an `organizer_flag`) |

Show it as a neutral label, never as "predatory" or "safe". It is one signal among several.

### Other fields

| Field | Meaning |
|---|---|
| `organizer_flag` | A warning about the organizer, e.g. WASET, a widely reported predatory organizer. `null` for almost all. We suggest hiding these by default. |
| `abstract_deadline` | Only about a quarter of conferences list one. When it is `null`, show `paper_deadline` instead, labelled as such. |
| `sources` | The sites the conference was found on. |
| `status`, `review_note` | Where the scraper got to (next table). |

### Fee status, in plain words

`status` and `review_note` are written for the scraper. Show these labels instead:

| `status` | `review_note` starts with | Show |
|---|---|---|
| `enriched` | anything | Fees found |
| `needs_review` | `fees not published yet` / `fees not found` | No fees posted yet |
| `needs_review` | `prices on the page were not extracted` | Fees on the site, not read yet |
| `needs_review` | `fees found, but none could be verified` | Fees unverified |
| `needs_review` | `no homepage` | Website unreadable |
| `needs_review` | contains `don't match listing` | Check the edition |
| `collected` | | Not read yet |

---

## 3. One conference: `GET /conferences/{id}`

The same fields as a list row, plus:

| Field | Contents |
|---|---|
| `description` | 1 to 3 sentences on the topics (can be long; clamp it) |
| `fees` | Every fee (next example) |
| `history` | `{ has_history, editions: [{ year, name, publisher, doi }], publishers, note }` |
| `attendance` | `{ mode, evidence: [{ source, says, text }] }` |
| `listings` | The raw records from each source site |
| `field_sources` | Where each value came from: `{ "start_date": { "source": "homepage", "url": "..." } }` |

One entry in `fees`:

```json
{
  "category": "International Participant IEEE Member",
  "tier": null,
  "amount": 550.0, "currency": "USD",
  "amount_usd": 550.0, "amount_php": 34374.89,
  "check_status": "verified",
  "evidence": "Registration Fee International Participant IEEE Member: USD 550 ...",
  "kind": { "who": "author", "student": false, "member": true,
            "mode": null, "period": null, "local": false }
}
```

- **`check_status`:**
  - `verified`: the amount was found on the page;
  - `not_found` or `implausible`: it failed the check. Show those crossed out; they don't count
    in the fee range.
- **`kind`:** the rate in fixed terms.
  - `who`: `author`, `attendee` or `extra` (an add-on such as an extra paper);
  - `mode`: `onsite` or `virtual`;
  - `period`: `early`, `regular` or `late`;
  - `local`: `true` for rates for the host country only.
- **DOI links:** `https://doi.org/{doi}`.

---

## 4. Counts: `GET /summary`

For filter chips and headings.

| Field | Example |
|---|---|
| `conferences_by_continent` | `{ "Asia": 701, "Europe": 407, ... }` |
| `conferences_by_source` | `{ "ieee": 633, "wikicfp": 428, ... }` |
| `scope` | `{ "continents": ["Asia"], "conferences": 701, "conferences_with_fees": 395 }` |
| `exchange_rates` | `{ "date": "Tue, 29 Sep 2026 00:02:31 +0000", "name": "ExchangeRate-API", "url": "https://www.exchangerate-api.com" }` for the attribution line |

---

## 5. Spreadsheet: `GET /conferences/export`

Returns a file download.

| Parameter | Values |
|---|---|
| `fmt` | `csv` or `xlsx` |
| `continent` | A continent, or `all`. Default: Asia |
| The date filters from `/conferences` | Same names, same meaning |

A plain link works: `<a href="http://127.0.0.1:8080/conferences/export?fmt=xlsx">Download</a>`.
The file name is in the `Content-Disposition` header, which the browser can read.

---

## 6. Health: `GET /health`

```json
{ "ok": true, "window": ["2026-10-01", "2026-11-30"], ... }
```

`window` is the date range the data covers; use it in headings.

---

## Good to know

- **Data changes.** We re-read sites while the window is open, and fees usually appear close to
  the event. Don't cache the list for more than a few hours.
- **Times are dates only.** Compare deadlines with the user's local date, not UTC, or a deadline
  that passed yesterday can still look open after midnight in Manila.
- **Questions or missing fields:** ask us before working around something. Most additions are
  small on our side.
