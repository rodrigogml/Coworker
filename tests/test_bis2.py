import importlib.util
import sys
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "instance" / "skills" / "bis2" / "scripts" / "bis2.py"
SPEC = importlib.util.spec_from_file_location("bis2_tool", MODULE)
assert SPEC and SPEC.loader
bis2 = importlib.util.module_from_spec(SPEC)
sys.modules["bis2_tool"] = bis2
SPEC.loader.exec_module(bis2)


class Bis2ToolTests(unittest.TestCase):
    def test_status_choices_match_doc_fiscal_enum(self):
        self.assertEqual(bis2.DOC_FISCAL_STATUSES, (
            "SELLING", "STORED", "SOLD", "CANCELLING", "CANCELED", "ERROR",
            "ERROR_SYNC", "VOID", "SEFAZVALIDATING", "SEFAZPROBLEM", "SEFAZOFFLINE",
        ))

    def test_status_query_uses_hidden_read_only_biscmd_operation(self):
        args = bis2.build_parser().parse_args([
            "--profile", "example", "nfce-listagem-chaves",
            "--status", "SEFAZPROBLEM", "--start", "2026-08-01T00:00:00",
            "--end", "2026-08-08T23:59:59", "--limit", "25",
        ])
        command, mutating = bis2.build_biscmd_arguments(args)
        self.assertFalse(mutating)
        self.assertEqual(command[:3], ["-facade", "-nfceStatusList", "status"])
        self.assertIn("SEFAZPROBLEM", command)

    def test_detail_query_is_read_only_and_uses_document_id(self):
        args = bis2.build_parser().parse_args(["--profile", "example", "nfce-detail", "--id", "164129"])
        command, mutating = bis2.build_biscmd_arguments(args)
        self.assertFalse(mutating)
        self.assertEqual(command, ["-facade", "-docFiscalDetail", "164129"])

    def test_detail_query_supports_series_and_number(self):
        args = bis2.build_parser().parse_args([
            "--profile", "example", "nfce-detail", "--serie", "1", "--number", "123",
        ])
        command, mutating = bis2.build_biscmd_arguments(args)
        self.assertFalse(mutating)
        self.assertEqual(command, ["-facade", "-docFiscalDetail", "serie", "1", "number", "123"])

    def test_legacy_key_listing_still_requires_identifiers(self):
        args = bis2.build_parser().parse_args([
            "nfce-listagem-chaves", "--start", "2026-08-01T00:00:00",
            "--end", "2026-08-08T23:59:59",
        ])
        with self.assertRaises(bis2.Bis2ToolError):
            bis2.build_biscmd_arguments(args)

    def test_structured_biscmd_records_are_parsed(self):
        payload = bis2._sanitize('BISJSON {"id":1}', ())
        self.assertEqual(payload, 'BISJSON {"id":1}')

    def test_doc_fiscal_repair_builds_multiple_scoped_changes(self):
        args = bis2.build_parser().parse_args([
            "--profile", "example", "doc-fiscal-repair", "--doc-id", "123",
            "--set", "total=15.90", "--item-set", "456:cbenef=RJ123456",
            "--clear", "payment:789:changeValue", "--confirm",
        ])
        command, mutating = bis2.build_biscmd_arguments(args)
        self.assertTrue(mutating)
        self.assertEqual(command, [
            "-facade", "-docFiscalRepair", "docId", "123",
            "DOC", "total", "15.90", "ITEM", "456", "cbenef", "RJ123456",
            "PAYMENT", "789", "CLEAR", "changeValue", "confirm",
        ])


if __name__ == "__main__":
    unittest.main()
