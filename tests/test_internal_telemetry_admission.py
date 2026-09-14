"""Pure authorization/timing/source boundaries; no real claim or credentials."""
import asyncio
import copy
import contextlib
import io
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_internal_telemetry import admission as a, contract as c, source
from researchops_internal_telemetry.run import run_internal_v1
from researchops_internal_telemetry.run import main, _load_key
from researchops_completion_timing.clock import _TimingClock, TimingCaptureError


class InternalAdmissionTests(unittest.TestCase):
    def documents(self):
        instant=c.now()
        freeze={"pricing":{"evidence_date_utc":instant.isoformat().replace("+00:00","Z")}}
        candidate=dict(schema_version="provider-completion-internal-authorization-candidate/1.0",scope=c.SCOPE,
            authorization_id="internal-synthetic-approval-test",freeze_commitment_sha256=c.commitment("freeze",freeze),
            execution_environment_id=c.ENVIRONMENT_ID,not_before_utc=(instant-timedelta(seconds=10)).isoformat().replace("+00:00","Z"),
            expires_at_utc=(instant+timedelta(hours=2)).isoformat().replace("+00:00","Z"),output_namespace="output/internal-telemetry-v1")
        approved=c.commitment("authorization-candidate",candidate)
        document=dict(schema_version="provider-completion-internal-authorization/1.0",candidate=candidate,
            approval_observation=dict(kind="explicit_user_approval",approved_digest=approved,
                observed_at_utc=(instant-timedelta(seconds=1)).isoformat().replace("+00:00","Z"),message_reference=None))
        return freeze,document,approved,instant

    def test_explicit_digest_and_observation_required(self):
        freeze,document,approved,instant=self.documents()
        a.validate_authorization(document,freeze,approved,at=instant)
        for mutation in (lambda x:x["approval_observation"].update(kind="automatic_approval"),
                         lambda x:x["candidate"].update(execution_environment_id="PCEENV-"+"F"*32),
                         lambda x:x["candidate"].update(scope="external_unseen")):
            changed=copy.deepcopy(document);mutation(changed)
            with self.assertRaises(c.InternalError):a.validate_authorization(changed,freeze,approved,at=instant)
        with self.assertRaisesRegex(c.InternalError,"approval_digest"):
            a.validate_authorization(document,freeze,"3"*64,at=instant)

    def test_expiry_and_utc_z_are_not_relaxed(self):
        freeze,document,approved,instant=self.documents()
        with self.assertRaisesRegex(c.InternalError,"authorization_expired"):
            a.validate_authorization(document,freeze,approved,at=instant+timedelta(hours=3))
        with self.assertRaisesRegex(c.InternalError,"utc_invalid"):c.utc("2026-09-13T00:00:00")

    def test_unconfirmed_entry_does_not_reach_preparation(self):
        with patch.object(a,"prepare",side_effect=AssertionError("must not reach store or Key")) as prepare:
            result=asyncio.run(run_internal_v1(freeze_bytes=b"{}",authorization_bytes=b"{}",approved_digest="1"*64))
        self.assertEqual(prepare.call_count,0)
        self.assertFalse(result["claim_consumed"])
        self.assertEqual(result["network_attempts"],0)

    def test_owner_cannot_be_constructed_from_receipt(self):
        with self.assertRaisesRegex(c.InternalError,"owner_required"):
            a._ClaimedInternal(None,freeze_bytes=b"{}",authorization_bytes=b"{}",receipt_bytes=b"{}",request_bytes=b"{}",
                approved_digest="1"*64,clock_domain="PCECLOCK-"+"A"*32)

    def test_key_reader_requires_owned_scope_before_environment_value_read(self):
        with patch("researchops_internal_telemetry.run.os.environ.get",side_effect=AssertionError("Key getter reached")) as get:
            with self.assertRaisesRegex(c.InternalError,"key_owner_required"):_load_key(None)
        self.assertEqual(get.call_count,0)

    def test_cli_errors_never_echo_arbitrary_argument_values(self):
        stdout,stderr=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            result=main(["--unknown","sk-FAKE-CLI-SECRET-0123456789"])
        self.assertEqual(result,4)
        self.assertNotIn("sk-FAKE",stdout.getvalue()+stderr.getvalue())

    def test_monotonic_clock_rejects_backwards_time(self):
        with patch("researchops_completion_timing.clock.time.monotonic_ns",side_effect=[100,99]) as clock:
            timer=_TimingClock(request_timeout_ns=120_000_000_000,phase_timeout_ns=5_400_000_000_000)
            with self.assertRaisesRegex(TimingCaptureError,"clock_order_invalid"):timer.task_released()
        self.assertEqual(clock.call_count,2)

    def test_source_domain_and_current_drift_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/"src").mkdir();(root/"evals").mkdir()
            for name in ("pyproject.toml","requirements.lock","requirements.linux.lock","probe_out_v3.json"):
                (root/name).write_bytes(b"synthetic")
            target=root/"src/demo.py";target.write_bytes(b"VALUE = 1\n")
            manifest=source.build_manifest(root)
            (root/c.DIRECTORY).mkdir()
            (root/source.MANIFEST).write_bytes(c.raw(manifest))
            self.assertEqual(source.verify_source(root),manifest)
            target.write_bytes(b"VALUE = 2\n")
            with self.assertRaisesRegex(c.InternalError,"source_drift"):source.verify_source(root)
            self.assertNotEqual(source.build_manifest(root)["commitment_sha256"],manifest["commitment_sha256"])

    def test_internal_mapping_scope_cannot_become_external_or_first_live(self):
        from researchops_completion_telemetry import surface_mapping as mapping
        offline=mapping.load_and_select_surface_mapping(c.ROOT,"deepseek","responses","openai_compatible_responses",purpose="offline_validation")
        # Low-level type fixture only: no claimed owner or Provider session.
        selection=mapping.VerifiedSurfaceSelection._create(mapping._SELECTION_TOKEN,purpose=c.RUNTIME_SCOPE,
            telemetry_schema_sha256=offline.telemetry_schema_sha256,mapping=offline.mapping_snapshot(),
            entry=dict(adapter_version=offline.adapter_version,mapping_version=offline.mapping_version,
                output_counter_comparability=offline.output_counter_comparability,output_counter_path=offline.output_counter_path,
                runtime_binding_allowed=True))
        with self.assertRaises(mapping.SurfaceMappingError):mapping.create_runtime_completion_binding(selection)
        with self.assertRaises(mapping.SurfaceMappingError):mapping._create_first_live_validation_binding(selection)
        binding=mapping.VerifiedRuntimeCompletionBinding._create(mapping._RUNTIME_BINDING_TOKEN,selection)
        binding.assert_runtime_authority(expected_scope=c.RUNTIME_SCOPE)
        for scope in ("campaign_runtime","first_live_validation"):
            with self.assertRaises(mapping.SurfaceMappingError):binding.assert_runtime_authority(expected_scope=scope)


if __name__=="__main__":unittest.main()
