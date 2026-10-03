from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from finscope.catalog import Catalog, CatalogError


DATA_PATH = Path(__file__).parents[1] / "data" / "catalog.json"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(DATA_PATH)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def changed_catalog(self, transform):
        document = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        transform(document)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return Catalog(path)

    def test_eight_products_six_terms_complete_and_synthetic(self):
        products = self.catalog.products()
        self.assertEqual(len(products), 8)
        self.assertTrue(all(product["synthetic"] is True and len(product["clauses"]) == 6 and not product["validation_errors"] for product in products))
        terms = self.catalog.search("", limit=100)
        self.assertEqual(len(terms), 48)
        self.assertTrue(all(term["quote"] == term["content"] and term["version"] == "2026.1" for term in terms))

    def test_effective_date_filtered_before_keyword_ranking(self):
        current = self.catalog.search("50000", product_id="FS-CASH")
        self.assertEqual(current, [])
        historic = self.catalog.search("50000", as_of="2025-12-31", product_id="FS-CASH")
        self.assertEqual(len(historic), 1)
        self.assertEqual(historic[0]["version"], "2025.1")
        boundary = self.catalog.search("100000", as_of="2026-01-01", product_id="FS-CASH")
        self.assertEqual(boundary[0]["version"], "2026.1")

    def test_end_date_is_exclusive_and_no_future_source_leaks(self):
        self.assertEqual(self.catalog.products("2027-01-01"), [])
        self.assertEqual(self.catalog.search("费用", as_of="2024-12-31"), [])

    def test_conflicting_active_versions_are_excluded(self):
        def transform(document):
            duplicate = deepcopy(document["products"][0])
            duplicate["version"] = "conflicting-version"
            document["products"].append(duplicate)
        catalog = self.changed_catalog(transform)
        product = next(product for product in catalog.products() if product["product_id"] == "FS-CASH")
        self.assertEqual(product["validation_errors"], ["version_conflict"])
        self.assertEqual(catalog.search("费用", product_id="FS-CASH"), [])
        self.assertNotEqual(catalog.fingerprint(), self.catalog.fingerprint())

    def test_products_are_detached_snapshots(self):
        products = self.catalog.products()
        products[0]["clauses"][0]["content"] = "tampered"
        products[0]["amount_bounds"]["min"] = "999999.00"
        self.assertNotIn("tampered", str(self.catalog.products()))

    def test_retrieval_is_deterministic_and_explicit_keyword_baseline(self):
        first = self.catalog.search("费用")
        self.assertEqual(first, self.catalog.search("费用"))
        self.assertTrue(all(term["retrieval_method"] == "keyword_character_overlap_v1" for term in first))
        self.assertTrue(all(term["synthetic"] is True and term["as_of"] == "2026-10-03" for term in first))
        self.assertEqual(self.catalog.search("zzzznonexistent"), [])

    def test_missing_fee_and_malformed_clause_are_explicit_errors(self):
        def transform(document):
            document["products"][0]["fees_bps"] = None
            document["products"][0]["clauses"][0]["kind"] = {"not": "a string"}
        catalog = self.changed_catalog(transform)
        product = next(product for product in catalog.products() if product["product_id"] == "FS-CASH")
        self.assertIn("missing_or_invalid_fees_bps", product["validation_errors"])
        self.assertIn("invalid_clause", product["validation_errors"])

    def test_invalid_dates_and_queries_rejected(self):
        for value in ("2026-02-30", "2026-1-1", "not-a-date", True):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.catalog.search("费用", as_of=value)
        for value in (None, 1, "x" * 1001):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.catalog.search(value)

    def test_invalid_term_id_cannot_enter_source_citations(self):
        catalog = self.changed_catalog(lambda document: document["products"][0]["clauses"][0].update(term_id="bad/source\nidentifier"))
        product = next(product for product in catalog.products() if product["product_id"] == "FS-CASH")
        self.assertIn("invalid_term_id", product["validation_errors"])
        self.assertFalse(any(term["term_id"] == "bad/source\nidentifier" for term in catalog.search("", product_id="FS-CASH")))

    def test_term_id_shared_across_active_products_is_rejected(self):
        def transform(document):
            cash = next(product for product in document["products"] if product["product_id"] == "FS-CASH" and product["version"] == "2026.1")
            flex = next(product for product in document["products"] if product["product_id"] == "FS-FLEX")
            shared = next(clause for clause in cash["clauses"] if clause["kind"] == "fees")["term_id"]
            next(clause for clause in flex["clauses"] if clause["kind"] == "fees")["term_id"] = shared
        catalog = self.changed_catalog(transform)
        affected = [product for product in catalog.products() if product["product_id"] in {"FS-CASH", "FS-FLEX"}]
        self.assertTrue(all("ambiguous_term_id" in product["validation_errors"] for product in affected))
        self.assertFalse(any(term["product_id"] in {"FS-CASH", "FS-FLEX"} for term in catalog.search("基点")))
        # Same IDs in a historical, non-overlapping version remain unambiguous.
        historical = catalog.search("基点", as_of="2025-12-31")
        self.assertEqual({term["product_id"] for term in historical}, {"FS-CASH"})
        self.assertTrue(any(term["term_id"] == "FS-CASH-fees" and term["version"] == "2025.1" for term in historical))

    def test_non_synthetic_catalog_cannot_load(self):
        with self.assertRaises(CatalogError):
            self.changed_catalog(lambda document: document.update(synthetic=False))

    def test_fingerprint_depends_on_active_terms_and_date(self):
        first = self.catalog.fingerprint()
        self.assertEqual(len(first), 64)
        self.assertEqual(first, Catalog(DATA_PATH).fingerprint())
        catalog = self.changed_catalog(lambda document: document["products"][0]["clauses"][0].update(content="修改后的模拟条款"))
        self.assertNotEqual(first, catalog.fingerprint())
        self.assertNotEqual(first, self.catalog.fingerprint("2026-10-04"))

    def test_data_copies_and_twenty_unconfirmed_profiles(self):
        root = Path(__file__).parents[1]
        for filename in ("catalog.json", "demo_users.json"):
            self.assertEqual((root / "data" / filename).read_bytes(), (root / "src" / "finscope" / "data" / filename).read_bytes())
        document = json.loads((root / "data" / "demo_users.json").read_text(encoding="utf-8"))
        self.assertEqual(len(document["users"]), 20)
        self.assertFalse(document["profiles_are_confirmed"])
        self.assertEqual(len({user["user_id"] for user in document["users"]}), 20)


if __name__ == "__main__":
    unittest.main()
