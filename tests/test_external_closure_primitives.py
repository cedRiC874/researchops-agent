from __future__ import annotations

import ast
import base64
import copy
import hashlib
import inspect
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from researchops_external_closure import (
    ExternalClosurePrimitiveError,
    FinalClosureResult,
    FinalDocumentBytes,
    PreReceiptDocumentBytes,
    PreReceiptRejected,
    ReceiptProjectionReady,
    UtcTimestamp,
    build_signature_message,
    canonical_json_bytes,
    compute_document_sha256,
    compute_ledger_entry_sha256,
    compute_ledger_resulting_head,
    decode_strict_json_object,
    derive_ed25519_key_id,
    parse_utc_timestamp,
    utc_interval_is_positive_and_at_most,
    verify_document_sha256,
    verify_ed25519_signature,
    verify_ledger_hashes,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "researchops_external_closure"

DOCUMENT_HASH = "434f85bd336a06d2bc5c31f669171ffaecd83e8981ac70183f274c4cda28ef94"
ENTRY_HASH = "ff34ec4ec40038b7a497bbbec742789c8e88d6bbb617b145f606017de4712301"
RESULTING_HEAD = "e11d75edaf537a2a122a26d77c16ad188915bf2034afaf554b0e0b552936e393"
PUBLIC_KEY_B64 = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
KEY_ID = "PCEKEY-90E59FE8D2167D382EED654BFCC9E0A7"
SIGNATURE_B64 = (
    "IrtMTCTKq5FwM5G6X0IuKObz70Th5ko3Izf1ilqKrJ/S+RjSCCMxm3V3kvsvgTXo"
    "v1/le6rTHhDjdyrQceM3Ag=="
)


def candidate_document() -> dict[str, object]:
    return {
        "schema_version": "provider-completion-candidate-freeze/1.0",
        "document_type": "completion_telemetry_candidate_freeze",
        "candidate_id": "PCECANDIDATE-0123456789ABCDEF0123456789ABCDEF",
        "document_sha256": "0" * 64,
        "signatures": [],
    }


def ledger_entry() -> dict[str, object]:
    return {
        "schema_version": "provider-completion-external-ledger-entry/1.0",
        "document_type": "completion_telemetry_external_ledger_entry",
        "entry_type": "candidate_frozen",
        "ledger_id": "PCELEDGER-0123456789ABCDEF0123456789ABCDEF",
        "sequence": 1,
        "previous_head_sha256": "0" * 64,
        "occurred_at_utc": "2026-09-05T12:34:56.789Z",
        "candidate_freeze_receipt_sha256": DOCUMENT_HASH,
        "preregistration_envelope_sha256": None,
        "authorization_grant_sha256": None,
        "consumption_receipt_sha256": None,
        "closure_receipt_sha256": None,
        "entry_sha256": "0" * 64,
        "resulting_head_sha256": "0" * 64,
        "signatures": [],
    }


class StrictJsonTests(unittest.TestCase):
    def assert_error(self, code: str, callable_object, *args, **kwargs) -> None:
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            callable_object(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_strict_json_accepts_a_bounded_utf8_object(self) -> None:
        raw = b'{"nested":{"ok":true},"snow":"\xe9\x9b\xaa"}'
        self.assertEqual(
            decode_strict_json_object(raw, max_bytes=len(raw)),
            {"nested": {"ok": True}, "snow": "\u96ea"},
        )

    def test_strict_json_rejects_bom_invalid_utf8_duplicates_and_nonfinite(self) -> None:
        cases = (
            (b"\xef\xbb\xbf{}", "external_closure_json_bom_forbidden"),
            (b'{"x":"\xff"}', "external_closure_json_utf8_invalid"),
            (b'{"x":1,"x":2}', "external_closure_json_duplicate_key"),
            (b'{"x":{"y":1,"y":2}}', "external_closure_json_duplicate_key"),
            (b'{"x":NaN}', "external_closure_json_nonfinite"),
            (b'{"x":Infinity}', "external_closure_json_nonfinite"),
            (b'{"x":1e999}', "external_closure_json_nonfinite"),
            (b'{"x":"\\ud800"}', "external_closure_json_unicode_invalid"),
            (b'{"\\udfff":1}', "external_closure_json_unicode_invalid"),
        )
        for payload, code in cases:
            with self.subTest(payload=payload):
                self.assert_error(
                    code, decode_strict_json_object, payload, max_bytes=100
                )

    def test_strict_json_enforces_maximum_and_object_top_level(self) -> None:
        self.assert_error(
            "external_closure_json_too_large",
            decode_strict_json_object,
            b'{"x":1}',
            max_bytes=6,
        )
        self.assert_error(
            "external_closure_json_object_required",
            decode_strict_json_object,
            b"[]",
            max_bytes=2,
        )
        self.assert_error(
            "external_closure_json_max_bytes_invalid",
            decode_strict_json_object,
            b"{}",
            max_bytes=True,
        )

    def test_strict_json_normalizes_decoder_value_error(self) -> None:
        with patch(
            "researchops_external_closure.primitives.json.loads",
            side_effect=ValueError("untrusted decoder detail"),
        ):
            self.assert_error(
                "external_closure_json_invalid",
                decode_strict_json_object,
                b'{"x":1}',
                max_bytes=7,
            )

    def test_canonical_json_vector_and_invalid_values(self) -> None:
        value = {"z": "\u96ea", "a": [True, None, 1, 1.5]}
        self.assertEqual(
            canonical_json_bytes(value),
            b'{"a":[true,null,1,1.5],"z":"\xe9\x9b\xaa"}',
        )
        self.assert_error(
            "external_closure_json_nonfinite",
            canonical_json_bytes,
            {"x": float("inf")},
        )
        self.assert_error(
            "external_closure_json_key_invalid", canonical_json_bytes, {1: "x"}
        )
        cyclic: list[object] = []
        cyclic.append(cyclic)
        self.assert_error(
            "external_closure_json_cycle", canonical_json_bytes, cyclic
        )


class TimestampTests(unittest.TestCase):
    def test_utc_parser_returns_an_aware_utc_value(self) -> None:
        parsed = parse_utc_timestamp("2026-09-05T12:34:56.789012Z")
        self.assertIsInstance(parsed, UtcTimestamp)
        self.assertIs(parsed.whole_second_utc.tzinfo, timezone.utc)
        self.assertEqual(parsed.isoformat(), "2026-09-05T12:34:56.789012Z")
        self.assertEqual(
            parse_utc_timestamp("2026-09-05T12:34:56Z").fraction_digits, ""
        )

    def test_utc_parser_preserves_arbitrary_precision_and_total_order(self) -> None:
        base = parse_utc_timestamp("2026-09-05T12:34:56.1234567Z")
        same = parse_utc_timestamp("2026-09-05T12:34:56.123456700000Z")
        later_fraction = parse_utc_timestamp(
            "2026-09-05T12:34:56.1234567000000000000000000001Z"
        )
        next_second = parse_utc_timestamp("2026-09-05T12:34:57Z")
        self.assertEqual(base, same)
        self.assertEqual(hash(base), hash(same))
        self.assertEqual(base.isoformat(), "2026-09-05T12:34:56.1234567Z")
        self.assertLess(base, later_fraction)
        self.assertLess(later_fraction, next_second)
        self.assertGreater(next_second, base)

    def test_utc_parser_rejects_non_z_or_ambiguous_forms(self) -> None:
        for value in (
            "2026-09-05 12:34:56Z",
            "2026-09-05t12:34:56Z",
            "2026-09-05T12:34:56+00:00",
            "2026-09-05T12:34:56z",
            "2026-12-31T23:59:60Z",
            "2026-02-30T12:34:56Z",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "^external_closure_timestamp_invalid$",
                ):
                    parse_utc_timestamp(value)

    def test_exact_interval_bound_includes_horizon_but_not_one_fraction_later(self) -> None:
        start = parse_utc_timestamp("2026-09-05T00:00:00.9Z")
        at_horizon = parse_utc_timestamp("2026-09-06T00:00:00.9000000000Z")
        just_inside = parse_utc_timestamp(
            "2026-09-06T00:00:00.8999999999999999999999999999Z"
        )
        just_outside = parse_utc_timestamp(
            "2026-09-06T00:00:00.9000000000000000000000000001Z"
        )
        self.assertTrue(
            utc_interval_is_positive_and_at_most(
                start=start, end=at_horizon, maximum_seconds=86_400
            )
        )
        self.assertTrue(
            utc_interval_is_positive_and_at_most(
                start=start, end=just_inside, maximum_seconds=86_400
            )
        )
        self.assertFalse(
            utc_interval_is_positive_and_at_most(
                start=start, end=just_outside, maximum_seconds=86_400
            )
        )
        self.assertFalse(
            utc_interval_is_positive_and_at_most(
                start=start, end=start, maximum_seconds=86_400
            )
        )


class HashProtocolTests(unittest.TestCase):
    def test_document_hash_fixed_vector_and_omitted_fields(self) -> None:
        document = candidate_document()
        self.assertEqual(compute_document_sha256(document), DOCUMENT_HASH)
        changed_signature = copy.deepcopy(document)
        changed_signature["signatures"] = [{"untrusted": "ignored by hash formula"}]
        changed_signature["document_sha256"] = "f" * 64
        self.assertEqual(compute_document_sha256(changed_signature), DOCUMENT_HASH)

    def test_document_hash_verification_detects_tampering(self) -> None:
        document = candidate_document()
        document["document_sha256"] = DOCUMENT_HASH
        verify_document_sha256(document)
        document["candidate_id"] = "PCECANDIDATE-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_document_hash_mismatch$",
        ):
            verify_document_sha256(document)
        document["document_type"] = "invented"
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_document_type_unsupported$",
        ):
            compute_document_sha256(document)

    def test_ledger_hash_vectors_and_verification(self) -> None:
        entry = ledger_entry()
        self.assertEqual(compute_ledger_entry_sha256(entry), ENTRY_HASH)
        self.assertEqual(
            compute_ledger_resulting_head("0" * 64, ENTRY_HASH), RESULTING_HEAD
        )
        entry["entry_sha256"] = ENTRY_HASH
        entry["resulting_head_sha256"] = RESULTING_HEAD
        verify_ledger_hashes(entry)

    def test_ledger_hashes_detect_body_and_head_tampering(self) -> None:
        entry = ledger_entry()
        entry["entry_sha256"] = ENTRY_HASH
        entry["resulting_head_sha256"] = RESULTING_HEAD
        body_tamper = copy.deepcopy(entry)
        body_tamper["occurred_at_utc"] = "2026-09-05T12:34:57.789Z"
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_ledger_entry_hash_mismatch$",
        ):
            verify_ledger_hashes(body_tamper)
        head_tamper = copy.deepcopy(entry)
        head_tamper["resulting_head_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_ledger_head_hash_mismatch$",
        ):
            verify_ledger_hashes(head_tamper)
        invalid_sequence = copy.deepcopy(entry)
        invalid_sequence["sequence"] = True
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_ledger_sequence_invalid$",
        ):
            compute_ledger_entry_sha256(invalid_sequence)


