# Conference API: guide for the frontend

A read-only API over computer science and technology conferences **starting 1 Oct 2026 to
31 Mar 2027**, focused on **Asia** (about 1,400 conferences, 800 of them with fees). For each
conference: dates, location, deadlines, registration fees (with USD and PHP conversions), online
attendance and publication history.

|  |  |
|---|---|
| **Base URL** | `https://conference-scraper-api.dcism.org` |
| **Authentication** | Every request needs the header `X-API-Key: <your key>`. We send the key separately. |
| **Methods** | `GET` only |
| **Format** | JSON. Dates are `YYYY-MM-DD`. Missing values are `null`, never left out. |
| **Try it in the browser** | `https://conference-scraper-api.dcism.org/docs`, click **Authorize**, paste the key |

---

## Quick start

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "https://conference-scraper-api.dcism.org/conferences?continent=Asia&compact=true&limit=5000"
```

```js
const API = "https://conference-scraper-api.dcism.org";
const KEY = process.env.CONFERENCE_API_KEY; // keep it out of the repository

async function api(path) {
  const res = await fetch(API + path, { headers: { "X-API-Key": KEY } });
  if (!res.ok) throw new Error(`${res.status}: ${(await res.json()).detail}`);
  return res.json();
}

const conferences = await api("/conferences?continent=Asia&compact=true&limit=5000");
```

---

## Endpoints

| You want | Request | Key needed |
|---|---|---|
| The list of conferences | `GET /conferences` | yes |
| One conference with all its fees | `GET /conferences/{id}` | yes |
| Counts (for filter chips and headings) | `GET /summary` | yes |
| A CSV or Excel download | `GET /conferences/export` | yes |
| Is the API up? | `GET /health` | no |

---

## 1. `GET /conferences`: the list

Returns an array of conferences, sorted by start date.

**Send `compact=true` for lists.** It leaves out the two long fields (`description`,
`field_sources`) that a list doesn't show. All of Asia is then about 100 KB.

### Query parameters (all optional)

| Parameter | Example | Meaning |
|---|---|---|
| `continent` | `Asia` | `Asia`, `Europe`, `North America`, `South America`, `Africa`, `Oceania` or `Unknown`. Leave out for every continent. |
| `has_fees` | `true` | `true`: only conferences with fees. `false`: only those without. |
| `starts_from`, `starts_to` | `2026-11-01` | Start date within this range (inclusive) |
| `abstract_from`, `abstract_to` | `2026-10-17` | Abstract deadline within this range (inclusive) |
| `has_abstract` | `true` | Only conferences that list an abstract deadline |
| `source` | `ieee` | Only conferences found on this site: `ieee`, `wikicfp`, `easychair`, `conferenceindex`, `edas`, `ieeecs` |
| `status` | `enriched` | `enriched`, `needs_review` or `collected` (see "Fee status" below) |
| `compact` | `true` | Leave out the long text fields. Default `false`. |
| `limit` | `5000` | Rows per call. Default **100**, maximum 5000. |
| `offset` | `0` | Rows to skip, for paging |

There is no search, sort or free-text parameter. Load the list once (one call with
`limit=5000` covers all of Asia) and search, sort and filter in the browser.

### Examples

```text
# All Asian conferences
GET /conferences?continent=Asia&compact=true&limit=5000

# Abstract deadline not passed yet (use the user's local date)
GET /conferences?continent=Asia&compact=true&limit=5000&abstract_from=2026-10-17

