# Source integrity

Process rule for literature claims in nano-scientist. Inspired by the v0.2.0 source-integrity contract in `xingtaxueshu/literature-review-skills`. This file is the canonical rule. Skills point here. The runtime preamble is the `prompt-guardrail` block below, loaded by `src/source_integrity.py`.

These rules constrain the agent. They do not guarantee that a model will comply, and they are not an evaluation of citation quality.

## Statuses

Every literature item carries exactly one bibliographic status.

| Status | Meaning | Evidence? |
|---|---|---|
| `user_provided` | The user supplied the bibliographic record (or a note, abstract, or passage). Use only the fields they gave. Do not silently "correct" year, authors, venue, pages, or DOI. Record doubts in the pending list. | Yes, with the caveat that the record was not independently checked. |
| `verified` | Title, author family, and year were checked against Crossref, OpenAlex, or a publisher page, and the method was recorded (`crossref_doi`, `crossref_title`, `openalex_doi`, `openalex_title`, or `publisher_page`). | Yes, as a bibliographic record only. |
| `unverified` | Title-only clue, a candidate recalled from memory, a lookup that failed, or a lookup that disagreed. May appear in the pending list. | No. Not as support, not as an example of "studies have shown," not as a filled-in BibTeX row. |

Chinese aliases, if a skill writes them: `用户提供` = `user_provided`, `已核验` = `verified`, `未核验` = `unverified`. Stored run data uses the English tokens.

`not found` is a lookup result, not a fourth status. A completed lookup that finds nothing stays `unverified` and says not found. Do not invent the missing author, year, venue, pages, or DOI.

## Bibliographic verification is not a claim-level check

Finding a paper, or matching its title and year, does not mean the paper supports the sentence that cites it.

- Bibliographic verification: the record exists and the title, author family, and year agree with the named source.
- Claim-level check: someone read the abstract or the full text and the cited conclusion is actually there.

Automatic runs set `claim_checked` to false. A `verified` or `user_provided` cite is still not proof of the claim. Say that the conclusion was not checked until a human, or a later reading pass, has done that check.

## Lookup

1. Keep a user-supplied record as written. Do not normalize it against an API before labeling it `user_provided`.
2. With a DOI, query Crossref `https://api.crossref.org/works/{doi}`, then OpenAlex `https://api.openalex.org/works/doi:{doi}` if Crossref returns nothing. Compare title, author family, and year. On agreement, label `verified` and record which service answered.
3. Without a DOI, search Crossref `query.bibliographic` and then OpenAlex with the title plus the first author. Same agreement rule.
4. A publisher or journal page can count as `publisher_page` when the agent actually opened it and the same three fields agree. Do not claim that method otherwise.
5. Do not search a bare citation key as if it were a title. Keys such as `smith2023attention` are not queries.
6. Disagreement or a failed lookup stays `unverified`. Keep the original fields. Do not replace them with a different hit.
7. No network: do not pretend the lookup succeeded. Leave the item `user_provided` or `unverified`.

### Chinese databases

CNKI, Wanfang, and VIP usually need the user's own access. The agent does not invent Chinese bibliographic metadata when those sites are not queried. Ask the user to search and paste the record or the result text. Record that paste as `user_provided`. Absence from Crossref or OpenAlex is not evidence that the Chinese source does not exist.

## Output contract

Literature and writing artifacts are drafts. They are not ready-to-use review prose.

1. A generated paragraph that depends on the literature is labeled as a draft the author must rewrite and check. In Markdown use `## Draft paragraphs (author rewrite required)`. In the paper, the writing loop inserts a visible draft sentence at the start of each section.
2. End the artifact with these two sections, in this order, even when they are empty (`None.`):
   - `## Pending verification` — unverified items, not-found lookups, user-supplied records with doubts, sentences that still need a source, and claims that have not been read against the paper. Accepted Chinese heading: `待核实清单`.
   - `## Questions for the author` — scope, wording, and source choices that belong to the author. Accepted Chinese heading: `给作者的问题`.
3. A sentence that attributes a finding to the literature either cites a `verified` or `user_provided` key, or is marked `[needs source]`. It does not cite an `unverified` key.
4. Do not assert a research gap, or that "few studies" exist, only because a lookup came back empty. Write that this needs checking.

<!-- prompt-guardrail:start -->
Use only `user_provided` or `verified` sources as evidence. Statuses: `user_provided` means the user supplied the record and you must not rewrite it; `verified` means title, author family, and year were checked via Crossref, OpenAlex, or a publisher page and you recorded which; `unverified` means title-only, memory, lookup failed, or mismatch. Unverified items go in `## Pending verification` and are not evidence. Write "not found" instead of inventing author, year, venue, pages, or DOI. Bibliographic verification is not a claim-level check (`claim_checked` stays false). For CNKI, Wanfang, and VIP, ask the user to search and paste the record; do not invent it. End literature and writing output with `## Pending verification` and `## Questions for the author`. Generated paragraphs are drafts for the author to rewrite, not ready-to-use review prose. Mark a user-supplied BibTeX entry with `note = {verification: user_provided}`.
<!-- prompt-guardrail:end -->

## How a run assigns a status

The model does not get to promote an item to `verified` by saying so. When a skill emits BibTeX, `_extend_bibtex` in `src/nodes.py` calls `assess_entry` in `src/source_integrity.py`.

1. A `note` containing `verification: user_provided` becomes `user_provided`. The fields stay as given. No lookup rewrites them.
2. Otherwise the entry needs a title, an author, and a year. A DOI is checked on Crossref, then OpenAlex. Without a DOI, those services are queried with the title plus the first author. If title, author family, and year agree, the status is `verified` and the method is stored. The original fields are kept.
3. A title-only record, a lookup failure, a mismatch, or a citation key with no resolved DOI stays `unverified`. Missing fields are not filled in from the candidate.
4. `claim_checked` is forced false. The pending file states that the conclusion was not read off the paper.

Writing may use `\cite` only for `verified` and `user_provided` keys. Other cite commands are rewritten to `[needs source]`. Unverified entries remain in `references.bib` with a short `note`, so LaTeX can still compile. They are listed in `pending_verification.md`. `summary.json` records `verified`, `unverified`, and `user_provided` counts under `citation_integrity`. A large unverified set does not stop the PDF.

`citation-audit` follows this file. Its report includes the same three counts and does not treat `unverified` rows as evidence. The pipeline Finisher writes those counts even when that skill was not selected.

## What this does not do

- It does not prove the model followed the rule. Check the pending list and the bibliography notes.
- Crossref and OpenAlex cover Chinese literature, books, and grey literature poorly. Not found means not found there, not that the work is real or fake.
- There is no eval of whether these labels improve a review.
- There is no automated CNKI, Wanfang, or VIP client. That loop is the user's.
- Venue strings are not forced to match character for character (`NeurIPS` versus the proceedings title). The recorded check is title, author family, and year, plus the method name.
