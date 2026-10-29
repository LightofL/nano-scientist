"""Bibliographic status rules. Lookups are injected; these tests do not use the network.

Run from the repo root or directly:
    python3 scripts/check_source_integrity.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.nodes import _extend_bibtex
from src.source_integrity import (
    assess_entry,
    citation_summary,
    ensure_output_contract,
    label_draft_section,
    load_prompt_guardrail,
    render_pending_document,
    resolve_missing_citation,
    split_cite_keys,
    stamp_entry,
    strip_unverified_cites,
)


ATTENTION = (
    "@inproceedings{vaswani2017attention,\n"
    "  author = {Vaswani, Ashish and Shazeer, Noam},\n"
    "  title  = {Attention Is All You Need},\n"
    "  year   = {2017},\n"
    "}\n"
)


def _crossref_doi(title="Attention Is All You Need", year=2017, family="Vaswani"):
    return {
        "message": {
            "DOI": "10.5555/3295222",
            "title": [title],
            "author": [{"family": family, "given": "Ashish"}],
            "published": {"date-parts": [[year]]},
            "container-title": ["Advances in Neural Information Processing Systems"],
            "type": "proceedings-article",
        }
    }


class AssessEntryTests(unittest.TestCase):
    def test_user_provided_does_not_lookup_or_rewrite(self):
        entry = (
            "@article{ada2020notes,\n"
            "  author = {Ada, Lovelace},\n"
            "  title = {Analytical notes},\n"
            "  year = {2020},\n"
            "  note = {verification: user_provided},\n"
            "}\n"
        )

        def fetch(_url):
            raise AssertionError("user_provided records must not be looked up")

        record = assess_entry(entry, fetch_json=fetch)
        stamped = stamp_entry(entry, record)
        self.assertEqual(record["status"], "user_provided")
        self.assertFalse(record["claim_checked"])
        self.assertIn("Ada, Lovelace", stamped)
        self.assertIn("Analytical notes", stamped)
        self.assertIn("verification: user_provided", stamped)
        self.assertEqual(stamped.count("verification:"), 1)

    def test_title_author_year_match_is_verified_without_rewriting_fields(self):
        def fetch(url):
            self.assertIn("query.bibliographic", url)
            return {
                "message": {
                    "items": [{
                        "title": ["Attention Is All You Need"],
                        "author": [{"family": "Vaswani", "given": "Ashish"}],
                        "published": {"date-parts": [[2017]]},
                        "container-title": ["NeurIPS"],
                        "type": "proceedings-article",
                    }]
                }
            }

        record = assess_entry(ATTENTION, fetch_json=fetch)
        stamped = stamp_entry(ATTENTION, record)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["method"], "crossref_title")
        self.assertFalse(record["claim_checked"])
        self.assertIn("Attention Is All You Need", stamped)
        self.assertIn("Vaswani, Ashish", stamped)
        self.assertNotIn("doi", stamped.lower())

    def test_mismatch_stays_unverified_and_keeps_original_title(self):
        def fetch(_url):
            return {
                "message": {
                    "items": [{
                        "title": ["A Completely Different Paper"],
                        "author": [{"family": "Other", "given": "A"}],
                        "published": {"date-parts": [[1999]]},
                    }]
                }
            }

        record = assess_entry(ATTENTION, fetch_json=fetch)
        stamped = stamp_entry(ATTENTION, record)
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["result"], "mismatch")
        self.assertIn("Attention Is All You Need", stamped)
        self.assertNotIn("A Completely Different Paper", stamped)

    def test_lookup_failure_is_unverified(self):
        def fetch(_url):
            raise TimeoutError("down")

        record = assess_entry(ATTENTION, fetch_json=fetch)
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["result"], "not_found")
        self.assertNotEqual(record["status"], "verified")

    def test_title_only_does_not_adopt_a_resolved_record(self):
        entry = (
            "@article{onlytitle,\n"
            "  title = {Attention Is All You Need},\n"
            "  doi = {10.5555/3295222},\n"
            "}\n"
        )

        def fetch(url):
            self.assertIn("10.5555/3295222", url)
            return _crossref_doi()

        record = assess_entry(entry, fetch_json=fetch)
        stamped = stamp_entry(entry, record)
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["result"], "insufficient")
        self.assertNotIn("Vaswani", stamped)
        self.assertIn("Attention Is All You Need", stamped)

    def test_chinese_miss_asks_for_human_databases(self):
        entry = (
            "@article{cn2021,\n"
            "  author = {张, 三},\n"
            "  title = {乡村振兴与数字普惠金融},\n"
            "  year = {2021},\n"
            "}\n"
        )

        def fetch(_url):
            return {"message": {"items": []}}

        record = assess_entry(entry, fetch_json=fetch)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("CNKI", record["detail"])
        self.assertIn("Wanfang", record["detail"])
        self.assertIn("VIP", record["detail"])

    def test_year_mismatch_is_not_verified(self):
        def fetch(_url):
            return {
                "message": {
                    "items": [{
                        "title": ["Attention Is All You Need"],
                        "author": [{"family": "Vaswani", "given": "Ashish"}],
                        "published": {"date-parts": [[2016]]},
                    }]
                }
            }

        record = assess_entry(ATTENTION, fetch_json=fetch)
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["result"], "mismatch")

    def test_openalex_verifies_when_crossref_has_no_hit(self):
        def fetch(url):
            if "api.crossref.org" in url:
                return {"message": {"items": []}}
            self.assertIn("api.openalex.org", url)
            return {
                "results": [{
                    "title": "Attention Is All You Need",
                    "publication_year": 2017,
                    "authorships": [{"author": {"display_name": "Ashish Vaswani"}}],
                }]
            }

        record = assess_entry(ATTENTION, fetch_json=fetch)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["method"], "openalex_title")
        self.assertFalse(record["claim_checked"])

    def test_self_labeled_verified_is_not_trusted(self):
        entry = ATTENTION.replace(
            "  year   = {2017},\n}",
            "  year   = {2017},\n  note   = {verification: verified},\n}",
        )

        def fetch(_url):
            raise TimeoutError("down")

        record = assess_entry(entry, fetch_json=fetch)
        stamped = stamp_entry(entry, record)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("verification: unverified", stamped)
        self.assertNotIn("verification: verified", stamped)

    def test_doi_match_uses_crossref_doi(self):
        entry = ATTENTION.replace(
            "  year   = {2017},\n",
            "  year   = {2017},\n  doi    = {10.5555/3295222},\n",
        )

        def fetch(url):
            self.assertNotIn("query.bibliographic", url)
            if "api.crossref.org/works/10.5555" in url:
                return _crossref_doi()
            raise AssertionError(url)

        record = assess_entry(entry, fetch_json=fetch)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["method"], "crossref_doi")
        self.assertIn("claim was not checked", record["detail"])


class OutputContractTests(unittest.TestCase):
    def test_unverified_cite_is_not_evidence(self):
        ledger = {
            "good2017": {"status": "verified", "method": "crossref_title", "claim_checked": False},
            "user2020": {"status": "user_provided", "method": "user_provided", "claim_checked": False},
            "maybe2024": {"status": "unverified", "method": None, "claim_checked": False},
        }
        evidence, pending = split_cite_keys(["good2017", "user2020", "maybe2024"], ledger)
        self.assertEqual(evidence, ["good2017", "user2020"])
        self.assertEqual(pending, ["maybe2024"])
        body = r"Real \cite{good2017}. Mixed \citep{good2017,maybe2024}. Fake \cite{maybe2024}."
        stripped = strip_unverified_cites(body, ledger)
        self.assertIn(r"\cite{good2017}", stripped)
        self.assertIn(r"\citep{good2017}", stripped)
        self.assertNotIn("maybe2024", stripped)
        self.assertIn("[needs source]", stripped)
        counts = citation_summary(ledger)
        self.assertEqual(counts["verified"], 1)
        self.assertEqual(counts["unverified"], 1)
        self.assertEqual(counts["user_provided"], 1)
        self.assertEqual(counts["claim_checked"], 0)

    def test_contract_sections_append_once(self):
        text = "Draft finding without a source.\n"
        once = ensure_output_contract(text, {})
        twice = ensure_output_contract(once, {})
        self.assertEqual(once, twice)
        self.assertIn("## Draft paragraphs (author rewrite required)", once)
        self.assertIn("## Pending verification", once)
        self.assertIn("## Questions for the author", once)
        self.assertLess(once.index("## Pending verification"), once.index("## Questions for the author"))

    def test_chinese_headings_count_as_present(self):
        text = "正文\n\n## 待核实清单\n\n无\n\n## 给作者的问题\n\n无\n\n段落草稿已经在上面。\n"
        # draft marker is the Chinese phrase 段落草稿
        self.assertIn("段落草稿", text)
        updated = ensure_output_contract(text, {})
        self.assertEqual(updated, text)

    def test_draft_label_is_visible_in_latex(self):
        body = "\\section{Introduction}\n\nStudies disagree \\cite{missing}."
        labeled = label_draft_section(body)
        self.assertIn("Draft for author rewrite", labeled)
        self.assertIn("not ready-to-use review prose", labeled)
        self.assertTrue(labeled.index("\\section{Introduction}") < labeled.index("Draft for author rewrite"))

    def test_missing_key_is_placeholder_not_a_title_query(self):
        def fetch(_url):
            raise AssertionError("a cite key must not be looked up as a title")

        entry, record = resolve_missing_citation("smith2023attention", fetch_json=fetch)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("Pending verification", entry)
        self.assertNotIn("smith2023attention", entry.split("note", 1)[-1])

    def test_doi_key_resolves_as_verified_bibliography(self):
        def fetch(url):
            self.assertIn("api.crossref.org/works/10.5555/3295222", url)
            return _crossref_doi()

        entry, record = resolve_missing_citation("10.5555/3295222", fetch_json=fetch)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["method"], "crossref_doi")
        self.assertFalse(record["claim_checked"])
        self.assertIn("Attention Is All You Need", entry)

    def test_prompt_guardrail_comes_from_the_rule_file(self):
        text = load_prompt_guardrail("skills")
        self.assertIn("unverified", text)
        self.assertIn("user_provided", text)
        self.assertIn("CNKI", text)
        self.assertIn("claim-level", text)

    def test_pending_document_does_not_treat_unverified_as_evidence(self):
        document = render_pending_document({
            "maybe2024": {
                "status": "unverified",
                "method": None,
                "result": "not_found",
                "claim_checked": False,
                "detail": "Title lookup failed or returned nothing. Result: not found.",
            }
        })
        self.assertIn("not evidence", document)
        self.assertIn("does not block the PDF", document)
        self.assertIn("`maybe2024`", document)
        self.assertIn("unverified=1", document)


class PipelineWiringTests(unittest.TestCase):
    def test_extend_bibtex_keeps_user_record_and_writes_pending_file(self):
        entry = (
            "@article{ada2020notes,\n"
            "  author = {Ada, Lovelace},\n"
            "  title = {Analytical notes},\n"
            "  year = {2020},\n"
            "  note = {verification: user_provided},\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            shared = {
                "bibtex_entries": [],
                "citation_ledger": {},
                "output_path": tmp,
            }
            added = _extend_bibtex(shared, [entry])
            self.assertEqual(shared["citation_ledger"]["ada2020notes"]["status"], "user_provided")
            self.assertIn("Ada, Lovelace", added[0])
            pending = Path(tmp, "pending_verification.md").read_text(encoding="utf-8")
            self.assertIn("user_provided=1", pending)
            summary = citation_summary(shared["citation_ledger"])
            self.assertEqual(summary["evidence_keys"], ["ada2020notes"])
            self.assertEqual(summary["pending_keys"], [])

    def test_rule_file_is_not_a_skill_package(self):
        root = Path(__file__).resolve().parents[1]
        index = json.loads((root / "skills" / "skills.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in index["skills"]]
        self.assertNotIn("source-integrity", ids)
        self.assertFalse((root / "skills" / "source-integrity" / "SKILL.md").exists())
        self.assertTrue((root / "skills" / "shared-references" / "source-integrity.md").exists())


if __name__ == "__main__":
    unittest.main()
