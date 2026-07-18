#!/usr/bin/env python3
from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from scripts import validate_backend_api_compatibility_contract


class BackendApiCompatibilityContractTests(unittest.TestCase):
    def test_contract_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_api_compatibility_contract.main(), 0)


if __name__ == "__main__":
    unittest.main()
