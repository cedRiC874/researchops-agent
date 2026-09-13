"""Bounded schema inventory checks; public files only, no Provider or writes."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchops import provider_completion_external_contract as contract


ROOT = Path(__file__).resolve().parents[1]
ERROR = "external_contract_schema_file_set_invalid"
SAFE_MESSAGE = "Provider completion external preregistration contract is invalid."


class GuardedEntries:
    def __init__(self, names, *, maximum_yields=None, read_error=False, close_error=False):
        self.names = iter(names)
        self.maximum_yields = maximum_yields
        self.read_error = read_error
        self.close_error = close_error
        self.yielded = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        if self.close_error:
            raise OSError("synthetic private diagnostic must not escape")
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self.read_error:
            raise OSError("synthetic private diagnostic must not escape")
        if self.maximum_yields is not None and self.yielded >= self.maximum_yields:
            raise AssertionError("enumeration continued after decisive rejection")
        name = next(self.names)
        self.yielded += 1
        return SimpleNamespace(name=name)


class SchemaEnumerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = json.loads((ROOT / contract.DESIGN_CONTRACT_RELATIVE_PATH).read_bytes())
        cls.supporting = json.loads((ROOT / contract.SUPPORTING_CONTRACT_RELATIVE_PATH).read_bytes())
        cls.names = sorted(set(contract._DIRECT_SCHEMA_FILES.values()) | set(contract._SUPPORTING_SCHEMA_PATHS.values()))
        assert len(cls.names) == 14

    def verify(self, entries):
        # The fourteen bound schema reads and their existing path/hash checks
        # remain real. Only directory enumeration is replaced, not its verdict.
        with patch.object(contract.os, "scandir", return_value=entries) as scan, patch.object(
            Path, "iterdir", side_effect=AssertionError("eager enumeration forbidden")
        ):
            try:
                return contract._verify_schemas(ROOT, self.design, self.supporting)
            finally:
                scan.assert_called_once_with(ROOT / contract.SCHEMA_ROOT_RELATIVE_PATH)

    def reject(self, entries):
        with self.assertRaises(contract.ExternalPreregistrationContractError) as caught:
            self.verify(entries)
        self.assertEqual(caught.exception.code, ERROR)
        self.assertEqual(str(caught.exception), SAFE_MESSAGE)
        self.assertTrue(entries.closed)

    def test_exact_fourteen_names_are_order_independent(self):
        entries = GuardedEntries(reversed(self.names))
        result = self.verify(entries)
        self.assertEqual(set(result), set(self.names))
        self.assertEqual(entries.yielded, 14)
        self.assertTrue(entries.closed)

    def test_unexpected_first_name_stops_before_a_second_entry(self):
        entries = GuardedEntries(["unexpected.schema.json"], maximum_yields=1)
        self.reject(entries)
        self.assertEqual(entries.yielded, 1)

    def test_fifteenth_entry_stops_without_consuming_a_sixteenth(self):
        entries = GuardedEntries(self.names + ["extra.schema.json"], maximum_yields=15)
        self.reject(entries)
        self.assertEqual(entries.yielded, 15)

    def test_duplicate_is_rejected_at_second_entry(self):
        entries = GuardedEntries([self.names[0], self.names[0]], maximum_yields=2)
        self.reject(entries)
        self.assertEqual(entries.yielded, 2)

    def test_expected_name_still_requires_regular_non_link_file(self):
        original_link_check = contract._is_link_like
        original_is_file = Path.is_file
        target = ROOT / contract.SCHEMA_ROOT_RELATIVE_PATH / self.names[0]
        for invalid_kind in ("link", "directory"):
            with self.subTest(kind=invalid_kind):
                entries = GuardedEntries([self.names[0]], maximum_yields=1)

                def is_link(path):
                    if entries.yielded and path == target and invalid_kind == "link":
                        return True
                    return original_link_check(path)

                def is_file(path):
                    if entries.yielded and path == target and invalid_kind == "directory":
                        return False
                    return original_is_file(path)

                with patch.object(contract, "_is_link_like", side_effect=is_link), patch.object(Path, "is_file", is_file):
                    self.reject(entries)
                self.assertEqual(entries.yielded, 1)

    def test_missing_and_empty_inventory_remain_rejected(self):
        for names in (self.names[:-1], []):
            with self.subTest(count=len(names)):
                self.reject(GuardedEntries(names))

    def test_enumeration_read_and_close_failures_are_sanitized(self):
        for options in ({"read_error": True}, {"close_error": True}):
            with self.subTest(options=options):
                self.reject(GuardedEntries(self.names, **options))

    def test_enumeration_open_failure_is_sanitized(self):
        with patch.object(contract.os, "scandir", side_effect=OSError("synthetic private detail")):
            with self.assertRaises(contract.ExternalPreregistrationContractError) as caught:
                contract._verify_schemas(ROOT, self.design, self.supporting)
        self.assertEqual(caught.exception.code, ERROR)
        self.assertEqual(str(caught.exception), SAFE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
