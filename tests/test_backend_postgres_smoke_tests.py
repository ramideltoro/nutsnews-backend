import unittest

import scripts.backend_postgres_smoke_tests as smoke_tests


class BackendPostgresSmokeTestsTest(unittest.TestCase):
    def test_covers_required_categories(self):
        categories = {item["category"] for item in smoke_tests.READ_CHECKS + smoke_tests.WRITE_CHECKS}
        self.assertTrue(
            {
                "public_feed",
                "article",
                "search",
                "worker",
                "admin",
                "quota",
                "release_readiness",
                "dashboard",
            }.issubset(categories)
        )

    def test_write_checks_are_rollback_only(self):
        for item in smoke_tests.WRITE_CHECKS:
            query = item["query"].lower()
            self.assertIn("begin;", query)
            self.assertIn("rollback;", query)
            self.assertNotIn("commit;", query)

    def test_queries_do_not_select_unbounded_row_data(self):
        for item in smoke_tests.READ_CHECKS:
            self.assertNotIn("select *", item["query"].lower())
            self.assertIn("limit", item["explain"].lower())


if __name__ == "__main__":
    unittest.main()
