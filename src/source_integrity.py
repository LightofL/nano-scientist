"""Bibliographic status for literature claims.

The canonical prose rule is ``skills/shared-references/source-integrity.md``.
This module is the check the writing loop and the Finisher actually run.
A status of ``verified`` means the title, author family, and year agreed with
Crossref or OpenAlex. It does not mean the paper supports the cited sentence.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

STATUS_USER_PROVIDED = "user_provided"
STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
EVIDENCE_STATUSES = frozenset({STATUS_USER_PROVIDED, STATUS_VERIFIED})

LITERATURE_SKILLS = frozenset({
    "alphaxiv",
    "arxiv",
    "citation-audit",
    "comm-lit-review",
    "deepxiv",
    "evo-memory",
    "gemini-search",
    "novelty-check",
    "openalex",
    "paper-navigator",
    "paper-plan",
    "paper-planning",
    "paper-write",
    "paper-writing",
    "prior-art-search",
    "research-ideation",
    "research-lit",
    "research-review",
    "research-survey",
    "semantic-scholar",
})

SKILL_GUARDRAIL_BLOCK = """<!-- source-integrity-guardrail -->
> **Guardrails:** Follow [`../shared-references/source-integrity.md`](../shared-references/source-integrity.md). Every literature claim carries `user_provided`, `verified`, or `unverified`. Unverified items are not evidence. Bibliographic verification is not a claim-level check. End with `## Pending verification` and `## Questions for the author`. Generated paragraphs are drafts for the author to rewrite, not ready-to-use review prose. Report counts for `verified`, `unverified`, and `user_provided`.
"""

PROMPT_GUARDRAIL_FALLBACK = (
    "Use only user_provided or verified sources as evidence. "
    "Unverified items are not evidence. Do not invent bibliographic metadata. "
    "Bibliographic verification is not a claim-level check."
)

DRAFT_HEADING = "## Draft paragraphs (author rewrite required)"
PENDING_HEADING = "## Pending verification"
QUESTIONS_HEADING = "## Questions for the author"

DRAFT_LATEX = (
    r"\noindent\textit{Draft for author rewrite. This is not ready-to-use review prose. "
    r"A verified or user-provided citation only means the bibliographic record was accepted. "
    r"It does not mean the cited paper supports the sentence. Unverified sources are not evidence.}"
)

_DRAFT_MARKERS = (DRAFT_HEADING, "段落草稿", "Draft for author rewrite")
_PENDING_MARKERS = (PENDING_HEADING, "待核实清单")
_QUESTION_MARKERS = (QUESTIONS_HEADING, "给作者的问题")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into",
    "using", "via", "over", "under", "between", "among",
}
_NOTE_FIELD = re.compile(r"(\bnote\s*=\s*\{)([^}]*)(\})", re.IGNORECASE)
_CITE_CMD = re.compile(r"\\cite[a-zA-Z*]*\{([^}]+)\}")
_USER_PROVIDED = re.compile(
    r"verification\s*[:=]\s*\{?\s*user_provided\b",
    re.IGNORECASE,
)
_STATUS_CLAUSE = re.compile(
    r"verification:\s*[^;]*;\s*method:\s*[^;]*;\s*result:\s*[^;]*;\s*claim_checked:\s*(?:true|false)\s*;?",
    re.IGNORECASE,
)


def source_integrity_path(skills_dir: str):
    from pathlib import Path
    return Path(skills_dir) / "shared-references" / "source-integrity.md"


def load_prompt_guardrail(skills_dir: str) -> str:
    """Return the short preamble stored in the canonical rule file."""
    path = source_integrity_path(skills_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return PROMPT_GUARDRAIL_FALLBACK
    match = re.search(
        r"<!-- prompt-guardrail:start -->(.*?)<!-- prompt-guardrail:end -->",
        text,
        re.DOTALL,
    )
    if not match:
        return PROMPT_GUARDRAIL_FALLBACK
    body = match.group(1).strip()
    return body or PROMPT_GUARDRAIL_FALLBACK


def _cite_key(entry: str) -> str:
    match = re.match(r"@\w+\{([^,]+),", entry.strip())
    return match.group(1).strip() if match else ""


def cite_keys_from_entries(entries: list[str]) -> list[str]:
    keys = []
    for entry in entries:
        key = _cite_key(entry)
        if key:
            keys.append(key)
    return keys


def _field(entry: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*\{{([^}}]*)\}}", entry, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _doi_from_entry(entry: str) -> str:
    raw = _field(entry, "doi")
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.IGNORECASE)
    return raw.strip()


def _author_families(entry: str) -> list[str]:
    raw = _field(entry, "author")
    families = []
    for part in re.split(r"\s+and\s+", raw):
        part = part.strip()
        if not part or part.lower() in {"unknown", "others"}:
            continue
        if "," in part:
            family = part.split(",", 1)[0]
        else:
            tokens = part.split()
            family = tokens[-1] if tokens else ""
        norm = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", family.lower())
        if norm and norm != "unknown":
            families.append(norm)
    return families


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _words(text: str) -> list[str]:
    tokens = re.findall(r"[0-9a-z\u4e00-\u9fff]+", text.lower())
    content = [w for w in tokens if len(w) > 2 and w not in _STOPWORDS]
    if content:
        return content
    return [w for w in tokens if len(w) > 1]


def _title_overlap(left: str, right: str) -> float:
    entry_words = _words(left)
    other_words = set(_words(right))
    if not entry_words:
        return 0.0
    return len(set(entry_words) & other_words) / len(set(entry_words))


def _is_user_provided(entry: str) -> bool:
    return bool(_USER_PROVIDED.search(entry))


def _record(key: str, status: str, method: str | None, result: str, detail: str) -> dict:
    return {
        "key": key,
        "status": status,
        "method": method,
        "result": result,
        "claim_checked": False,
        "detail": detail,
    }


def _note_clause(record: dict) -> str:
    return (
        f"verification: {record.get('status', STATUS_UNVERIFIED)}; "
        f"method: {record.get('method') or 'none'}; "
        f"result: {record.get('result') or 'not_checked'}; "
        f"claim_checked: false"
    )


def _upsert_clause(note_body: str, clause: str) -> str:
    cleaned = _STATUS_CLAUSE.sub("", note_body)
    cleaned = re.sub(
        r"verification\s*[:=]\s*\{?\s*(?:user_provided|verified|unverified)\s*\}?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*;\s*", "; ", cleaned).strip(" ;")
    if cleaned:
        return f"{cleaned}; {clause}"
    return clause


def stamp_entry(entry: str, record: dict) -> str:
    """Record the status inside the BibTeX note without changing other fields."""
    clause = _note_clause(record)
    match = _NOTE_FIELD.search(entry)
    if match:
        body = _upsert_clause(match.group(2), clause)
        return entry[: match.start()] + match.group(1) + body + match.group(3) + entry[match.end() :]
    stripped = entry.rstrip()
    if not stripped.endswith("}"):
        return entry.rstrip() + f"\n  note = {{{clause}}},\n"
    inner = stripped[:-1].rstrip()
    if not inner.endswith(","):
        inner += ","
    return inner + f"\n  note   = {{{clause}}},\n}}\n"


def default_fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "nanoscientist/1.0 (mailto:research@example.com)"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _try_fetch(url: str, fetch_json) -> dict | None:
    try:
        payload = fetch_json(url)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, TimeoutError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _from_crossref_item(item: dict) -> dict:
    authors_raw = item.get("author") or []
    families = []
    bib_authors = []
    for author in authors_raw:
        family = (author.get("family") or "").strip()
        given = (author.get("given") or "").strip()
        if not family:
            continue
        families.append(family)
        bib_authors.append(f"{family}, {given}".rstrip(", "))
    date_parts = (item.get("published") or item.get("issued") or {}).get("date-parts") or [[]]
    year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""
    titles = item.get("title") or []
    return {
        "title": titles[0] if titles else "",
        "authors": families,
        "author_bib": " and ".join(bib_authors),
        "year": year,
        "doi": item.get("DOI") or "",
        "venue": (item.get("container-title") or [""])[0],
        "type": item.get("type") or "",
    }


def _from_openalex(item: dict) -> dict:
    families = []
    bib_authors = []
    for authorship in item.get("authorships") or []:
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if not name:
            continue
        tokens = name.split()
        family = tokens[-1]
        given = " ".join(tokens[:-1])
        families.append(family)
        bib_authors.append(f"{family}, {given}".rstrip(", "))
    doi = item.get("doi") or ""
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    source = ((item.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    return {
        "title": item.get("display_name") or item.get("title") or "",
        "authors": families,
        "author_bib": " and ".join(bib_authors),
        "year": str(item.get("publication_year") or ""),
        "doi": doi,
        "venue": source,
        "type": item.get("type") or "",
    }


def _crossref_work(payload: dict) -> dict | None:
    message = payload.get("message")
    if isinstance(message, dict) and message.get("title"):
        return _from_crossref_item(message)
    if isinstance(message, dict) and message.get("items"):
        return _from_crossref_item(message["items"][0])
    return None


def _lookup_doi(doi: str, fetch_json) -> tuple[dict | None, str | None]:
    encoded = urllib.parse.quote(doi, safe="/")
    crossref = _try_fetch(f"https://api.crossref.org/works/{encoded}", fetch_json)
    if crossref:
        work = _crossref_work(crossref)
        if work and work.get("title"):
            return work, "crossref_doi"
    openalex = _try_fetch(f"https://api.openalex.org/works/doi:{encoded}", fetch_json)
    if openalex and (openalex.get("title") or openalex.get("display_name")):
        return _from_openalex(openalex), "openalex_doi"
    return None, None


def _lookup_title(title: str, authors: list[str], fetch_json) -> tuple[dict | None, str | None]:
    first = authors[0] if authors else ""
    query = urllib.parse.quote(f"{title} {first}".strip())
    crossref_url = (
        "https://api.crossref.org/works?query.bibliographic="
        f"{query}&rows=1&select=DOI,title,author,published,container-title,type"
    )
    crossref = _try_fetch(crossref_url, fetch_json)
    if crossref:
        work = _crossref_work(crossref)
        if work and work.get("title"):
            return work, "crossref_title"
    openalex = _try_fetch(
        f"https://api.openalex.org/works?search={query}&per-page=1",
        fetch_json,
    )
    results = (openalex or {}).get("results") or []
    if results:
        return _from_openalex(results[0]), "openalex_title"
    return None, None


def _matches(title: str, authors: list[str], year: str, work: dict) -> tuple[bool, str]:
    if not title or not work.get("title"):
        return False, "insufficient"
    if _title_overlap(title, work["title"]) < 0.6:
        return False, "mismatch"
    work_year = str(work.get("year") or "")
    if year and work_year and str(year) != work_year:
        return False, "mismatch"
    work_authors = {
        re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", name.lower())
        for name in work.get("authors") or []
    }
    work_authors.discard("")
    if not authors or not work_authors:
        return False, "insufficient"
    if set(authors).isdisjoint(work_authors):
        return False, "mismatch"
    return True, "match"


def _with_cjk_note(title: str, status: str, detail: str) -> str:
    if status == STATUS_VERIFIED or not _contains_cjk(title):
        return detail
    return (
        detail.rstrip()
        + " Ask the user to search CNKI, Wanfang, and VIP and paste the record. Do not invent metadata."
    )


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:180]


def assess_entry(entry: str, fetch_json=None) -> dict:
    """Assign a bibliographic status. Does not rewrite the entry's fields."""
    fetch_json = fetch_json or default_fetch_json
    key = _cite_key(entry)
    if _is_user_provided(entry):
        return _record(
            key,
            STATUS_USER_PROVIDED,
            STATUS_USER_PROVIDED,
            "not_checked",
            "User supplied this record. Fields were not rewritten or independently checked.",
        )

    title = _field(entry, "title")
    authors = _author_families(entry)
    year = _field(entry, "year")
    doi = _doi_from_entry(entry)

    def finish(status: str, method: str | None, result: str, detail: str) -> dict:
        return _record(key, status, method, result, _with_cjk_note(title, status, detail))

    if not title or not authors or not year:
        detail = "Record is missing title, author, or year. Missing fields were not filled in."
        if doi:
            work, method = _lookup_doi(doi, fetch_json)
            if work and work.get("title"):
                detail += (
                    f" A {method} record exists for the DOI ({_short(work['title'])}) "
                    "but was not copied into the entry."
                )
            else:
                detail += " DOI lookup failed or returned nothing. Result: not found."
        return finish(STATUS_UNVERIFIED, None, "insufficient", detail)

    if doi:
        work, method = _lookup_doi(doi, fetch_json)
        if work is None:
            return finish(
                STATUS_UNVERIFIED,
                None,
                "not_found",
                "DOI lookup failed or returned nothing. Result: not found.",
            )
        ok, why = _matches(title, authors, year, work)
        if ok:
            return finish(
                STATUS_VERIFIED,
                method,
                "match",
                f"Checked via {method}. Title, author family, and year agree. "
                "Venue was not string-matched. The claim was not checked against the abstract or full text.",
            )
        return finish(
            STATUS_UNVERIFIED,
            method,
            why,
            f"Checked via {method}. Bibliographic fields did not agree ({why}). "
            f"Candidate title: {_short(work.get('title', ''))}. Original fields kept.",
        )

    work, method = _lookup_title(title, authors, fetch_json)
    if work is None:
        return finish(
            STATUS_UNVERIFIED,
            None,
            "not_found",
            "Title lookup failed or returned nothing. Result: not found.",
        )
    ok, why = _matches(title, authors, year, work)
    if ok:
        return finish(
            STATUS_VERIFIED,
            method,
            "match",
            f"Checked via {method}. Title, author family, and year agree. "
            "Venue was not string-matched. The claim was not checked against the abstract or full text.",
        )
    return finish(
        STATUS_UNVERIFIED,
        method,
        why,
        f"Checked via {method}. Candidate did not agree ({why}). "
        f"Candidate title: {_short(work.get('title', ''))}. Original fields kept.",
    )


