"""Windows-native replay tests, confined to test-owned temporary known folders."""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import local_claim as claim
from researchops_external_closure.primitives import canonical_json_bytes as raw


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "designated Windows execution environment only")
class LocalClaimTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.locator = patch.object(claim, "_windows_local_app_data", return_value=self.base)
        self.locator.start(); self.addCleanup(self.locator.stop)
        self.store = self.base / "ResearchOpsAgent/completion-claims-v1"

    def request(self, environment_id):
        return dict(schema_version="provider-completion-local-claim-request/1.0", execution_environment_id=environment_id,
                    authorization_id_sha256="1" * 64, authorization_grant_sha256="2" * 64,
                    consumption_entry_sha256="3" * 64, timing_plan_commitment_sha256="4" * 64,
                    execution_commit="5" * 40, clock_domain_id="PCECLOCK-" + "A" * 32)

    def provision(self):
        return claim.provision_local_claim_store()["execution_environment_id"]

    def test_status_does_not_provision_and_no_public_path_override_exists(self):
        self.assertEqual(claim.local_claim_store_status()["status"], "not_provisioned")
        self.assertFalse(self.store.exists())
        self.assertEqual(list(inspect.signature(claim.provision_local_claim_store).parameters), [])
        self.assertEqual(list(inspect.signature(claim.local_claim_store_status).parameters), [])
        self.assertEqual(list(inspect.signature(claim.reserve_local_claim).parameters), ["request_bytes"])

    def test_provision_reserve_and_replay_share_one_fixed_store(self):
        environment_id = self.provision()
        request = self.request(environment_id)
        result = claim.reserve_local_claim(raw(request))
        self.assertEqual(result["status"], "reserved")
        self.assertFalse(result["provider_actions_authorized"])
        self.assertFalse(result["runtime_authority_granted"])
        path = self.store / "claims" / ("1" * 64 + ".json")
        original = path.read_bytes()
        self.assertEqual(result["receipt_sha256"], claim.digest(original))
        self.assertNotIn(str(self.base), original.decode())
        request["authorization_grant_sha256"] = "6" * 64
        request["clock_domain_id"] = "PCECLOCK-" + "B" * 32
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(raw(request))
        self.assertEqual(original, path.read_bytes())

    def test_read_reserved_snapshot_is_readonly_and_has_no_path_override(self):
        request = raw(self.request(self.provision()))
        reserved = claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        before = marker.read_bytes()
        with patch.object(claim, "_write_once", side_effect=AssertionError("readonly")), \
             patch.object(claim, "provision_local_claim_store", side_effect=AssertionError("no provisioning")):
            result = claim.read_reserved_local_claim(request, expected_receipt_sha256=reserved["receipt_sha256"])
        self.assertEqual(result, before)
        self.assertEqual(list(inspect.signature(claim.read_reserved_local_claim).parameters),
                         ["request_bytes", "expected_receipt_sha256"])
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(request)

    def test_snapshot_missing_store_or_marker_is_never_created(self):
        request = raw(self.request("PCEENV-" + "A" * 32))
        with self.assertRaises(claim.LocalClaimError):
            claim.read_reserved_local_claim(request, expected_receipt_sha256="2" * 64)
        self.assertFalse(self.store.exists())
        request = raw(self.request(self.provision()))
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_receipt_unverifiable"):
            claim.read_reserved_local_claim(request, expected_receipt_sha256="2" * 64)
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_snapshot_rejects_wrong_hash_environment_and_rehashed_receipt(self):
        document = self.request(self.provision()); request = raw(document)
        reserved = claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        original = marker.read_bytes()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_receipt_hash_mismatch"):
            claim.read_reserved_local_claim(request, expected_receipt_sha256="f" * 64)
        wrong = dict(document, execution_environment_id="PCEENV-" + "F" * 32)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_environment_mismatch"):
            claim.read_reserved_local_claim(raw(wrong), expected_receipt_sha256=reserved["receipt_sha256"])
        for field, value in (("authorization_grant_sha256", "f" * 64), ("provider_actions_authorized", 0),
                             ("clock_domain_id", "PCECLOCK-" + "F" * 32)):
            mutated = json.loads(original); mutated[field] = value
            marker.write_bytes(raw(mutated))  # This test's temporary fixture only.
            with self.subTest(field=field), self.assertRaisesRegex(claim.LocalClaimError, "local_claim_receipt_binding_mismatch"):
                claim.read_reserved_local_claim(request, expected_receipt_sha256=claim.digest(raw(mutated)))

    def test_snapshot_rejects_empty_partial_hardlinked_and_oversized_markers(self):
        request = raw(self.request(self.provision()))
        claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        original = marker.read_bytes()
        for payload in (b"", b"{", b"a" * 4097):
            marker.write_bytes(payload)
            with self.subTest(size=len(payload)), self.assertRaises(claim.LocalClaimError):
                claim.read_reserved_local_claim(request, expected_receipt_sha256=claim.digest(payload))
            self.assertEqual(marker.read_bytes(), payload)
        marker.write_bytes(original)
        os.link(marker, self.base / "claim-alias.json")
        with self.assertRaises(claim.LocalClaimError):
            claim.read_reserved_local_claim(request, expected_receipt_sha256=claim.digest(original))

    def test_snapshot_rechecks_marker_after_validation(self):
        request = raw(self.request(self.provision()))
        reserved = claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        original_reader = claim.read_regular_file_no_follow
        reads = []
        def read(path, **kwargs):
            payload = original_reader(path, **kwargs)
            if path == marker:
                reads.append(True)
                if len(reads) == 1:
                    marker.write_bytes(b"{}"); return payload
            return payload
        with patch.object(claim, "read_regular_file_no_follow", side_effect=read):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_store_changed"):
                claim.read_reserved_local_claim(request, expected_receipt_sha256=reserved["receipt_sha256"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(marker.read_bytes(), b"{}")

    def test_snapshot_rejects_authority_changed_during_receipt_read(self):
        request = raw(self.request(self.provision()))
        reserved = claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        authority = self.store / "authority.json"
        original_reader = claim.read_regular_file_no_follow
        def read(path, **kwargs):
            payload = original_reader(path, **kwargs)
            if path == marker:
                value = json.loads(authority.read_bytes())
                value["execution_environment_id"] = "PCEENV-" + "F" * 32
                authority.write_bytes(raw(value))
            return payload
        with patch.object(claim, "read_regular_file_no_follow", side_effect=read):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_store_changed"):
                claim.read_reserved_local_claim(request, expected_receipt_sha256=reserved["receipt_sha256"])

    def test_successfully_readable_uncertain_fsync_claim_still_cannot_be_reserved_again(self):
        request = raw(self.request(self.provision()))
        with patch.object(claim.os, "fsync", side_effect=OSError("synthetic failure")):
            with self.assertRaises(claim.LocalClaimError):
                claim.reserve_local_claim(request)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        stored = marker.read_bytes()
        self.assertEqual(claim.read_reserved_local_claim(request, expected_receipt_sha256=claim.digest(stored)), stored)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(request)

    def test_worktree_cwd_and_environment_do_not_change_the_store(self):
        environment_id = self.provision()
        original_cwd = Path.cwd()
        other = self.base / "other-worktree"; other.mkdir()
        try:
            os.chdir(other)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(other)}):
                self.assertEqual(claim.local_claim_store_status()["execution_environment_id"], environment_id)
                claim.reserve_local_claim(raw(self.request(environment_id)))
        finally:
            os.chdir(original_cwd)
        self.assertTrue((self.store / "claims" / ("1" * 64 + ".json")).is_file())
        self.assertFalse((other / "ResearchOpsAgent").exists())

    def test_wrong_environment_rejects_before_claim_creation(self):
        self.provision()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_environment_mismatch"):
            claim.reserve_local_claim(raw(self.request("PCEENV-" + "F" * 32)))
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_missing_or_corrupt_authority_is_not_repaired(self):
        environment_id = self.provision()
        authority = self.store / "authority.json"
        authority.write_bytes(b"broken synthetic state")
        with self.assertRaises(claim.LocalClaimError):
            claim.reserve_local_claim(raw(self.request(environment_id)))
        with self.assertRaises(claim.LocalClaimError):
            claim.provision_local_claim_store()
        self.assertEqual(authority.read_bytes(), b"broken synthetic state")
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_missing_authority_in_existing_store_is_not_recreated(self):
        environment_id = self.provision()
        authority = self.store / "authority.json"
        authority.unlink()  # Test-owned temporary state only.
        with self.assertRaises(claim.LocalClaimError):
            claim.reserve_local_claim(raw(self.request(environment_id)))
        with self.assertRaises(claim.LocalClaimError):
            claim.provision_local_claim_store()
        self.assertFalse(authority.exists())
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_sensitive_or_extra_request_fields_do_not_create_markers(self):
        environment_id = self.provision()
        for value in ("Authorization: Bearer FAKECANARY", "ordinary extra field"):
            request = self.request(environment_id); request["extra"] = value
            with self.assertRaises(claim.LocalClaimError):
                claim.reserve_local_claim(raw(request))
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_empty_existing_marker_blocks_without_being_read(self):
        environment_id = self.provision()
        marker = self.store / "claims" / ("1" * 64 + ".json")
        marker.write_bytes(b"")
        original = claim.read_regular_file_no_follow
        def read(path, **kwargs):
            self.assertNotEqual(path, marker)
            return original(path, **kwargs)
        with patch.object(claim, "read_regular_file_no_follow", side_effect=read):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
                claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertEqual(marker.read_bytes(), b"")

    def test_fsync_failure_leaves_a_sticky_marker_and_never_authorizes_retry(self):
        environment_id = self.provision()
        with patch.object(claim.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(claim.LocalClaimError) as caught:
                claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertTrue(caught.exception.claim_may_exist)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        self.assertTrue(marker.exists())
        before = marker.read_bytes()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertEqual(before, marker.read_bytes())

    def test_short_write_failure_preserves_partial_marker(self):
        environment_id = self.provision()
        original = claim.os.write
        calls = []
        def write(descriptor, payload):
            calls.append(True)
            if len(calls) == 1:
                return original(descriptor, payload[:3])
            raise OSError("synthetic write failure")
        with patch.object(claim.os, "write", side_effect=write):
            with self.assertRaises(claim.LocalClaimError) as caught:
                claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertTrue(caught.exception.claim_may_exist)
        marker = self.store / "claims" / ("1" * 64 + ".json")
        self.assertEqual(len(marker.read_bytes()), 3)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(raw(self.request(environment_id)))

    def test_copied_store_at_another_location_fails_binding(self):
        environment_id = self.provision()
        other = self.base / "different-known-folder"; other.mkdir()
        copied = other / "ResearchOpsAgent/completion-claims-v1"
        shutil.copytree(self.store, copied)
        with patch.object(claim, "_windows_local_app_data", return_value=other):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_authority_invalid"):
                claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertEqual(list((copied / "claims").iterdir()), [])

    def test_directory_lock_blocks_retargeting_until_closed(self):
        self.provision()
        target = self.base / "moved-store"
        with claim._locked_chain(self.store / "claims"):
            with self.assertRaises(OSError):
                self.store.rename(target)
        self.assertTrue(self.store.is_dir())
        self.assertFalse(target.exists())

    def test_hardlinked_authority_is_rejected(self):
        environment_id = self.provision()
        os.link(self.store / "authority.json", self.base / "authority-alias.json")
        with self.assertRaises(claim.LocalClaimError):
            claim.reserve_local_claim(raw(self.request(environment_id)))
        self.assertEqual(list((self.store / "claims").iterdir()), [])

    def test_two_processes_from_different_worktrees_get_one_reservation(self):
        environment_id = self.provision()
        request = json.dumps(self.request(environment_id))
        code = "\n".join((
            "import json, sys, time", "from pathlib import Path",
            "from researchops_completion_timing import local_claim as c",
            "c._windows_local_app_data = lambda: Path(sys.argv[1])",
            "(Path(sys.argv[1]) / ('ready-' + sys.argv[3])).touch(exist_ok=False)",
            "deadline = time.monotonic() + 20",
            "while not (Path(sys.argv[1]) / 'release').exists():",
            "    if time.monotonic() >= deadline: raise RuntimeError('fixture barrier timeout')",
            "    time.sleep(0.01)",
            "try:", "    c.reserve_local_claim(sys.argv[2].encode())", "    print('reserved')",
            "except c.LocalClaimError as error:", "    print(error.code)",
        ))
        # Whitelist non-credential process settings; do not copy/read API keys.
        environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
        environment["PYTHONPATH"] = str(ROOT / "src")
        processes = []
        try:
            for name in ("worktree-a", "worktree-b"):
                directory = self.base / name; directory.mkdir()
                processes.append(subprocess.Popen([sys.executable, "-c", code, str(self.base), request, name],
                    cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, creationflags=subprocess.CREATE_NO_WINDOW))
            deadline = time.monotonic() + 20
            while not all((self.base / ("ready-" + name)).exists() for name in ("worktree-a", "worktree-b")):
                if time.monotonic() >= deadline or any(process.poll() is not None for process in processes):
                    self.fail("claim workers did not reach the shared barrier")
                time.sleep(0.01)
            (self.base / "release").touch(exist_ok=False)
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(stdout.strip())
            self.assertCountEqual(outputs, ["reserved", "local_claim_already_exists"])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.communicate()


class LocalClaimPortableContractTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Known Folder API")
    def test_native_known_folder_ignores_localappdata_environment_override(self):
        # Metadata lookup only; do not provision or inspect the real store.
        first = claim._windows_local_app_data()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                second = claim._windows_local_app_data()
        self.assertTrue(first == second, "Known Folder changed with environment variable")
        self.assertTrue(first.is_absolute())

    def test_contract_scope_and_no_path_environment_lookup(self):
        profile = claim._profile()
        self.assertFalse(profile["cross_host_execution_authorized"])
        self.assertFalse(profile["provider_calls_authorized"])
        source = inspect.getsource(claim._windows_local_app_data)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


if __name__ == "__main__":
    unittest.main()