# Starting in the first half of November, with fees
GET /conferences?continent=Asia&compact=true&has_fees=true&starts_from=2026-11-01&starts_to=2026-11-15
```

### One row (real data)

```json
{
  "id": 975,
  "acronym": "PCDS",
  "name": "2026 3rd International Symposium on Parallel Computing and Distributed Systems (PCDS)",
  "start_date": "2026-11-16",
  "end_date": "2026-11-18",
  "city": "Singapore",
  "country": "Singapore",
  "continent": "Asia",

  "abstract_deadline": null,
  "paper_deadline": "2026-09-27",
  "notification_date": "2026-10-11",
  "camera_ready_deadline": null,
  "registration_deadline": "2026-10-18",

  "homepage": "https://pcds.cc/",
  "registration_url": "https://pcds.cc/reg.html",

  "standard_fee": {
    "amount": 500.0, "currency": "USD", "category": "Regular Author", "tier": null,
    "amount_usd": 500.0, "amount_php": 31542.15
  },
  "student_fee": {
    "amount": 450.0, "currency": "USD", "category": "Student Author", "tier": null,
    "amount_usd": 450.0, "amount_php": 28387.93
  },
  "fee_min": 50.0, "fee_max": 500.0, "currency": "USD",
  "fee_min_usd": 50.0, "fee_max_usd": 500.0,
  "fee_min_php": 3154.21, "fee_max_php": 31542.15,

  "online": "online option",
  "attendance": {
    "mode": "online option",
    "evidence": [{ "source": "ieee", "says": "hybrid", "text": "IEEE lists the format as hybrid" }],
    "url": "https://pcds.cc/",
    "checked_at": "2026-09-29T12:57:52+00:00"
  },
  "history_label": "has history",
  "history_years": [2025, 2024],
  "organizer_flag": null,

  "status": "enriched",
  "review_note": null,
  "sources": ["ieee"]
}
```

---

## 2. What the fields mean

### Fees

| Field | Use it for |
|---|---|
| `standard_fee` | **The headline price**: what a regular, non-member, international author pays to attend in person. `null` when no fee fits. |
| `student_fee` | The lowest student author rate open to international participants. `null` when none is listed. |
| `fee_min`, `fee_max`, `currency` | The range of all verified fees, in the conference's own currency. `null` when there are no fees. |
| `*_usd`, `*_php` | The same amounts in US dollars and Philippine pesos at today's rate. `null` when the currency can't be converted. |

- To sort by price across currencies, use `standard_fee.amount_usd` (or `fee_min_usd`).
- `category` and `tier` name the rate that was picked; they make a good tooltip.
- **Required:** wherever converted prices (USD or PHP) are shown, add the line
  "Rates by [ExchangeRate-API](https://www.exchangerate-api.com)". Their free terms require it.

### Deadlines

`abstract_deadline` is listed by only about 1 in 4 conferences. When it is `null`, show
`paper_deadline` instead, labelled "paper deadline". Compare deadlines with the **user's local
date**, not UTC, or a deadline that passed yesterday can look open after midnight in Manila.

### Online attendance: `online`

| Value | Meaning |
|---|---|
| `"online option"` | Hybrid, or presenting online is allowed |
| `"online only"` | Fully virtual |
| `"in person only"` | The conference says presenting in person is required |
| `null` | The conference doesn't say |

`attendance.evidence` holds the quote or fact behind the answer, for a tooltip.

### Publication history: `history_label`, `history_years`

| Value | Meaning |
|---|---|
| `"has history"` | Earlier editions published proceedings (IEEE, ACM, Springer, ...). `history_years` lists the years, newest first. |
| `"no history found"` | Nothing found. **Not a warning:** first editions and smaller publishers land here too. |
| `null` | Not checked (conferences with an `organizer_flag`) |

Show it as a neutral label, never as "safe" or "predatory".

### Warnings: `organizer_flag`

`null` for almost every conference. When set, it is a warning about the organizer, for example
`"WASET, a widely reported predatory conference organizer"`. We suggest hiding these
conferences by default, with a toggle to show them.

### Fee status, in plain words

`status` and `review_note` are written for us. Show these labels instead:

| `status` | `review_note` starts with | Show |
|---|---|---|
| `enriched` | (anything) | Fees found |
| `needs_review` | `fees not published yet` or `fees not found` | No fees posted yet |
| `needs_review` | `prices on the page were not extracted` | Fees on the site, not read yet |
| `needs_review` | `fees found, but none could be verified` | Fees unverified |
| `needs_review` | `no homepage` | Website unreadable |
| `needs_review` | contains `don't match listing` | Check the edition |
| `collected` | | Not read yet |

### Other fields

| Field | Meaning |
|---|---|
| `id` | Stable number; use it for `/conferences/{id}` and in your URLs |
| `homepage`, `registration_url` | Links to the conference's own site |
| `sources` | The sites the conference was found on |

---

## 3. `GET /conferences/{id}`: one conference

The same fields as a list row, plus:

| Field | Contents |
|---|---|
| `description` | A few sentences on the topics (can be long; clamp it) |
| `fees` | Every fee (below) |
| `history` | `{ has_history, editions: [{ year, name, publisher, doi }], publishers, note }` |
| `listings` | The raw records from each source site (you can ignore these) |
| `field_sources` | Where each value came from: `{ "start_date": { "source": "homepage", "url": "..." } }` |