class SignatureProtocolTests(unittest.TestCase):
    def test_key_id_message_and_signature_fixed_vectors(self) -> None:
        self.assertEqual(derive_ed25519_key_id(PUBLIC_KEY_B64), KEY_ID)
        message = build_signature_message(
            document_type="completion_telemetry_candidate_freeze",
            role="freeze_authority",
            key_id=KEY_ID,
            signed_sha256=DOCUMENT_HASH,
        )
        self.assertEqual(
            hashlib.sha256(message).hexdigest(),
            "dbe89778b3f2c0c45bdb359997441786766f5ce99caadfc28b8ae1097d9585d4",
        )
        verify_ed25519_signature(
            public_key_b64=PUBLIC_KEY_B64,
            signature_b64=SIGNATURE_B64,
            message=message,
        )

    def test_public_signature_verification_detects_tampering(self) -> None:
        message = build_signature_message(
            document_type="completion_telemetry_candidate_freeze",
            role="freeze_authority",
            key_id=KEY_ID,
            signed_sha256=DOCUMENT_HASH,
        )
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "^external_closure_signature_invalid$",
        ):
            verify_ed25519_signature(
                public_key_b64=PUBLIC_KEY_B64,
                signature_b64=SIGNATURE_B64,
                message=message + b"tamper",
            )
        secret_like = "sk-live-this-must-not-be-echoed"
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:
            verify_ed25519_signature(
                public_key_b64=PUBLIC_KEY_B64,
                signature_b64=secret_like,
                message=message,
            )
        self.assertNotIn(secret_like, str(caught.exception))

    def test_signature_is_interoperable_with_a_fixed_private_test_key(self) -> None:
        private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.assertEqual(base64.b64encode(public_key).decode("ascii"), PUBLIC_KEY_B64)
        message = build_signature_message(
            document_type="completion_telemetry_candidate_freeze",
            role="freeze_authority",
            key_id=KEY_ID,
            signed_sha256=DOCUMENT_HASH,
        )
        self.assertEqual(base64.b64encode(private_key.sign(message)).decode("ascii"), SIGNATURE_B64)


