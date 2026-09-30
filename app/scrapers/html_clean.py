"""Turn pages into compact text an LLM can read (tables kept as 'cell | cell' rows)."""

import io
import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup, NavigableString

_DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas"]


def _span(cell, attr: str) -> int:
    try:
        return max(1, min(int(str(cell.get(attr, 1)).strip() or 1), 30))
    except ValueError:
        return 1


def _table_grid(table) -> list[tuple[bool, list[str]]]:
    """Rows as (all header cells?, cell texts), with rowspan/colspan cells repeated in every
    position they cover, so a row-group label ("Student") or a price spanning two columns
    appears on each row it belongs to and columns stay aligned."""
    grid: list[tuple[bool, list[str]]] = []
    carried: dict[int, tuple[str, int]] = {}  # column -> (text, rows still covered)
    for tr in table.find_all("tr"):
        row: list[str] = []
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        for cell in [*cells, None]:  # None: fill columns still covered after the last cell
            while len(row) in carried:  # columns covered by a rowspan from above
                text, left = carried.pop(len(row))
                if left > 1:
                    carried[len(row)] = (text, left - 1)
                row.append(text)
            if cell is None:
                break
            text = " ".join(cell.get_text(" ", strip=True).split())
            rows, cols = _span(cell, "rowspan"), _span(cell, "colspan")
            for _ in range(cols):
                if rows > 1:
                    carried[len(row)] = (text, rows - 1)
                row.append(text)
        if any(row):
            header = all(c.name == "th" for c in cells) and bool(cells)
            grid.append((header, row))
    return grid


def _merge_headers(rows: list[list[str]]) -> list[str]:
    """Stacked header rows ("Member | Member | Non-member" over "Onsite | Virtual | Onsite")
    as one row naming each column by all its headers: "Member / Onsite | Member / Virtual"."""
    merged = []
    for j in range(max(len(r) for r in rows)):
        parts: list[str] = []
        for r in rows:
            if j < len(r) and r[j] and r[j] not in parts:
                parts.append(r[j])
        merged.append(" / ".join(parts))
    return merged


def _table_to_text(table) -> str:
    out_rows: list[list[str]] = []
    headers: list[list[str]] = []  # header rows waiting for the data rows they name

    def flush_headers() -> None:
        if headers:
            out_rows.append(_merge_headers(headers) if len(headers) > 1 else headers[0])
            headers.clear()

    for is_th, row in _table_grid(table):
        while row and not row[-1]:
            row = row[:-1]
        if len(set(row)) == 1:  # a title row spanning the whole table: its own line
            flush_headers()
            out_rows.append(row[:1])
        elif is_th or not any(re.search(r"\d", c) for c in row):
            # Header rows are <th> rows or, on sites using <td> for everything, rows without
            # a digit. A block of them may start anywhere (a second price table below).
            headers.append(row)
        else:
            flush_headers()
            out_rows.append(row)
    flush_headers()

    lines = []
    for r in out_rows:
        # A long note spanning several columns is written once, not once per column.
        r = [c for i, c in enumerate(r) if i == 0 or len(c) <= 40 or c != r[i - 1]]
        # Empty cells stay (as "-") so every value stays under its column.
        lines.append(" | ".join(c or "-" for c in r))
    return "\n".join(lines)


def html_to_text(html: str, *, drop_chrome: bool = True) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    if drop_chrome:
        for tag in soup.find_all(["nav", "footer"]):
            tag.decompose()
    # Innermost tables first so nested layout tables don't swallow data tables.
    for table in reversed(soup.find_all("table")):
        table.replace_with(NavigableString("\n" + _table_to_text(table) + "\n"))
    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def page_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    return soup.title.get_text(strip=True) if soup.title else None


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Absolute (url, anchor text) pairs, de-duplicated, fragments removed."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if url in seen:
            continue
        seen.add(url)
        links.append((url, " ".join(a.get_text(" ", strip=True).split())))
    return links


def pdf_to_text(content: bytes, max_pages: int = 10) -> str:
    import pdfplumber

    parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()