def looks_like_doi(key: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/\S+$", key))


def unverified_placeholder(key: str) -> tuple[str, dict]:
    record = _record(
        key,
        STATUS_UNVERIFIED,
        None,
        "not_found",
        "No bibliographic record was resolved for this citation key. Result: not found. Not evidence.",
    )
    entry = (
        f"@misc{{{key},\n"
        f"  author = {{Unknown}},\n"
        f"  title  = {{Pending verification}},\n"
        f"  note   = {{{_note_clause(record)}}},\n"
        f"}}\n"
    )
    return entry, record


def _bibtex_from_work(key: str, work: dict, record: dict) -> str:
    entry_type = "inproceedings" if "proceedings" in (work.get("type") or "") else "article"
    venue_field = "booktitle" if entry_type == "inproceedings" else "journal"
    lines = [
        f"@{entry_type}{{{key},",
        f"  author  = {{{work.get('author_bib') or 'Unknown'}}},",
        f"  title   = {{{work.get('title') or ''}}},",
        f"  year    = {{{work.get('year') or ''}}},",
        f"  {venue_field} = {{{work.get('venue') or ''}}},",
    ]
    if work.get("doi"):
        lines.append(f"  doi     = {{{work['doi']}}},")
    lines.append(f"  note    = {{{_note_clause(record)}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def resolve_missing_citation(key: str, fetch_json=None) -> tuple[str, dict]:
    """Resolve an undefined cite key without guessing from the key text.

    A key that is itself a DOI is fetched from Crossref or OpenAlex and, when
    that record exists, stored as ``verified``. Any other key becomes an
    ``unverified`` placeholder. The key is never used as a title query.
    """
    fetch_json = fetch_json or default_fetch_json
    if looks_like_doi(key):
        work, method = _lookup_doi(key, fetch_json)
        if work and work.get("title") and work.get("authors"):
            record = _record(
                key,
                STATUS_VERIFIED,
                method,
                "match",
                f"Citation key is a DOI. Bibliographic record taken from {method}. "
                "The claim was not checked against the abstract or full text.",
            )
            return _bibtex_from_work(key, work, record), record
    return unverified_placeholder(key)


def count_statuses(ledger: dict) -> dict:
    counts = {STATUS_VERIFIED: 0, STATUS_UNVERIFIED: 0, STATUS_USER_PROVIDED: 0}
    for record in (ledger or {}).values():
        status = record.get("status", STATUS_UNVERIFIED)
        if status not in counts:
            status = STATUS_UNVERIFIED
        counts[status] += 1
    return counts


def citation_summary(ledger: dict) -> dict:
    counts = count_statuses(ledger)
    evidence = [key for key, record in (ledger or {}).items() if record.get("status") in EVIDENCE_STATUSES]
    pending = [key for key, record in (ledger or {}).items() if record.get("status") not in EVIDENCE_STATUSES]
    return {
        **counts,
        "claim_checked": 0,
        "evidence_keys": evidence,
        "pending_keys": pending,
        "note": (
            "Unverified entries are not evidence. "
            "verified and user_provided are bibliographic statuses, not claim-level checks."
        ),
    }


def split_cite_keys(keys: list[str], ledger: dict) -> tuple[list[str], list[str]]:
    evidence, pending = [], []
    for key in keys:
        status = ((ledger or {}).get(key) or {}).get("status", STATUS_UNVERIFIED)
        if status in EVIDENCE_STATUSES:
            evidence.append(key)
        else:
            pending.append(key)
    return evidence, pending


def format_ledger_block(ledger: dict) -> str:
    counts = count_statuses(ledger)
    lines = [
        (
            f"verified={counts[STATUS_VERIFIED]} "
            f"unverified={counts[STATUS_UNVERIFIED]} "
            f"user_provided={counts[STATUS_USER_PROVIDED]}"
        ),
        "Unverified items are not evidence. verified and user_provided mean the bibliographic "
        "record was accepted, not that the paper supports the claim.",
    ]
    for key, record in list((ledger or {}).items())[:40]:
        lines.append(
            f"- {key}: {record.get('status')} "
            f"(method={record.get('method') or 'none'}, result={record.get('result')})"
        )
    return "\n".join(lines)


def strip_unverified_cites(body: str, ledger: dict) -> str:
    """Replace cites that are not verified or user-provided with a visible marker."""

    def allowed(key: str) -> bool:
        return ((ledger or {}).get(key) or {}).get("status") in EVIDENCE_STATUSES

    def replace(match: re.Match) -> str:
        keys = [key.strip() for key in match.group(1).split(",") if key.strip()]
        kept = [key for key in keys if allowed(key)]
        dropped = [key for key in keys if not allowed(key)]
        if not dropped:
            return match.group(0)
        if not kept:
            return r"\textbf{[needs source]}"
        prefix = match.group(0).split("{", 1)[0]
        return prefix + "{" + ",".join(kept) + "}"

    return _CITE_CMD.sub(replace, body)


def label_draft_section(body: str) -> str:
    if "Draft for author rewrite" in body:
        return body
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if re.match(r"\\section\{", line):
            lines.insert(index + 1, "\n" + DRAFT_LATEX + "\n")
            return "\n".join(lines)
    return DRAFT_LATEX + "\n\n" + body


def _section_present(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def render_draft_section() -> str:
    return (
        f"{DRAFT_HEADING}\n\n"
        "The prose above is a draft for the author to rewrite and check. "
        "It is not ready-to-use review prose. A sentence that depends on the literature "
        "needs a `verified` or `user_provided` source, or `[needs source]`."
    )


def render_pending_section(ledger: dict) -> str:
    lines = [PENDING_HEADING, ""]
    if not ledger:
        lines.append("None.")
        return "\n".join(lines)
    for key, record in (ledger or {}).items():
        status = record.get("status")
        if status == STATUS_VERIFIED and record.get("claim_checked"):
            continue
        detail = record.get("detail") or ""
        if status == STATUS_VERIFIED:
            lines.append(
                f"- [{status}] `{key}`: bibliographic record accepted via {record.get('method')}. "
                "Claim-level check is still open. The paper was not read against this sentence."
            )
        else:
            lines.append(f"- [{status}] `{key}`: {detail}")
    if len(lines) == 2:
        lines.append("None.")
    return "\n".join(lines)


def render_questions_section(ledger: dict) -> str:
    lines = [QUESTIONS_HEADING, ""]
    questions = []
    user_keys = [key for key, record in (ledger or {}).items() if record.get("status") == STATUS_USER_PROVIDED]
    pending_keys = [key for key, record in (ledger or {}).items() if record.get("status") == STATUS_UNVERIFIED]
    cjk = [
        key for key, record in (ledger or {}).items()
        if "CNKI" in (record.get("detail") or "")
    ]
    if user_keys:
        questions.append(
            "Please confirm these user-supplied records are the versions you want cited: "
            + ", ".join(f"`{key}`" for key in user_keys)
            + "."
        )
    if pending_keys:
        questions.append(
            "Which pending items should be dropped, and which will you verify yourself? "
            + ", ".join(f"`{key}`" for key in pending_keys)
            + "."
        )
    if cjk:
        questions.append(
            "Please search CNKI, Wanfang, or VIP and paste the records for: "
            + ", ".join(f"`{key}`" for key in cjk)
            + ". The agent will not invent those fields."
        )
    questions.append(
        "Which sentences should be checked against the abstract or the full text? "
        "A bibliographic status does not answer that."
    )
    if not ledger:
        lines.append("None.")
        return "\n".join(lines)
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question}")
    return "\n".join(lines)


def render_contract_sections(ledger: dict) -> str:
    return "\n\n".join([
        render_draft_section(),
        render_pending_section(ledger),
        render_questions_section(ledger),
    ])


def ensure_output_contract(text: str, ledger: dict) -> str:
    """Append any missing output-contract sections. Does not duplicate them."""
    parts = []
    if not _section_present(text, _DRAFT_MARKERS):
        parts.append(render_draft_section())
    if not _section_present(text, _PENDING_MARKERS):
        parts.append(render_pending_section(ledger))
    if not _section_present(text, _QUESTION_MARKERS):
        parts.append(render_questions_section(ledger))
    if not parts:
        return text
    return text.rstrip() + "\n\n" + "\n\n".join(parts) + "\n"


def render_pending_document(ledger: dict) -> str:
    counts = count_statuses(ledger)
    intro = (
        "# Pending verification\n\n"
        "Bibliographic status is not a claim-level check. "
        "Unverified items are not evidence. "
        "This list does not block the PDF.\n\n"
        f"Counts: verified={counts[STATUS_VERIFIED]}, "
        f"unverified={counts[STATUS_UNVERIFIED]}, "
        f"user_provided={counts[STATUS_USER_PROVIDED]}, "
        "claim_checked=0.\n"
    )
    return intro + "\n" + render_contract_sections(ledger) + "\n"