class DataTransferObjectTests(unittest.TestCase):
    def make_pre_receipt(self) -> PreReceiptDocumentBytes:
        return PreReceiptDocumentBytes(*(b"{}",) * 9)

    def test_document_containers_and_results_are_frozen(self) -> None:
        pre_receipt = self.make_pre_receipt()
        final = FinalDocumentBytes(pre_receipt, b"{}", b"{}")
        ready = ReceiptProjectionReady(
            b"{}", "a" * 64, True, True, None, ()
        )
        rejected = PreReceiptRejected("pce_invalid")
        result = FinalClosureResult(
            "unclosed_invalid", False, False, "pce_invalid", None, None
        )
        self.assertEqual(final.pre_receipt, pre_receipt)
        self.assertEqual(ready.kind, "receipt_projection_ready")
        self.assertEqual(rejected.kind, "unclosed_invalid")
        self.assertEqual(
            (
                result.verification_network_calls,
                result.verification_provider_calls,
                result.verification_key_loads,
            ),
            (0, 0, 0),
        )
        with self.assertRaises(FrozenInstanceError):
            pre_receipt.trust_manifest = b"tamper"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.status = "closed_evidence_verified"  # type: ignore[misc]

    def test_result_truth_tables_and_nested_types_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "invalid_evidence_cannot_be_closure_eligible"
        ):
            ReceiptProjectionReady(
                b"{}", "a" * 64, False, True, "closure_invalid", ()
            )
        with self.assertRaisesRegex(TypeError, "receipt_document_sha256"):
            ReceiptProjectionReady(
                b"{}", [], True, True, None, ()  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "unclosed_invalid_truth_values_invalid"):
            FinalClosureResult(
                "unclosed_invalid", True, True, "closure_invalid", None, None
            )
        with self.assertRaisesRegex(TypeError, "error_code"):
            FinalClosureResult(
                "unclosed_invalid", False, False, [], None, None  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "closed_result_requires_receipt_hash"):
            FinalClosureResult(
                "closed_failure_attested", False, False, None, None, None
            )

    def test_document_containers_reject_non_bytes(self) -> None:
        values: list[object] = [b"{}"] * 9
        values[4] = "{}"
        with self.assertRaisesRegex(TypeError, "must_be_bytes"):
            PreReceiptDocumentBytes(*values)  # type: ignore[arg-type]