One entry in `fees`:

```json
{
  "id": 9953,
  "category": "Regular Author",
  "tier": null,
  "amount": 500.0,
  "currency": "USD",
  "amount_usd": 500.0,
  "amount_php": 31542.15,
  "notes": "Full paper (within 5 pages)",
  "source_url": "https://pcds.cc/reg.html",
  "check_status": "verified",
  "evidence": "Regular Author | 500USD | Full paper (within 5 pages) ...",
  "kind": { "who": "author", "student": false, "member": null,
            "mode": null, "period": "regular", "local": null }
}
```

- **`check_status`:** `verified` means the amount was found on the conference's page.
  `not_found` or `implausible` means it failed our check: show it crossed out or leave it out.
  Those fees don't count in `fee_min`/`fee_max`.
- **`kind`** describes the rate in fixed terms (`null` = not stated):
  - `who`: `author`, `attendee` or `extra` (an add-on such as an extra paper)
  - `student`, `member`: `true` / `false`
  - `mode`: `onsite` or `virtual`
  - `period`: `early`, `regular` or `late`
  - `local`: `true` for rates only for the host country
- **DOI links** in `history.editions`: `https://doi.org/{doi}`

A conference that doesn't exist returns **404**.

---

## 4. `GET /summary`: counts

| Field | Example |
|---|---|
| `conferences_by_continent` | `{ "Asia": 1413, "Europe": 516, "North America": 127, ... }` |
| `conferences_by_source` | `{ "ieee": 1081, "wikicfp": 742, ... }` |
| `scope` | `{ "continents": ["Asia"], "conferences": 1413, "conferences_with_fees": 801 }` |
| `exchange_rates` | `{ "date": "Fri, 16 Oct 2026 00:02:31 +0000", "name": "ExchangeRate-API", "url": "https://www.exchangerate-api.com" }`, for the attribution line |

Other fields in the response are for us.

---

## 5. `GET /conferences/export`: CSV or Excel file

| Parameter | Values |
|---|---|
| `fmt` | `csv` (default) or `xlsx` |
| `continent` | A continent, or `all`. **Default: Asia.** |
| `source`, `starts_from`, `starts_to`, `abstract_from`, `abstract_to`, `has_abstract` | Same as for `/conferences` |

The answer is a file; its name is in the `Content-Disposition` header (for example
`conferences-20261017-023045.xlsx`). A plain `<a href>` link **won't work**, because it can't
send the key. Download it with `fetch` instead:

```js
async function download(fmt = "xlsx") {
  const res = await fetch(`${API}/conferences/export?fmt=${fmt}`, { headers: { "X-API-Key": KEY } });
  const name = /filename="(.+)"/.exec(res.headers.get("Content-Disposition"))?.[1] ?? `conferences.${fmt}`;
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  URL.revokeObjectURL(url);
}
```

---

## 6. `GET /health`: is it up?

No key needed.

```json
{ "ok": true, "read_only": true, "window": ["2026-10-01", "2027-03-31"], ... }
```

`window` is the date range the data covers; use it in headings ("Conferences from October 2026
to March 2027").

---

## Errors

Every error is JSON with a `detail` field.

| Status | When | Example `detail` |
|---|---|---|
| `401` | Missing or wrong `X-API-Key` | `"Missing or invalid API key. Send it in the X-API-Key header."` |
| `404` | Unknown conference id, or an endpoint that doesn't exist | `"Not Found"` |
| `422` | A parameter is invalid (bad date, `limit` over 5000, ...) | A list saying which parameter and why |
| `500` / `503` | Our side is down; try again later and tell us | |

---

## Calling from a browser (CORS)

- **Local development:** pages on `localhost` or `127.0.0.1` (any port) can call the API
  directly.
- **Your deployed site:** send us its address (for example `https://conferences.example.com`)
  and we add it. Until then the browser blocks the calls.
- **The key and the browser:** a key in browser code can be read by anyone who opens the
  developer tools. If you can, call the API from your server (an API route or server
  component) and keep the key in a server-side environment variable. If the key leaks, tell us
  and we replace it.

---

## Good to know

- **Data changes.** Conferences are re-checked while the window is open, and fees often appear
  close to the event. Don't cache the list for more than a few hours.
- **Missing a field, or something looks wrong?** Ask us before working around it. Most
  additions are small on our side.