class CapabilityAndPublicApiTests(unittest.TestCase):
    def test_public_function_signatures_are_fixed(self) -> None:
        expected = {
            decode_strict_json_object: ("payload", "max_bytes"),
            canonical_json_bytes: ("value",),
            parse_utc_timestamp: ("value",),
            compute_document_sha256: ("document",),
            verify_document_sha256: ("document",),
            derive_ed25519_key_id: ("public_key_b64",),
            build_signature_message: (
                "document_type",
                "role",
                "key_id",
                "signed_sha256",
            ),
            verify_ed25519_signature: (
                "public_key_b64",
                "signature_b64",
                "message",
            ),
            compute_ledger_entry_sha256: ("entry",),
            compute_ledger_resulting_head: (
                "previous_head_sha256",
                "entry_sha256",
            ),
            verify_ledger_hashes: ("entry",),
            utc_interval_is_positive_and_at_most: (
                "start",
                "end",
                "maximum_seconds",
            ),
        }
        for function, names in expected.items():
            with self.subTest(function=function.__name__):
                self.assertEqual(tuple(inspect.signature(function).parameters), names)

    def test_importing_package_does_not_import_researchops_or_network_stacks(self) -> None:
        probe = """
import sys
import researchops_external_closure as package
assert callable(package.compute_document_sha256)
banned = sorted(
    name for name in sys.modules
    if name == 'researchops'
    or name.startswith('researchops.')
    or name == 'researchops_completion_telemetry'
    or name.startswith('researchops_completion_telemetry.')
    or name.split('.', 1)[0] in {'anthropic', 'httpx', 'openai', 'requests'}
)
print(repr(banned))
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_primitive_modules_have_no_runtime_or_write_capabilities(self) -> None:
        allowed_imports = {
            "__future__",
            "base64",
            "binascii",
            "cryptography.exceptions",
            "cryptography.hazmat.primitives.asymmetric.ed25519",
            "dataclasses",
            "decimal",
            "datetime",
            "errors",
            "functools",
            "hashlib",
            "json",
            "jsonschema",
            "math",
            "primitives",
            "re",
            "typing",
            "types",
        }
        forbidden_calls = {
            "__import__",
            "compile",
            "delattr",
            "eval",
            "exec",
            "getattr",
            "input",
            "open",
            "print",
            "setattr",
        }
        forbidden_method_calls = {
            "connect",
            "open",
            "read",
            "read_bytes",
            "read_text",
            "recv",
            "request",
            "send",
            "sendall",
            "write",
            "write_bytes",
            "write_text",
        }
        primitive_paths = (
            PACKAGE / "__init__.py",
            PACKAGE / "errors.py",
            PACKAGE / "primitives.py",
            PACKAGE / "types.py",
        )
        for path in primitive_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            called: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"now", "utcnow", "today"}
                ):
                    self.fail(f"clock read in {path.name}: {node.func.attr}")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in forbidden_method_calls
                ):
                    self.fail(f"I/O-capable call in {path.name}: {node.func.attr}")
            self.assertEqual(imported - allowed_imports, set(), path.name)
            self.assertFalse(called & forbidden_calls, path.name)

if __name__ == "__main__":
    unittest.main()
