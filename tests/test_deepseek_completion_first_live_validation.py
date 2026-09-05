from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
from agents import OpenAIResponsesModel

from researchops.audit import AuditError, AuditLedger, _event_hash, sha256_json
from researchops.cli import build_parser, main as cli_main
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
import researchops.deepseek_completion_first_live_validation as first_live
import researchops.model_providers as provider_module
from researchops.model_providers import DeepSeekProvider
from researchops.model_providers import ProviderConfigurationError, _validate_completion_session
from researchops.phase6_runner import Phase6RunError, _validate_runtime_denominator_tracker
from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    validate_runtime_denominator_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMITMENT = "a" * 64
EXECUTION_COMMIT = "b" * 40
SAFE_KEY = "SAFE-DEEPSEEK-FIRST-LIVE-KEY"
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


def _response_body(status: str, cap: int, *, output_text: str = "ok") -> dict:
    completed = status == "completed"
    output_tokens = 1 if completed else cap
    return {
        "id": f"resp_mock_{status}_{cap}",
        "object": "response",
        "created_at": 1,
        "status": status,
        "error": None,
        "incomplete_details": (
            None if completed else {"reason": "max_output_tokens"}
        ),
        "instructions": None,
        "max_output_tokens": cap,
        "model": "deepseek-v4-flash",
        "output": (
            [
                {
                    "id": "msg_mock_completed",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            ]
            if completed
            else []
        ),
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "temperature": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {
                "reasoning_tokens": 0 if completed else output_tokens
            },
            "total_tokens": 5 + output_tokens,
        },
        "metadata": {},
    }


class _MockResponsesTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        if not self.responses:
            raise AssertionError("unexpected third request")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return httpx.Response(
            200,
            json=value,
            headers={"x-request-id": f"req_mock_{len(self.requests)}"},
            request=request,
        )

    def client_factory(self, **kwargs):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            timeout=120.0,
            follow_redirects=False,
            trust_env=False,
            event_hooks=kwargs.get("event_hooks"),
        )


class _HangingResponsesTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(await request.aread())
        await asyncio.Future()
        raise AssertionError("unreachable")

    def client_factory(self, **kwargs):
        return httpx.AsyncClient(
            transport=self,
            follow_redirects=False,
            trust_env=False,
            event_hooks=kwargs.get("event_hooks"),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _run_arguments(now: datetime) -> dict:
    return {
        "project_root": ROOT,
        "authorization_id": "deepseek-first-live-offline-test-001",
        "authorization_expires_at_utc": (now + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "expected_contract_commitment_sha256": (
            first_live.CONTRACT_COMMITMENT_SHA256
        ),
        "expected_source_integrity_commitment_sha256": SOURCE_COMMITMENT,
        "expected_execution_commit": EXECUTION_COMMIT,
        "pricing_snapshot_date": now.date().isoformat(),
        "pricing_source_url": PRICING_URL,
        "input_price_per_million_cny": "3.000000",
        "output_price_per_million_cny": "9.000000",
        "confirm_online": True,
        "accept_locked_caps": True,
        "attest_pricing_current": True,
        "_key_loader": lambda: SAFE_KEY,
        "_clock": lambda: now,
        "_git_state_loader": lambda root: (
            EXECUTION_COMMIT,
            True,
            EXECUTION_COMMIT,
        ),
    }


def _bound_run_arguments(arguments: dict) -> dict:
    bound = dict(arguments)
    if "expected_authorization_binding_sha256" not in bound:
        authorization_id = bound["authorization_id"]
        authorization_hash = hashlib.sha256(
            authorization_id.encode("utf-8")
        ).hexdigest()
        expiry = first_live._timestamp(
            first_live._parse_utc(bound["authorization_expires_at_utc"])
        )
        bound["expected_authorization_binding_sha256"] = (
            first_live._authorization_binding(
                authorization_id_sha256=authorization_hash,
                expires_at_utc=expiry,
                execution_commit=bound["expected_execution_commit"],
                source_integrity_commitment=bound[
                    "expected_source_integrity_commitment_sha256"
                ],
                pricing_snapshot_date=bound["pricing_snapshot_date"],
                pricing_source_url=bound["pricing_source_url"],
                input_price=first_live.Decimal(
                    bound["input_price_per_million_cny"]
                ),
                output_price=first_live.Decimal(
                    bound["output_price_per_million_cny"]
                ),
            )
        )
    return bound


def _run_impl(arguments: dict) -> dict:
    return asyncio.run(
        first_live._run_deepseek_first_live_validation_impl(
            **_bound_run_arguments(arguments)
        )
    )


def _source_integrity() -> dict:
    return {
        "status": "valid",
        "plan_id": first_live.SOURCE_INTEGRITY_PLAN_ID,
        "plan_commitment_sha256": SOURCE_COMMITMENT,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
    }


def _overwrite_canonical(path: Path, value: object) -> bytes:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    path.write_bytes(body)
    return body


def _refresh_manifest_and_terminal(
    artifact: Path,
    changed_filenames: tuple[str, ...],
) -> None:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for filename in changed_filenames:
        payload = (artifact / filename).read_bytes()
        manifest["files"][filename] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    manifest_bytes = _overwrite_canonical(manifest_path, manifest)
    terminal_path = artifact / "terminal.json"
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    _overwrite_canonical(terminal_path, terminal)


def _rewrite_audit_event_payload(
    database: Path,
    *,
    target_event_type: str,
    mutate,
) -> None:
    run_id = "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        trigger_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'audit_events_no_update'"
        ).fetchone()
        if trigger_row is None or not isinstance(trigger_row[0], str):
            raise AssertionError("append-only update trigger missing")
        trigger_sql = trigger_row[0]
        rows = connection.execute(
            "SELECT sequence, event_type, occurred_at_utc, actor_kind, "
            "safe_payload_json, prev_hash FROM audit_events "
            "WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        connection.execute("DROP TRIGGER audit_events_no_update")
        previous_hash = "0" * 64
        changed = False
        for sequence, event_type, occurred_at, actor_kind, safe_json, _ in rows:
            payload = json.loads(safe_json)
            if event_type == target_event_type and not changed:
                mutate(payload)
                changed = True
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            event_hash = _event_hash(
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                occurred_at_utc=occurred_at,
                actor_kind=actor_kind,
                safe_payload_json=canonical,
                prev_hash=previous_hash,
            )
            connection.execute(
                "UPDATE audit_events SET safe_payload_json = ?, prev_hash = ?, "
                "event_hash = ? WHERE run_id = ? AND sequence = ?",
                (canonical, previous_hash, event_hash, run_id, sequence),
            )
            previous_hash = event_hash
        if not changed:
            raise AssertionError(f"event type not found: {target_event_type}")
        connection.execute(trigger_sql)
        connection.commit()


def _run_mock_success(directory: str, authorization_id: str) -> tuple[Path, str]:
    now = _now()
    transport = _MockResponsesTransport(
        [_response_body("completed", 256), _response_body("incomplete", 16)]
    )
    with patch(
        "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
        return_value=_source_integrity(),
    ), patch(
        "researchops.model_providers._load_responses_transport",
        return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
    ):
        arguments = _run_arguments(now)
        arguments["authorization_id"] = authorization_id
        arguments["_artifact_root"] = Path(directory)
        terminal = _run_impl(arguments)
    if terminal["status"] != "success":
        raise AssertionError(terminal)
    authorization_hash = hashlib.sha256(authorization_id.encode()).hexdigest()
    return Path(directory) / authorization_hash, authorization_hash


def _convert_success_to_manifestless_prefix(
    artifact: Path,
    prefix_length: int,
    *,
    interrupted_success: bool = True,
) -> None:
    order = (
        "audit_index.json",
        "runtime_denominator.json",
        "completion_telemetry.json",
    )
    if prefix_length not in range(len(order) + 1):
        raise AssertionError("invalid manifestless prefix length")
    (artifact / "manifest.json").unlink()
    for filename in order[prefix_length:]:
        (artifact / filename).unlink()
    terminal_path = artifact / "terminal.json"
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    if interrupted_success:
        terminal["status"] = "failed"
        terminal["error_code"] = (
            "deepseek_first_live_evidence_persistence_failed"
        )
    terminal["manifest_complete"] = False
    terminal["manifest_sha256"] = None
    partial_names = (
        "consumption.json",
        "audit.sqlite3",
        *order[:prefix_length],
    )
    terminal["partial_artifacts"] = {
        filename: {
            "bytes": (artifact / filename).stat().st_size,
            "sha256": hashlib.sha256((artifact / filename).read_bytes()).hexdigest(),
        }
        for filename in partial_names
    }
    _overwrite_canonical(terminal_path, terminal)


class DeepSeekCompletionFirstLiveValidationTests(unittest.TestCase):
    def _verify(self, *args, **kwargs):
        artifact_root = Path(kwargs["_artifact_root"])
        authorization_hash = args[1]
        consumption = json.loads(
            (
                artifact_root / authorization_hash / "consumption.json"
            ).read_text(encoding="utf-8")
        )
        kwargs.setdefault(
            "expected_authorization_binding_sha256",
            consumption["authorization_binding_sha256"],
        )
        runtime_binding = first_live._validation_runtime_binding(ROOT)
        with patch(
            "researchops.deepseek_completion_first_live_validation."
            "_validate_persisted_execution_identity",
            return_value=runtime_binding,
        ):
            return first_live.verify_deepseek_first_live_artifacts(*args, **kwargs)

    def test_raw_response_cleanup_timeout_is_bounded_and_fail_closed(self) -> None:
        class HangingRawResponse:
            async def aclose(self) -> None:
                await asyncio.Future()

        async def exercise() -> None:
            await provider_module._close_response(HangingRawResponse())

        with patch.object(
            provider_module,
            "_RAW_RESPONSE_CLEANUP_TIMEOUT_SECONDS",
            0.001,
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(
            caught.exception.code,
            "provider_completion_raw_cleanup_failed",
        )

    def test_deepseek_post_request_resources_each_have_bounded_cleanup(self) -> None:
        class HangingClient:
            instances: list["HangingClient"] = []

            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.__class__.instances.append(self)

            async def close(self) -> None:
                await asyncio.Future()

        class TrackingHttpClient:
            instances: list["TrackingHttpClient"] = []

            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.closed = False
                self.__class__.instances.append(self)

            async def aclose(self) -> None:
                self.closed = True

        class OfflineResponsesModel:
            def __init__(self, *, model: str, openai_client: object) -> None:
                self.model = model
                self.openai_client = openai_client

        async def exercise() -> None:
            async with DeepSeekProvider().open_model(
                model_id="deepseek-v4-flash",
                api_key=SAFE_KEY,
            ):
                pass

        with patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(HangingClient, OfflineResponsesModel, TrackingHttpClient),
        ), patch.object(
            provider_module,
            "_DEEPSEEK_POST_REQUEST_CLEANUP_TIMEOUT_SECONDS",
            0.001,
        ), self.assertRaises(ProviderConfigurationError) as caught:
            asyncio.run(exercise())
        self.assertEqual(
            caught.exception.code,
            "provider_completion_post_request_cleanup_failed",
        )
        self.assertEqual(len(HangingClient.instances), 1)
        self.assertEqual(len(TrackingHttpClient.instances), 1)
        self.assertTrue(TrackingHttpClient.instances[0].closed)

    def test_confirmed_contract_commitment_and_bound_bytes_validate_offline(self) -> None:
        value = first_live.validate_deepseek_first_live_contract(ROOT)
        self.assertEqual(
            value["contract_commitment"]["sha256"],
            first_live.CONTRACT_COMMITMENT_SHA256,
        )
        status = first_live.deepseek_first_live_validation_status(ROOT)
        self.assertEqual(
            status["status"], "offline_implemented_requires_fresh_authorization"
        )
        self.assertTrue(status["cli_implemented"])
        self.assertTrue(status["validation_only_authority_implemented"])
        self.assertTrue(status["source_integrity_successor_present"])
        self.assertEqual(
            status["authorization_binding_schema_version"],
            "deepseek-first-live-authorization-binding/2.0",
        )
        self.assertTrue(status["external_authorization_binding_required"])
        self.assertEqual(
            status["source_integrity_commitment_sha256"],
            "8a5474db1e9ad59d501bf109d4a7ecbf616f40599763a20188581e336d379bd7",
        )
        self.assertFalse(status["online_execution_authorized"])
        self.assertEqual((status["network_calls"], status["model_calls"]), (0, 0))
        implementation = first_live.validate_deepseek_first_live_implementation(ROOT)
        self.assertEqual(
            implementation["implementation_commitment"]["sha256"],
            first_live.IMPLEMENTATION_COMMITMENT_SHA256,
        )
        self.assertEqual(
            implementation["predecessor_design"]["contract_commitment_sha256"],
            first_live.CONTRACT_COMMITMENT_SHA256,
        )

    def test_persisted_execution_identity_binds_git_plan_and_pricing_date(self) -> None:
        plan_path = ROOT / first_live.SOURCE_INTEGRITY_PLAN_RELATIVE_PATH
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        consumption = {
            "execution_commit": "a" * 40,
            "source_integrity_plan_id": first_live.SOURCE_INTEGRITY_PLAN_ID,
            "source_integrity_commitment_sha256": plan[
                "plan_commitment_sha256"
            ],
            "consumed_at_utc": "2026-09-04T00:00:00.000Z",
            "pricing_snapshot_date": "2026-09-04",
        }
        @contextlib.contextmanager
        def committed_tree(root, commit):
            del root, commit
            yield ROOT

        with patch(
            "researchops.deepseek_completion_first_live_validation.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ), patch(
            "researchops.deepseek_completion_first_live_validation._materialized_git_tree",
            new=committed_tree,
        ):
            first_live._validate_persisted_execution_identity(ROOT, consumption)
        forged = dict(consumption)
        forged["pricing_snapshot_date"] = "2000-01-01"
        with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
            first_live._validate_persisted_execution_identity(ROOT, forged)
        self.assertEqual(
            caught.exception.code,
            "deepseek_first_live_execution_identity_invalid",
        )

    def test_git_archive_materializer_rejects_unsafe_members_and_disables_fetch(
        self,
    ) -> None:
        def archive_payload(names: tuple[str, ...], *, symlink: bool = False) -> bytes:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name in names:
                    member = tarfile.TarInfo(name)
                    if symlink:
                        member.type = tarfile.SYMTYPE
                        member.linkname = "target"
                        member.size = 0
                        archive.addfile(member)
                    else:
                        payload = b"x"
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
            return stream.getvalue()

        cases = (
            archive_payload(("../escape",)),
            archive_payload(("/absolute",)),
            archive_payload(("C:/drive",)),
            archive_payload(("..\\escape",)),
            archive_payload(("link",), symlink=True),
            archive_payload(("duplicate", "duplicate")),
        )
        for payload in cases:
            def write_archive(*args, **kwargs):
                del args
                kwargs["stdout"].write(payload)
                return SimpleNamespace(returncode=0)

            with self.subTest(payload_sha256=hashlib.sha256(payload).hexdigest()), patch(
                "researchops.deepseek_completion_first_live_validation.subprocess.run",
                side_effect=write_archive,
            ) as run:
                with self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ) as caught:
                    with first_live._materialized_git_tree(ROOT, "a" * 40):
                        self.fail("unsafe archive must not materialize")
                self.assertEqual(
                    caught.exception.code,
                    "deepseek_first_live_execution_archive_invalid",
                )
                command = run.call_args.args[0]
                environment = run.call_args.kwargs["env"]
                self.assertEqual(command[:3], ["git", "--no-replace-objects", "archive"])
                self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_git_archive_materializer_reads_the_checked_out_commit_offline(
        self,
    ) -> None:
        head = subprocess.run(
            ["git", "--no-replace-objects", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=first_live._git_offline_environment(),
        ).stdout.strip()
        with first_live._materialized_git_tree(ROOT, head) as committed_root:
            self.assertFalse((committed_root / ".git").exists())
            plan = committed_root / first_live.SOURCE_INTEGRITY_PLAN_RELATIVE_PATH
            self.assertEqual(
                json.loads(plan.read_text(encoding="utf-8"))["plan_id"],
                first_live.SOURCE_INTEGRITY_PLAN_ID,
            )

    def test_execution_identity_rejects_self_consistent_stale_v5_components(
        self,
    ) -> None:
        from researchops.phase6_depth60 import build_depth60_successor_plan_v5
        from tests.test_phase6_depth60_source_integrity_v5 import (
            _copy_v5_root,
            _write_plan,
        )

        with tempfile.TemporaryDirectory() as directory:
            committed_root = _copy_v5_root(directory)
            plan = build_depth60_successor_plan_v5(
                committed_root,
                locked_at_utc="2026-09-04T00:00:00.000Z",
            )
            _write_plan(committed_root, plan)
            source = (
                committed_root
                / "src/researchops/deepseek_completion_first_live_validation.py"
            )
            source.write_bytes(source.read_bytes() + b"\n")

            @contextlib.contextmanager
            def stale_tree(root, commit):
                del root, commit
                yield committed_root

            consumption = {
                "execution_commit": "a" * 40,
                "source_integrity_plan_id": first_live.SOURCE_INTEGRITY_PLAN_ID,
                "source_integrity_commitment_sha256": plan[
                    "plan_commitment_sha256"
                ],
                "consumed_at_utc": "2026-09-04T00:00:00.000Z",
                "pricing_snapshot_date": "2026-09-04",
            }
            with patch(
                "researchops.deepseek_completion_first_live_validation.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ), patch(
                "researchops.deepseek_completion_first_live_validation._materialized_git_tree",
                new=stale_tree,
            ), self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                first_live._validate_persisted_execution_identity(ROOT, consumption)
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_execution_identity_invalid",
            )

    def test_cli_surface_has_no_key_model_prompt_tool_or_output_override(self) -> None:
        parameters = inspect.signature(
            first_live.run_deepseek_first_live_validation
        ).parameters
        self.assertFalse(any(name.startswith("_") for name in parameters))
        self.assertNotIn("api_key", parameters)
        self.assertIs(
            parameters["expected_authorization_binding_sha256"].default,
            inspect.Parameter.empty,
        )
        verify_parameters = inspect.signature(
            first_live.verify_deepseek_first_live_artifacts
        ).parameters
        self.assertIs(
            verify_parameters["expected_authorization_binding_sha256"].default,
            inspect.Parameter.empty,
        )
        parser = build_parser()
        parsed = parser.parse_args(
            [
                "deepseek-completion-first-live-run",
                "--authorization-id",
                "AUTH-001",
                "--authorization-expires-at-utc",
                "2099-01-01T00:00:00Z",
                "--expected-contract-commitment",
                first_live.CONTRACT_COMMITMENT_SHA256,
                "--expected-source-integrity-commitment",
                SOURCE_COMMITMENT,
                "--expected-execution-commit",
                EXECUTION_COMMIT,
                "--expected-authorization-binding-sha256",
                "c" * 64,
                "--pricing-snapshot-date",
                "2099-01-01",
                "--pricing-source-url",
                PRICING_URL,
                "--input-price-per-million-cny",
                "3",
                "--output-price-per-million-cny",
                "9",
            ]
        )
        for forbidden in ("api_key", "model", "prompt", "tools", "output_dir"):
            self.assertFalse(hasattr(parsed, forbidden))
        self.assertEqual(parsed.expected_contract_commitment, first_live.CONTRACT_COMMITMENT_SHA256)
        self.assertEqual(parsed.expected_authorization_binding_sha256, "c" * 64)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            sys,
            "argv",
            ["researchops", "deepseek-completion-first-live-validate"],
        ):
            exit_code = cli_main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["network_calls"], 0)

    def test_authorization_binding_calculator_is_offline_and_matches_run_gate(
        self,
    ) -> None:
        now = _now()
        arguments = _run_arguments(now)
        expected = _bound_run_arguments(arguments)[
            "expected_authorization_binding_sha256"
        ]
        result = first_live.calculate_deepseek_first_live_authorization_binding(
            project_root=ROOT,
            authorization_id=arguments["authorization_id"],
            authorization_expires_at_utc=arguments[
                "authorization_expires_at_utc"
            ],
            expected_contract_commitment_sha256=arguments[
                "expected_contract_commitment_sha256"
            ],
            expected_source_integrity_commitment_sha256=arguments[
                "expected_source_integrity_commitment_sha256"
            ],
            expected_execution_commit=arguments["expected_execution_commit"],
            pricing_snapshot_date=arguments["pricing_snapshot_date"],
            pricing_source_url=arguments["pricing_source_url"],
            input_price_per_million_cny=arguments[
                "input_price_per_million_cny"
            ],
            output_price_per_million_cny=arguments[
                "output_price_per_million_cny"
            ],
        )
        self.assertEqual(result["authorization_binding_sha256"], expected)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["provider_key_loaded"])
        self.assertFalse(result["authorizes_online_execution"])
        output = io.StringIO()
        argv = [
            "researchops",
            "deepseek-completion-first-live-bind-authorization",
            "--authorization-id",
            arguments["authorization_id"],
            "--authorization-expires-at-utc",
            arguments["authorization_expires_at_utc"],
            "--expected-contract-commitment",
            arguments["expected_contract_commitment_sha256"],
            "--expected-source-integrity-commitment",
            arguments["expected_source_integrity_commitment_sha256"],
            "--expected-execution-commit",
            arguments["expected_execution_commit"],
            "--pricing-snapshot-date",
            arguments["pricing_snapshot_date"],
            "--pricing-source-url",
            arguments["pricing_source_url"],
            "--input-price-per-million-cny",
            arguments["input_price_per_million_cny"],
            "--output-price-per-million-cny",
            arguments["output_price_per_million_cny"],
        ]
        with contextlib.redirect_stdout(output), patch.object(sys, "argv", argv):
            self.assertEqual(cli_main(), 0)
        cli_result = json.loads(output.getvalue())
        self.assertEqual(cli_result["authorization_binding_sha256"], expected)
        self.assertEqual((cli_result["network_calls"], cli_result["model_calls"]), (0, 0))

    def test_authorization_binding_calculator_rejects_reserved_cost_breach(
        self,
    ) -> None:
        now = _now()
        arguments = _run_arguments(now)
        with self.assertRaises(
            first_live.DeepSeekFirstLiveValidationError
        ) as caught:
            first_live.calculate_deepseek_first_live_authorization_binding(
                project_root=ROOT,
                authorization_id=arguments["authorization_id"],
                authorization_expires_at_utc=arguments[
                    "authorization_expires_at_utc"
                ],
                expected_contract_commitment_sha256=arguments[
                    "expected_contract_commitment_sha256"
                ],
                expected_source_integrity_commitment_sha256=arguments[
                    "expected_source_integrity_commitment_sha256"
                ],
                expected_execution_commit=arguments["expected_execution_commit"],
                pricing_snapshot_date=arguments["pricing_snapshot_date"],
                pricing_source_url=arguments["pricing_source_url"],
                input_price_per_million_cny="1000",
                output_price_per_million_cny="1000",
            )
        self.assertEqual(
            caught.exception.code,
            "deepseek_first_live_cost_reservation_exceeded",
        )
        self.assertTrue(caught.exception.not_run)

    def test_verifier_rejects_high_price_self_consistent_zero_call_receipt(
        self,
    ) -> None:
        now = _now()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-high-price-forgery"
            arguments["_artifact_root"] = Path(directory)
            arguments["_key_loader"] = lambda: None
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["model_requests"], 0)
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            consumption_path = artifact / "consumption.json"
            consumption = json.loads(
                consumption_path.read_text(encoding="utf-8")
            )
            consumption["input_price_per_million_cny"] = "1000.000000"
            consumption["output_price_per_million_cny"] = "1000.000000"
            forged_binding = first_live._authorization_binding(
                authorization_id_sha256=authorization_hash,
                expires_at_utc=consumption["authorization_expires_at_utc"],
                execution_commit=consumption["execution_commit"],
                source_integrity_commitment=consumption[
                    "source_integrity_commitment_sha256"
                ],
                pricing_snapshot_date=consumption["pricing_snapshot_date"],
                pricing_source_url=consumption["pricing_source_url"],
                input_price=first_live.Decimal("1000"),
                output_price=first_live.Decimal("1000"),
            )
            consumption["authorization_binding_sha256"] = forged_binding
            consumption_bytes = _overwrite_canonical(
                consumption_path,
                consumption,
            )
            terminal_path = artifact / "terminal.json"
            forged_terminal = json.loads(
                terminal_path.read_text(encoding="utf-8")
            )
            forged_terminal["authorization_binding_sha256"] = forged_binding
            forged_terminal["consumption_receipt_sha256"] = hashlib.sha256(
                consumption_bytes
            ).hexdigest()
            forged_terminal["partial_artifacts"]["consumption.json"] = {
                "bytes": len(consumption_bytes),
                "sha256": hashlib.sha256(consumption_bytes).hexdigest(),
            }
            _overwrite_canonical(terminal_path, forged_terminal)
            with self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    expected_authorization_binding_sha256=forged_binding,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_cost_reservation_exceeded",
            )
            self.assertFalse(caught.exception.not_run)

    def test_preconsumption_gate_matrix_never_loads_key_or_writes(self) -> None:
        now = _now()
        base = _run_arguments(now)
        cases = {
            "confirmation": ({"confirm_online": False}, "deepseek_first_live_confirmation_required"),
            "caps": ({"accept_locked_caps": False}, "deepseek_first_live_locked_caps_not_accepted"),
            "pricing_attestation": ({"attest_pricing_current": False}, "deepseek_first_live_pricing_attestation_required"),
            "contract": ({"expected_contract_commitment_sha256": "0" * 64}, "deepseek_first_live_contract_not_authorized"),
            "source": ({"expected_source_integrity_commitment_sha256": "bad"}, "deepseek_first_live_source_commitment_invalid"),
            "commit": ({"expected_execution_commit": "0" * 64}, "deepseek_first_live_execution_commit_invalid"),
            "authorization": ({"authorization_id": "unsafe auth"}, "deepseek_first_live_authorization_invalid"),
            "authorization_binding_missing": (
                {"expected_authorization_binding_sha256": None},
                "deepseek_first_live_authorization_binding_required",
            ),
            "authorization_binding_invalid": (
                {"expected_authorization_binding_sha256": "bad"},
                "deepseek_first_live_authorization_binding_invalid",
            ),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                calls: list[str] = []
                arguments = dict(base)
                arguments.update(changes)
                arguments["_key_loader"] = lambda: calls.append("key") or SAFE_KEY
                arguments["_artifact_root"] = Path(directory)
                result = _run_impl(arguments)
                self.assertEqual(result["status"], "not_run")
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(calls, [])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_source_successor_fails_before_key_and_consumption(self) -> None:
        now = _now()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            arguments = _run_arguments(now)
            arguments["_key_loader"] = lambda: calls.append("key") or SAFE_KEY
            arguments["_artifact_root"] = Path(directory)
            result = _run_impl(arguments)
            self.assertEqual(result["status"], "not_run")
            self.assertEqual(
                result["error_code"],
                "deepseek_first_live_source_integrity_invalid",
            )
            self.assertEqual(calls, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_authorization_binding_mismatch_precedes_consumption_and_key(self) -> None:
        now = _now()
        key_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            arguments = _run_arguments(now)
            arguments["expected_authorization_binding_sha256"] = "0" * 64
            arguments["_key_loader"] = lambda: key_calls.append("key") or SAFE_KEY
            arguments["_artifact_root"] = Path(directory)
            result = _run_impl(arguments)
            self.assertEqual(result["status"], "not_run")
            self.assertEqual(
                result["error_code"],
                "deepseek_first_live_authorization_binding_mismatch",
            )
            self.assertEqual(key_calls, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_expiry_pricing_git_environment_and_wall_gates_precede_consumption(self) -> None:
        now = _now()
        base = _run_arguments(now)
        cases = (
            (
                "short_expiry",
                {
                    "authorization_expires_at_utc": (now + timedelta(seconds=100))
                    .isoformat()
                    .replace("+00:00", "Z")
                },
                None,
                "deepseek_first_live_authorization_window_too_short",
            ),
            (
                "long_horizon",
                {
                    "authorization_expires_at_utc": (now + timedelta(hours=25))
                    .isoformat()
                    .replace("+00:00", "Z")
                },
                None,
                "deepseek_first_live_authorization_horizon_exceeded",
            ),
            (
                "stale_pricing",
                {"pricing_snapshot_date": (now.date() - timedelta(days=1)).isoformat()},
                None,
                "deepseek_first_live_pricing_stale",
            ),
            (
                "pricing_origin",
                {"pricing_source_url": "https://example.com/pricing"},
                None,
                "deepseek_first_live_pricing_source_invalid",
            ),
            (
                "pricing_path_is_not_a_persistence_channel",
                {
                    "pricing_source_url": (
                        "https://api-docs.deepseek.com/zh-cn/quick_start/"
                        "pricing/sk-must-not-persist"
                    )
                },
                None,
                "deepseek_first_live_pricing_source_invalid",
            ),
            (
                "cost_reservation",
                {
                    "input_price_per_million_cny": "1000",
                    "output_price_per_million_cny": "1000",
                },
                None,
                "deepseek_first_live_cost_reservation_exceeded",
            ),
            (
                "wrong_commit",
                {},
                lambda root: ("c" * 40, True, "c" * 40),
                "deepseek_first_live_execution_tree_invalid",
            ),
            (
                "not_remote_main",
                {},
                lambda root: (EXECUTION_COMMIT, True, "c" * 40),
                "deepseek_first_live_execution_tree_invalid",
            ),
            (
                "dirty_tree",
                {},
                lambda root: (EXECUTION_COMMIT, False, EXECUTION_COMMIT),
                "deepseek_first_live_execution_tree_invalid",
            ),
        )
        with patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            for label, changes, git_loader, expected in cases:
                with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                    calls: list[str] = []
                    arguments = dict(base)
                    arguments.update(changes)
                    if git_loader is not None:
                        arguments["_git_state_loader"] = git_loader
                    arguments["_key_loader"] = lambda: calls.append("key") or SAFE_KEY
                    arguments["_artifact_root"] = Path(directory)
                    result = _run_impl(arguments)
                    self.assertEqual(result["status"], "not_run")
                    self.assertEqual(result["error_code"], expected)
                    self.assertEqual(calls, [])
                    self.assertEqual(list(Path(directory).iterdir()), [])

            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {"OPENAI_CUSTOM_HEADERS": "X-Unsafe: value"},
                clear=False,
            ):
                arguments = dict(base)
                arguments["_artifact_root"] = Path(directory)
                result = _run_impl(arguments)
                self.assertEqual(
                    result["error_code"],
                    "deepseek_first_live_environment_not_isolated",
                )
                self.assertEqual(list(Path(directory).iterdir()), [])

            with tempfile.TemporaryDirectory() as directory:
                monotonic_values = iter((0.0, 331.0))
                arguments = dict(base)
                arguments["_artifact_root"] = Path(directory)
                arguments["_monotonic"] = lambda: next(monotonic_values)
                result = _run_impl(arguments)
                self.assertEqual(result["status"], "not_run")
                self.assertEqual(
                    result["error_code"],
                    "deepseek_first_live_total_timeout",
                )
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_key_loader_failure_is_consumed_sanitized_and_not_retriable(self) -> None:
        now = _now()
        secret_exception = "key loader exploded: sk-never-persist-first-live"
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-key-failure"
            arguments["_artifact_root"] = Path(directory)

            def fail_key():
                raise RuntimeError(secret_exception)

            arguments["_key_loader"] = fail_key
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"], "deepseek_first_live_key_load_failed"
            )
            self.assertFalse(terminal["provider_key_loaded"])
            self.assertFalse(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=(secret_exception,),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid_failure_receipt")
            second = _run_impl(arguments)
            self.assertEqual(
                second["error_code"],
                "deepseek_first_live_authorization_already_consumed",
            )

    def test_no_ledger_receipt_rejects_stage_and_file_set_rewrites(self) -> None:
        now = _now()
        cases = (
            ("arbitrary_error", {"error_code": "forged_safe_error"}, False),
            ("key_state", {"provider_key_loaded": True}, False),
            ("cleanup_state", {"raw_response_cleanup_complete": True}, False),
            ("impossible_extra_file", {}, True),
        )
        for label, changes, add_file in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, patch(
                "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
                return_value=_source_integrity(),
            ):
                arguments = _run_arguments(now)
                arguments["authorization_id"] = f"deepseek-first-live-no-db-{label}"
                arguments["_artifact_root"] = Path(directory)

                def fail_key() -> str:
                    raise RuntimeError("synthetic key-loader failure")

                arguments["_key_loader"] = fail_key
                terminal = _run_impl(arguments)
                self.assertEqual(
                    terminal["error_code"],
                    "deepseek_first_live_key_load_failed",
                )
                authorization_hash = hashlib.sha256(
                    arguments["authorization_id"].encode()
                ).hexdigest()
                artifact = Path(directory) / authorization_hash
                terminal_path = artifact / "terminal.json"
                forged = json.loads(terminal_path.read_text(encoding="utf-8"))
                forged.update(changes)
                if add_file:
                    extra_path = artifact / "audit_index.json"
                    extra_body = _overwrite_canonical(extra_path, {})
                    forged["partial_artifacts"]["audit_index.json"] = {
                        "bytes": len(extra_body),
                        "sha256": hashlib.sha256(extra_body).hexdigest(),
                    }
                _overwrite_canonical(terminal_path, forged)
                with self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ) as caught:
                    self._verify(
                        ROOT,
                        authorization_hash,
                        _artifact_root=Path(directory),
                    )
                self.assertIn(
                    caught.exception.code,
                    {
                        "deepseek_first_live_partial_ledger_mismatch",
                        "deepseek_first_live_partial_manifest_invalid",
                    },
                )

    def test_no_ledger_runtime_and_audit_initialization_failures_are_bound(
        self,
    ) -> None:
        now = _now()
        cases = (
            (
                "runtime",
                "_mint_validation_tracker",
                "deepseek_first_live_runtime_initialization_failed",
            ),
            (
                "audit",
                "AuditLedger",
                "deepseek_first_live_audit_initialization_failed",
            ),
        )
        for label, target, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, patch(
                "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
                return_value=_source_integrity(),
            ):
                arguments = _run_arguments(now)
                arguments["authorization_id"] = (
                    f"deepseek-first-live-no-ledger-{label}"
                )
                arguments["_artifact_root"] = Path(directory)
                if target == "AuditLedger":
                    def fail_after_touch(path: Path):
                        Path(path).touch()
                        Path(f"{path}-wal").touch()
                        Path(f"{path}-shm").touch()
                        raise RuntimeError("synthetic audit initialization failure")

                    failure = fail_after_touch
                else:
                    failure = RuntimeError("synthetic runtime initialization failure")
                with patch.object(first_live, target, side_effect=failure):
                    terminal = _run_impl(arguments)
                self.assertEqual(terminal["error_code"], expected_error)
                self.assertTrue(terminal["provider_key_loaded"])
                authorization_hash = hashlib.sha256(
                    arguments["authorization_id"].encode()
                ).hexdigest()
                artifact = Path(directory) / authorization_hash
                self.assertFalse((artifact / "audit.sqlite3").exists())
                self.assertFalse((artifact / "audit.sqlite3-wal").exists())
                self.assertFalse((artifact / "audit.sqlite3-shm").exists())
                self.assertFalse((artifact / "audit.sqlite3-journal").exists())
                result = self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
                self.assertEqual(result["status"], "valid_failure_receipt")

    def test_manifestless_terminal_wrong_types_are_rejected(self) -> None:
        now = _now()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-wrong-types-test"
            arguments["_artifact_root"] = Path(directory)
            arguments["_key_loader"] = lambda: None
            terminal = _run_impl(arguments)
            self.assertFalse(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            terminal_path = Path(directory) / authorization_hash / "terminal.json"
            forged = json.loads(terminal_path.read_text(encoding="utf-8"))
            forged["provider_key_loaded"] = "false"
            forged["usage_complete"] = "false"
            forged["input_tokens"] = "0"
            forged["output_tokens"] = {}
            _overwrite_canonical(terminal_path, forged)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_terminal_invalid",
            )

    def test_validation_scope_cannot_enter_campaign_ledger_or_phase6(self) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        authority = first_live._ConsumedValidationAuthorization._create(
            first_live._AUTHORITY_TOKEN,
            authorization_id_sha256="1" * 64,
            authorization_binding_sha256="2" * 64,
            authorization_expires_at_utc=expiry,
            contract_commitment_sha256=first_live.CONTRACT_COMMITMENT_SHA256,
            implementation_commitment_sha256=(
                first_live.IMPLEMENTATION_COMMITMENT_SHA256
            ),
            execution_commit=EXECUTION_COMMIT,
            source_integrity_commitment_sha256=SOURCE_COMMITMENT,
            input_price_per_million_cny=first_live.Decimal("3"),
            output_price_per_million_cny=first_live.Decimal("9"),
        )
        tracker, plan = first_live._mint_validation_tracker(ROOT, authority)
        self.assertEqual(
            tracker.runtime_binding().authority_scope, "first_live_validation"
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = AuditLedger(Path(directory) / "audit.sqlite3")
            run_id = ledger.start_run(
                mode="scope-test", request_summary={"scope": "validation-only"}
            )
            with self.assertRaises(AuditError):
                LedgerCompletionTelemetrySession(
                    tracker.bind_case(first_live.VALIDATION_CASE_ID),
                    ledger=ledger,
                    run_id=run_id,
                    runtime_plan_binding=plan,
                )
        with self.assertRaises(Phase6RunError) as caught:
            _validate_runtime_denominator_tracker(
                tracker,
                required=True,
                adapter=DeepSeekProvider(),
                selected=(SimpleNamespace(task_id=first_live.VALIDATION_CASE_ID),),
                max_turns=2,
                deepseek_policy=None,
            )
        self.assertEqual(caught.exception.code, "completion_telemetry_tracker_invalid")
        with self.assertRaises(ProviderConfigurationError) as provider_scope:
            _validate_completion_session(
                SimpleNamespace(),
                provider_id="deepseek",
                api_surface="responses",
                transport_id="openai_compatible_responses",
                adapter_version="deepseek-responses-adapter/1.0",
            )
        self.assertEqual(
            provider_scope.exception.code,
            "provider_completion_session_binding_mismatch",
        )

    def test_transport_observer_rejects_a_second_send_for_one_attempt(self) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        authority = first_live._ConsumedValidationAuthorization._create(
            first_live._AUTHORITY_TOKEN,
            authorization_id_sha256="3" * 64,
            authorization_binding_sha256="4" * 64,
            authorization_expires_at_utc=expiry,
            contract_commitment_sha256=first_live.CONTRACT_COMMITMENT_SHA256,
            implementation_commitment_sha256=(
                first_live.IMPLEMENTATION_COMMITMENT_SHA256
            ),
            execution_commit=EXECUTION_COMMIT,
            source_integrity_commitment_sha256=SOURCE_COMMITMENT,
            input_price_per_million_cny=first_live.Decimal("3"),
            output_price_per_million_cny=first_live.Decimal("9"),
        )
        tracker, plan = first_live._mint_validation_tracker(ROOT, authority)
        with tempfile.TemporaryDirectory() as directory:
            ledger = AuditLedger(Path(directory) / "audit.sqlite3")
            run_id = ledger.start_run(
                mode="deepseek_completion_first_live_validation",
                run_id="RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                request_summary={"offline": "transport-retry-test"},
            )
            session = first_live._DeepSeekFirstLiveValidationLedgerSession(
                tracker.bind_case(first_live.VALIDATION_CASE_ID),
                ledger=ledger,
                run_id=run_id,
                runtime_plan_binding=plan,
                authorization=authority,
            )
            session.arm_deepseek_transport_observation()
            session.begin_attempt()
            request = httpx.Request(
                "POST",
                "https://api.deepseek.com/responses",
            )
            asyncio.run(session.observe_deepseek_transport_send(request))
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                asyncio.run(session.observe_deepseek_transport_send(request))
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_transport_retry_detected",
            )
            self.assertEqual(session.transport_observation_snapshot(), (1, True))

    def test_mocktransport_success_writes_only_sanitized_atomic_evidence(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [
                _response_body(
                    "completed",
                    256,
                    output_text=(
                        SAFE_KEY
                        + " Authorization: Bearer echoed-secret "
                        + r"C:\Users\private\traceback user@example.com"
                    ),
                ),
                _response_body("incomplete", 16),
            ]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(
                openai.AsyncOpenAI,
                OpenAIResponsesModel,
                transport.client_factory,
            ),
        ):
            arguments = _run_arguments(now)
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "success")
            self.assertEqual(terminal["network_calls"], 2)
            self.assertTrue(terminal["manifest_complete"])
            self.assertFalse(terminal["closure_claim_allowed"])
            self.assertEqual([item["max_output_tokens"] for item in transport.requests], [256, 16])
            self.assertTrue(
                all(item.get("stream", False) is False for item in transport.requests)
            )
            self.assertTrue(
                all(item.get("tools", []) == [] for item in transport.requests)
            )
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode("utf-8")
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=(SAFE_KEY,),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid")
            self.assertEqual(verification["terminal_status"], "success")
            self.assertEqual(verification["network_calls"], 2)
            self.assertEqual(verification["verification_network_calls"], 0)
            artifact_directory = Path(directory) / authorization_hash
            persisted = b"".join(
                path.read_bytes() for path in artifact_directory.iterdir() if path.is_file()
            )
            self.assertNotIn(SAFE_KEY.encode("utf-8"), persisted)
            contract = first_live.validate_deepseek_first_live_contract(ROOT)
            for scenario in contract["frozen_inputs"]["scenarios"]:
                self.assertNotIn(scenario["input"].encode("utf-8"), persisted)
            self.assertNotIn(arguments["authorization_id"].encode("utf-8"), persisted)
            self.assertNotIn(b"req_mock_1", persisted)
            self.assertNotIn(b"Authorization: Bearer echoed-secret", persisted)
            self.assertNotIn(b"C:\\Users\\private\\traceback", persisted)
            self.assertNotIn(b"user@example.com", persisted)

    def test_mocktransport_timeout_stops_and_persists_failure_without_retry(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [httpx.ReadTimeout("synthetic timeout")]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-timeout-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertTrue(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertEqual(len(transport.requests), 1)
            self.assertFalse(terminal["closure_claim_allowed"])
            self.assertTrue(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=("synthetic timeout",),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["terminal_status"], "failed")

    def test_transport_observer_write_failure_is_pre_send_no_response(self) -> None:
        now = _now()
        transport = _MockResponsesTransport([_response_body("completed", 256)])
        original_append = AuditLedger.append_event

        def fail_transport_event(
            ledger,
            run_id,
            event_type,
            payload,
            *,
            actor_kind="system",
        ):
            if event_type == "provider_transport_request_sent":
                raise AuditError(
                    "synthetic_transport_event_failure",
                    "must-not-be-persisted",
                )
            return original_append(
                ledger,
                run_id,
                event_type,
                payload,
                actor_kind=actor_kind,
            )

        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch.object(AuditLedger, "append_event", new=fail_transport_event):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-pre-send-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "provider_completion_no_response",
            )
            self.assertFalse(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 0)
            self.assertEqual(terminal["model_requests"], 1)
            self.assertEqual(transport.requests, [])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=("must-not-be-persisted",),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid")
            self.assertFalse(verification["outcome_unknown"])

    def test_first_success_second_timeout_does_not_publish_partial_usage_as_complete(
        self,
    ) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [
                _response_body("completed", 256),
                httpx.ReadTimeout("synthetic second timeout"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-partial-usage-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertTrue(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 2)
            self.assertEqual(terminal["model_requests"], 2)
            self.assertFalse(terminal["usage_complete"])
            self.assertIsNone(terminal["input_tokens"])
            self.assertIsNone(terminal["output_tokens"])
            self.assertIsNone(terminal["local_observed_usage_cost_cny"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            evidence = json.loads(
                (artifact / "completion_telemetry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                len(evidence["runtime_denominator"]["attempts"]),
                2,
            )
            self.assertEqual(
                len(evidence["runtime_denominator"]["records"]),
                1,
            )
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=("synthetic second timeout",),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid")
            self.assertTrue(verification["outcome_unknown"])
            terminal_path = artifact / "terminal.json"
            forged_terminal = json.loads(
                terminal_path.read_text(encoding="utf-8")
            )
            forged_terminal["outcome_unknown"] = False
            _overwrite_canonical(terminal_path, forged_terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )
            forged_terminal["outcome_unknown"] = True
            forged_terminal["usage_complete"] = True
            forged_terminal["input_tokens"] = 5
            forged_terminal["output_tokens"] = 1
            forged_terminal["local_observed_usage_cost_cny"] = "0.000024"
            _overwrite_canonical(terminal_path, forged_terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as usage:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                usage.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )

    def test_request_wall_timeout_bounds_the_entire_sdk_fetch(self) -> None:
        now = _now()
        transport = _HangingResponsesTransport()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch.object(
            first_live,
            "_REQUEST_TIMEOUT_SECONDS",
            0.5,
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-wall-timeout-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"], "deepseek_first_live_request_timeout"
            )
            self.assertTrue(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertLessEqual(len(transport.requests), 1)

    def test_failure_evidence_error_code_must_match_terminal(self) -> None:
        now = _now()
        transport = _MockResponsesTransport([httpx.ReadTimeout("synthetic timeout")])
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-failure-binding-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["error_code"] = "forged_safe_error"
            _overwrite_canonical(evidence_path, evidence)
            _refresh_manifest_and_terminal(
                artifact,
                ("completion_telemetry.json",),
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_failure_evidence_invalid",
            )

    def test_request_phase_has_an_independent_total_wall_timeout(self) -> None:
        now = _now()
        transport = _HangingResponsesTransport()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch.object(
            first_live,
            "_REQUEST_PHASE_TIMEOUT_SECONDS",
            0.5,
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-phase-timeout-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_request_phase_timeout",
            )
            self.assertTrue(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertLessEqual(len(transport.requests), 1)

    def test_raw_response_cleanup_failure_stops_after_one_mock_request(self) -> None:
        now = _now()
        transport = _MockResponsesTransport([_response_body("completed", 256)])
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.model_providers._close_response",
            new=AsyncMock(side_effect=RuntimeError("sk-cleanup-secret")),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-cleanup-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"], "provider_completion_raw_cleanup_failed"
            )
            self.assertFalse(terminal["outcome_unknown"])
            self.assertEqual(len(transport.requests), 1)
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            persisted = b"".join(
                path.read_bytes()
                for path in (Path(directory) / authorization_hash).iterdir()
                if path.is_file()
            )
            self.assertNotIn(b"sk-cleanup-secret", persisted)

    def test_post_request_cleanup_failure_is_consumed_after_two_clean_responses(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [
                _response_body("completed", 256),
                _response_body("incomplete", 16),
            ]
        )
        cleanup_error = ProviderConfigurationError(
            "provider_completion_post_request_cleanup_failed",
            "must-not-be-persisted",
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.model_providers._close_deepseek_transport_resources",
            new=AsyncMock(side_effect=cleanup_error),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-post-cleanup-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "provider_completion_post_request_cleanup_failed",
            )
            self.assertEqual(terminal["network_attempts"], 2)
            self.assertEqual(len(transport.requests), 2)
            self.assertFalse(terminal["outcome_unknown"])
            self.assertTrue(terminal["raw_response_cleanup_complete"])
            self.assertTrue(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            persisted = b"".join(
                path.read_bytes()
                for path in (Path(directory) / authorization_hash).iterdir()
                if path.is_file()
            )
            self.assertNotIn(b"must-not-be-persisted", persisted)
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=("must-not-be-persisted",),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["terminal_status"], "failed")

    def test_first_response_usage_cap_stops_before_second_request(self) -> None:
        now = _now()
        oversized = _response_body("completed", 256)
        oversized["usage"]["input_tokens"] = 513
        oversized["usage"]["total_tokens"] = 514
        transport = _MockResponsesTransport(
            [oversized, _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-usage-cap"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"], "deepseek_first_live_token_cap_exceeded"
            )
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertEqual(len(transport.requests), 1)
            self.assertFalse(terminal["usage_complete"])
            self.assertIsNone(terminal["input_tokens"])
            self.assertIsNone(terminal["output_tokens"])
            self.assertIsNone(terminal["local_observed_usage_cost_cny"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid")

    def test_first_response_shape_mismatch_stops_before_second_request(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [
                _response_body("incomplete", 256),
                _response_body("incomplete", 16),
            ]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-shape-mismatch-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_scenario_shape_mismatch",
            )
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertEqual(terminal["model_requests"], 1)
            self.assertEqual(len(transport.requests), 1)

    def test_second_response_total_cap_breach_writes_a_verifiable_null_usage_receipt(
        self,
    ) -> None:
        now = _now()
        first = _response_body("completed", 256)
        first["usage"]["input_tokens"] = 512
        first["usage"]["total_tokens"] = 513
        second = _response_body("incomplete", 16)
        second["usage"]["input_tokens"] = 513
        second["usage"]["total_tokens"] = 529
        transport = _MockResponsesTransport([first, second])
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-total-cap-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_token_cap_exceeded",
            )
            self.assertEqual(terminal["network_attempts"], 2)
            self.assertFalse(terminal["usage_complete"])
            self.assertIsNone(terminal["input_tokens"])
            self.assertIsNone(terminal["output_tokens"])
            self.assertIsNone(terminal["local_observed_usage_cost_cny"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid")

    def test_post_consumption_source_recheck_stops_before_network(self) -> None:
        now = _now()
        source_calls = 0

        def source_gate(*args, **kwargs):
            nonlocal source_calls
            del args, kwargs
            source_calls += 1
            if source_calls == 3:
                raise first_live.DeepSeekFirstLiveValidationError(
                    "deepseek_first_live_source_integrity_invalid",
                    not_run=True,
                )
            return _source_integrity()

        transport = _MockResponsesTransport([])
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            side_effect=source_gate,
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-source-recheck"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_runtime_revalidation_failed",
            )
            self.assertTrue(terminal["provider_key_loaded"])
            self.assertEqual(terminal["network_attempts"], 0)
            self.assertEqual(transport.requests, [])
            self.assertEqual(source_calls, 3)

    def test_post_consumption_revalidation_failure_has_no_ledger_stage(self) -> None:
        now = _now()
        source_calls = 0

        def source_gate(*args, **kwargs):
            nonlocal source_calls
            del args, kwargs
            source_calls += 1
            if source_calls == 2:
                raise first_live.DeepSeekFirstLiveValidationError(
                    "deepseek_first_live_source_integrity_invalid",
                    not_run=True,
                )
            return _source_integrity()

        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            side_effect=source_gate,
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = (
                "deepseek-first-live-post-consumption-revalidation"
            )
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_post_consumption_revalidation_failed",
            )
            self.assertFalse(terminal["provider_key_loaded"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            result = self._verify(
                ROOT,
                authorization_hash,
                _artifact_root=Path(directory),
            )
            self.assertEqual(result["status"], "valid_failure_receipt")

    def test_git_drift_after_consumption_and_between_requests_fails_closed(self) -> None:
        now = _now()
        valid = (EXECUTION_COMMIT, True, EXECUTION_COMMIT)
        invalid = (EXECUTION_COMMIT, False, EXECUTION_COMMIT)
        cases = (
            (
                "after-consumption",
                [valid, invalid],
                0,
                False,
                "deepseek_first_live_post_consumption_revalidation_failed",
            ),
            (
                "between-requests",
                [valid, valid, valid, invalid],
                1,
                True,
                "deepseek_first_live_runtime_revalidation_failed",
            ),
        )
        with patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            for label, states, expected_requests, key_loaded, expected_error in cases:
                with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                    transport = _MockResponsesTransport(
                        [
                            _response_body("completed", 256),
                            _response_body("incomplete", 16),
                        ]
                    )
                    with patch(
                        "researchops.model_providers._load_responses_transport",
                        return_value=(
                            openai.AsyncOpenAI,
                            OpenAIResponsesModel,
                            transport.client_factory,
                        ),
                    ):
                        queue = list(states)
                        arguments = _run_arguments(now)
                        arguments["authorization_id"] = (
                            f"deepseek-first-live-git-{label}"
                        )
                        arguments["_artifact_root"] = Path(directory)
                        arguments["_git_state_loader"] = lambda root: queue.pop(0)
                        terminal = _run_impl(arguments)
                    self.assertEqual(terminal["status"], "failed")
                    self.assertEqual(
                        terminal["error_code"],
                        expected_error,
                    )
                    self.assertEqual(len(transport.requests), expected_requests)
                    self.assertEqual(terminal["network_attempts"], expected_requests)
                    self.assertEqual(terminal["provider_key_loaded"], key_loaded)

    def test_external_cancellation_consumes_and_writes_terminal(self) -> None:
        now = _now()
        requests: list[httpx.Request] = []

        async def exercise(directory: str) -> tuple[dict, str]:
            entered = asyncio.Event()
            never = asyncio.Event()

            async def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                entered.set()
                await never.wait()
                raise AssertionError("unreachable")

            def client_factory(**kwargs):
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    timeout=120.0,
                    follow_redirects=False,
                    trust_env=False,
                    event_hooks=kwargs.get("event_hooks"),
                )

            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-cancelled"
            arguments["_artifact_root"] = Path(directory)
            with patch(
                "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
                return_value=_source_integrity(),
            ), patch(
                "researchops.model_providers._load_responses_transport",
                return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, client_factory),
            ):
                task = asyncio.create_task(
                    first_live._run_deepseek_first_live_validation_impl(
                        **_bound_run_arguments(arguments)
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            terminal = json.loads(
                (
                    Path(directory) / authorization_hash / "terminal.json"
                ).read_text(encoding="utf-8")
            )
            return terminal, authorization_hash

        with tempfile.TemporaryDirectory() as directory:
            terminal, authorization_hash = asyncio.run(exercise(directory))
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(
                terminal["error_code"], "deepseek_first_live_cancelled"
            )
            self.assertTrue(terminal["outcome_unknown"])
            self.assertEqual(terminal["network_attempts"], 1)
            self.assertEqual(len(requests), 1)
            verification = self._verify(
                ROOT,
                authorization_hash,
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["terminal_status"], "failed")

    def test_authorization_directory_makes_every_outcome_single_use(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-single-use-test"
            arguments["_artifact_root"] = Path(directory)
            first = _run_impl(arguments)
            self.assertEqual(first["status"], "success")
            key_calls: list[str] = []
            arguments["_key_loader"] = lambda: key_calls.append("key") or SAFE_KEY
            second = _run_impl(arguments)
            self.assertEqual(second["status"], "not_run")
            self.assertEqual(second["error_code"], "deepseek_first_live_authorization_already_consumed")
            self.assertEqual(key_calls, [])
            self.assertEqual(len(transport.requests), 2)

    def test_manifest_and_record_tampering_fail_closed(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-tamper-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "success")
            authorization_hash = hashlib.sha256(arguments["authorization_id"].encode()).hexdigest()
            target = Path(directory) / authorization_hash / "runtime_denominator.json"
            value = json.loads(target.read_text(encoding="utf-8"))
            value["records"][0]["normalized_completion_state"] = "error"
            target.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT, authorization_hash, _artifact_root=Path(directory)
                )
            self.assertEqual(caught.exception.code, "deepseek_first_live_artifact_hash_mismatch")

    def test_artifact_verifier_rejects_an_unexpected_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-directory-bypass-test",
            )
            unexpected = artifact / "raw"
            unexpected.mkdir()
            (unexpected / "provider.txt").write_text(
                "Authorization: Bearer synthetic-secret",
                encoding="utf-8",
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_artifact_file_set_invalid",
            )

    def test_artifact_verifier_revalidates_implementation_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-implementation-drift-test",
            )
            with patch(
                "researchops.deepseek_completion_first_live_validation."
                "validate_deepseek_first_live_implementation",
                side_effect=first_live.DeepSeekFirstLiveValidationError(
                    "deepseek_first_live_implementation_file_drift",
                    not_run=True,
                ),
            ), self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_implementation_file_drift",
            )

    def test_manifestless_failure_preserves_observed_network_count(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.deepseek_completion_first_live_validation._write_evidence_artifacts",
            side_effect=OSError("must-not-be-persisted"),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-no-manifest-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            self.assertFalse(terminal["manifest_complete"])
            self.assertEqual(terminal["network_attempts"], 2)
            self.assertEqual(terminal["network_calls"], 2)
            self.assertTrue(terminal["network_call_observation_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            verification = self._verify(
                ROOT,
                authorization_hash,
                sensitive_canaries=("must-not-be-persisted",),
                _artifact_root=Path(directory),
            )
            self.assertEqual(verification["status"], "valid_failure_receipt")
            self.assertEqual(verification["network_attempts"], 2)
            self.assertEqual(verification["network_calls"], 2)
            self.assertTrue(verification["network_call_observation_complete"])
            terminal_path = Path(directory) / authorization_hash / "terminal.json"
            forged = json.loads(terminal_path.read_text(encoding="utf-8"))
            forged["network_attempts"] = 0
            forged["network_calls"] = 0
            forged["model_requests"] = 0
            forged["provider_key_loaded"] = False
            forged["raw_response_cleanup_complete"] = False
            _overwrite_canonical(terminal_path, forged)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_partial_ledger_mismatch",
            )

    def test_full_receipt_rejects_key_and_cleanup_stage_rewrites(self) -> None:
        for field, value in (
            ("provider_key_loaded", False),
            ("raw_response_cleanup_complete", False),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                artifact, authorization_hash = _run_mock_success(
                    directory,
                    f"deepseek-first-live-full-stage-{field}",
                )
                terminal_path = artifact / "terminal.json"
                terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
                terminal[field] = value
                _overwrite_canonical(terminal_path, terminal)
                with self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ) as caught:
                    self._verify(
                        ROOT,
                        authorization_hash,
                        _artifact_root=Path(directory),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "deepseek_first_live_ledger_event_mismatch",
                )

    def test_ledger_receipt_rejects_unrecognized_outer_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-outer-error-allowlist",
            )
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["status"] = "failed"
            terminal["error_code"] = "forged_safe_error"
            _overwrite_canonical(terminal_path, terminal)
            with self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_evidence_schema_invalid",
            )
        with self.assertRaises(
            first_live.DeepSeekFirstLiveValidationError
        ) as stage:
            first_live._verify_terminal_stage_projection(
                {
                    "status": "failed",
                    "error_code": "forged_safe_error",
                    "ledger_run_status": "completed",
                    "provider_key_loaded": True,
                    "raw_response_cleanup_complete": True,
                },
                {
                    "started_attempt_indices": (0, 1),
                    "terminal_kinds": (
                        "response_accepted",
                        "response_accepted",
                    ),
                },
                input_price=first_live.Decimal("3"),
                output_price=first_live.Decimal("9"),
                mismatch_code="deepseek_first_live_ledger_event_mismatch",
            )
        self.assertEqual(
            stage.exception.code,
            "deepseek_first_live_ledger_event_mismatch",
        )

    def test_known_outer_error_must_match_the_ledger_trace_stage(self) -> None:
        cases = (
            (
                "deepseek_first_live_scenario_shape_mismatch",
                ("outcome_unknown",),
            ),
            (
                "deepseek_first_live_request_timeout",
                ("response_accepted",),
            ),
            (
                "deepseek_first_live_request_execution_failed",
                ("response_accepted",),
            ),
        )
        for error_code, kinds in cases:
            with self.subTest(error_code=error_code), self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                first_live._verify_terminal_stage_projection(
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "ledger_run_status": "failed",
                        "provider_key_loaded": True,
                        "raw_response_cleanup_complete": all(
                            kind == "response_accepted" for kind in kinds
                        ),
                        "manifest_complete": True,
                    },
                    {
                        "started_attempt_indices": tuple(range(len(kinds))),
                        "terminal_kinds": kinds,
                        "records": [],
                    },
                    input_price=first_live.Decimal("3"),
                    output_price=first_live.Decimal("9"),
                    mismatch_code="deepseek_first_live_ledger_event_mismatch",
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )

    def test_second_attempt_requires_the_first_scenario_gate_to_have_passed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = _run_mock_success(
                directory,
                "deepseek-first-live-invalid-first-prefix",
            )
            denominator = json.loads(
                (artifact / "runtime_denominator.json").read_text(
                    encoding="utf-8"
                )
            )
            records = denominator["records"]
            records[0]["normalized_completion_state"] = "incomplete_length"
            with self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                first_live._verify_terminal_stage_projection(
                    {
                        "status": "failed",
                        "error_code": (
                            "deepseek_first_live_scenario_shape_mismatch"
                        ),
                        "ledger_run_status": "failed",
                        "provider_key_loaded": True,
                        "raw_response_cleanup_complete": True,
                        "manifest_complete": True,
                    },
                    {
                        "started_attempt_indices": (0, 1),
                        "terminal_kinds": (
                            "response_accepted",
                            "response_accepted",
                        ),
                        "records": records,
                    },
                    input_price=first_live.Decimal("3"),
                    output_price=first_live.Decimal("9"),
                    mismatch_code="deepseek_first_live_ledger_event_mismatch",
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )

    def test_failure_trace_cannot_rewrite_locked_record_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = _run_mock_success(
                directory,
                "deepseek-first-live-failure-record-metadata",
            )
            denominator = json.loads(
                (artifact / "runtime_denominator.json").read_text(
                    encoding="utf-8"
                )
            )
            for label in ("output_cap", "provenance"):
                records = json.loads(json.dumps(denominator["records"]))
                if label == "output_cap":
                    records[0]["output_token_cap"]["value"] = 255
                else:
                    records[0]["record_provenance"] = "offline_validation"
                with self.subTest(label=label), self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ):
                    first_live._verify_terminal_stage_projection(
                        {
                            "status": "failed",
                            "error_code": (
                                "deepseek_first_live_request_phase_timeout"
                            ),
                            "ledger_run_status": "failed",
                            "provider_key_loaded": True,
                            "raw_response_cleanup_complete": True,
                            "manifest_complete": True,
                        },
                        {
                            "started_attempt_indices": (0, 1),
                            "terminal_kinds": (
                                "response_accepted",
                                "response_accepted",
                            ),
                            "records": records,
                        },
                        input_price=first_live.Decimal("3"),
                        output_price=first_live.Decimal("9"),
                        mismatch_code=(
                            "deepseek_first_live_ledger_event_mismatch"
                        ),
                    )

    def test_post_ledger_token_cap_state_is_not_claimable_from_persisted_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = _run_mock_success(
                directory,
                "deepseek-first-live-post-ledger-token-cap",
            )
            denominator = json.loads(
                (artifact / "runtime_denominator.json").read_text(
                    encoding="utf-8"
                )
            )
            records = denominator["records"]
            records[1]["usage"]["normalized"]["output_tokens"] = 17
            for error_code in (
                "deepseek_first_live_token_or_cost_cap_exceeded",
                "deepseek_first_live_post_ledger_validation_failed",
            ):
                with self.subTest(error_code=error_code), self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ):
                    first_live._verify_terminal_stage_projection(
                        {
                            "status": "failed",
                            "error_code": error_code,
                            "ledger_run_status": "completed",
                            "provider_key_loaded": True,
                            "raw_response_cleanup_complete": True,
                            "manifest_complete": True,
                        },
                        {
                            "started_attempt_indices": (0, 1),
                            "terminal_kinds": (
                                "response_accepted",
                                "response_accepted",
                            ),
                            "records": records,
                        },
                        input_price=first_live.Decimal("3"),
                        output_price=first_live.Decimal("9"),
                        mismatch_code=(
                            "deepseek_first_live_ledger_event_mismatch"
                        ),
                    )

    def test_known_wrong_stage_error_is_rejected_after_coordinated_rehash(
        self,
    ) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [httpx.ReadTimeout("synthetic timeout")]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(
                openai.AsyncOpenAI,
                OpenAIResponsesModel,
                transport.client_factory,
            ),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = (
                "deepseek-first-live-known-wrong-stage-rehash"
            )
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "failed")
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            database = artifact / "audit.sqlite3"
            forged_error = "deepseek_first_live_scenario_shape_mismatch"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE runs SET terminal_error_code = ? WHERE run_id = ?",
                    (
                        forged_error,
                        "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                    ),
                )
                connection.commit()
            _rewrite_audit_event_payload(
                database,
                target_event_type="run_status_changed",
                mutate=lambda payload: payload.update(
                    {"error_code": forged_error}
                ),
            )
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["runs"][0]["chain_verification"] = AuditLedger(
                database
            ).verify_chain(
                "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
            ).to_dict()
            _overwrite_canonical(index_path, index)
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["error_code"] = forged_error
            _overwrite_canonical(evidence_path, evidence)
            terminal_path = artifact / "terminal.json"
            forged_terminal = json.loads(
                terminal_path.read_text(encoding="utf-8")
            )
            forged_terminal["error_code"] = forged_error
            _overwrite_canonical(terminal_path, forged_terminal)
            _refresh_manifest_and_terminal(
                artifact,
                (
                    "audit.sqlite3",
                    "audit_index.json",
                    "completion_telemetry.json",
                ),
            )
            with self.assertRaises(
                first_live.DeepSeekFirstLiveValidationError
            ) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )

    def test_unknown_provider_code_is_normalized_to_observed_stage(self) -> None:
        now = _now()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            side_effect=ProviderConfigurationError(
                "provider_completion_canary_invalid",
                "synthetic provider initialization failure",
            ),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = (
                "deepseek-first-live-provider-code-normalization"
            )
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(
                terminal["error_code"],
                "deepseek_first_live_provider_initialization_failed",
            )
            self.assertEqual(terminal["model_requests"], 0)
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            result = self._verify(
                ROOT,
                authorization_hash,
                _artifact_root=Path(directory),
            )
            self.assertEqual(result["status"], "valid")

    def test_manifestless_writer_prefixes_are_verified_exactly(self) -> None:
        for prefix_length in range(4):
            with self.subTest(prefix_length=prefix_length), tempfile.TemporaryDirectory() as directory:
                artifact, authorization_hash = _run_mock_success(
                    directory,
                    f"deepseek-first-live-prefix-{prefix_length}",
                )
                _convert_success_to_manifestless_prefix(artifact, prefix_length)
                result = self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
                self.assertEqual(result["status"], "valid_failure_receipt")

    def test_manifestless_failure_evidence_prefixes_are_verified(self) -> None:
        for prefix_length in (2, 3):
            with self.subTest(prefix_length=prefix_length), tempfile.TemporaryDirectory() as directory, patch(
                "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
                return_value=_source_integrity(),
            ), patch(
                "researchops.model_providers._load_responses_transport",
                return_value=(
                    openai.AsyncOpenAI,
                    OpenAIResponsesModel,
                    _MockResponsesTransport(
                        [httpx.ReadTimeout("synthetic timeout")]
                    ).client_factory,
                ),
            ):
                arguments = _run_arguments(_now())
                arguments["authorization_id"] = (
                    f"deepseek-first-live-failure-prefix-{prefix_length}"
                )
                arguments["_artifact_root"] = Path(directory)
                terminal = _run_impl(arguments)
                self.assertEqual(terminal["status"], "failed")
                authorization_hash = hashlib.sha256(
                    arguments["authorization_id"].encode()
                ).hexdigest()
                artifact = Path(directory) / authorization_hash
                _convert_success_to_manifestless_prefix(
                    artifact,
                    prefix_length,
                    interrupted_success=False,
                )
                result = self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
                self.assertEqual(result["status"], "valid_failure_receipt")

    def test_manifestless_write_failed_self_report_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-write-failed-rewrite",
            )
            _convert_success_to_manifestless_prefix(artifact, 3)
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            commitment = index["runs"][0][
                "completion_telemetry_event_commitment"
            ]
            commitment["write_failed"] = True
            body = dict(commitment)
            body.pop("commitment_sha256")
            commitment["commitment_sha256"] = sha256_json(body)
            index_body = _overwrite_canonical(index_path, index)
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["ledger_reconciliation"]["ledger_failure_observed"] = True
            evidence["ledger_reconciliation"]["reasons"] = [
                "completion_telemetry_ledger_write_failed"
            ]
            evidence_body = _overwrite_canonical(evidence_path, evidence)
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            for filename, payload in (
                ("audit_index.json", index_body),
                ("completion_telemetry.json", evidence_body),
            ):
                terminal["partial_artifacts"][filename] = {
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            _overwrite_canonical(terminal_path, terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError):
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )

    def test_full_write_failed_self_report_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-full-write-failed-rewrite",
            )
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            commitment = index["runs"][0][
                "completion_telemetry_event_commitment"
            ]
            commitment["write_failed"] = True
            body = dict(commitment)
            body.pop("commitment_sha256")
            commitment["commitment_sha256"] = sha256_json(body)
            _overwrite_canonical(index_path, index)
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["ledger_reconciliation"]["ledger_failure_observed"] = True
            evidence["ledger_reconciliation"]["reasons"] = [
                "completion_telemetry_ledger_write_failed"
            ]
            _overwrite_canonical(evidence_path, evidence)
            _refresh_manifest_and_terminal(
                artifact,
                ("audit_index.json", "completion_telemetry.json"),
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError):
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )

    def test_manifestless_writer_prefix_rejects_gaps_and_invalid_json(self) -> None:
        cases = (
            ("gap", 3, "audit_index.json", None),
            ("audit_index", 1, "audit_index.json", {}),
            ("denominator", 2, "runtime_denominator.json", {}),
            ("evidence", 3, "completion_telemetry.json", {}),
        )
        for label, prefix_length, target, replacement in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                artifact, authorization_hash = _run_mock_success(
                    directory,
                    f"deepseek-first-live-prefix-invalid-{label}",
                )
                _convert_success_to_manifestless_prefix(artifact, prefix_length)
                terminal_path = artifact / "terminal.json"
                terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
                target_path = artifact / target
                if replacement is None:
                    target_path.unlink()
                    terminal["partial_artifacts"].pop(target)
                else:
                    body = _overwrite_canonical(target_path, replacement)
                    terminal["partial_artifacts"][target] = {
                        "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                _overwrite_canonical(terminal_path, terminal)
                with self.assertRaises(first_live.DeepSeekFirstLiveValidationError):
                    self._verify(
                        ROOT,
                        authorization_hash,
                        _artifact_root=Path(directory),
                    )

    def test_manifestless_completed_run_cannot_be_rewritten_to_zero_calls(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.deepseek_completion_first_live_validation._write_evidence_artifacts",
            side_effect=OSError("synthetic persistence failure"),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-zero-rewrite-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            database = artifact / "audit.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    "SELECT sequence, event_type, occurred_at_utc, actor_kind, "
                    "safe_payload_json, event_hash FROM audit_events "
                    "WHERE run_id = ? ORDER BY sequence",
                    ("RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",),
                ).fetchall()
                self.assertEqual(len(rows), 8)
                first_hash = rows[0][5]
                status_event = rows[-1]
                rewritten_hash = _event_hash(
                    run_id="RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                    sequence=2,
                    event_type=status_event[1],
                    occurred_at_utc=status_event[2],
                    actor_kind=status_event[3],
                    safe_payload_json=status_event[4],
                    prev_hash=first_hash,
                )
                connection.execute("DROP TRIGGER audit_events_no_update")
                connection.execute("DROP TRIGGER audit_events_no_delete")
                connection.execute(
                    "DELETE FROM audit_events WHERE sequence BETWEEN 2 AND 7"
                )
                connection.execute(
                    "UPDATE audit_events SET sequence = 2, prev_hash = ?, "
                    "event_hash = ? WHERE sequence = 8",
                    (first_hash, rewritten_hash),
                )
                connection.commit()
            self.assertTrue(
                AuditLedger(database)
                .verify_chain("RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001")
                .valid
            )
            terminal_path = artifact / "terminal.json"
            forged = json.loads(terminal_path.read_text(encoding="utf-8"))
            forged["network_attempts"] = 0
            forged["network_calls"] = 0
            forged["model_requests"] = 0
            forged["raw_response_cleanup_complete"] = False
            database_bytes = database.read_bytes()
            forged["partial_artifacts"]["audit.sqlite3"] = {
                "bytes": len(database_bytes),
                "sha256": hashlib.sha256(database_bytes).hexdigest(),
            }
            _overwrite_canonical(terminal_path, forged)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_partial_ledger_mismatch",
            )

    def test_manifestless_terminal_payload_is_exact_after_chain_rewrite(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.deepseek_completion_first_live_validation._write_evidence_artifacts",
            side_effect=OSError("synthetic persistence failure"),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-partial-payload-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertFalse(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            database = artifact / "audit.sqlite3"
            _rewrite_audit_event_payload(
                database,
                target_event_type="model_response_telemetry_recorded",
                mutate=lambda payload: payload.__setitem__("forged_extra", True),
            )
            self.assertTrue(
                AuditLedger(database)
                .verify_chain("RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001")
                .valid
            )
            terminal_path = artifact / "terminal.json"
            forged = json.loads(terminal_path.read_text(encoding="utf-8"))
            database_bytes = database.read_bytes()
            forged["partial_artifacts"]["audit.sqlite3"] = {
                "bytes": len(database_bytes),
                "sha256": hashlib.sha256(database_bytes).hexdigest(),
            }
            _overwrite_canonical(terminal_path, forged)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_partial_ledger_mismatch",
            )

    def test_manifestless_terminal_error_semantics_are_exact_after_rehash(self) -> None:
        now = _now()
        transport = _MockResponsesTransport([httpx.ReadTimeout("synthetic timeout")])
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ), patch(
            "researchops.deepseek_completion_first_live_validation._write_evidence_artifacts",
            side_effect=OSError("synthetic persistence failure"),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-error-semantics-test"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertFalse(terminal["manifest_complete"])
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            database = artifact / "audit.sqlite3"
            _rewrite_audit_event_payload(
                database,
                target_event_type="model_request_outcome_unknown",
                mutate=lambda payload: payload.__setitem__(
                    "error_code", "forged_safe_error"
                ),
            )
            self.assertTrue(
                AuditLedger(database)
                .verify_chain("RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001")
                .valid
            )
            terminal_path = artifact / "terminal.json"
            forged = json.loads(terminal_path.read_text(encoding="utf-8"))
            database_bytes = database.read_bytes()
            forged["partial_artifacts"]["audit.sqlite3"] = {
                "bytes": len(database_bytes),
                "sha256": hashlib.sha256(database_bytes).hexdigest(),
            }
            _overwrite_canonical(terminal_path, forged)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_partial_ledger_mismatch",
            )

    def test_self_consistent_extra_ledger_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-extra-event-test",
            )
            database = artifact / "audit.sqlite3"
            ledger = AuditLedger(database)
            run_id = "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
            ledger.append_event(
                run_id,
                "model_quality_claim",
                {"allowed": True},
            )
            verification = ledger.verify_chain(run_id)
            self.assertTrue(verification.valid)
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["runs"][0]["chain_verification"] = verification.to_dict()
            _overwrite_canonical(index_path, index)
            _refresh_manifest_and_terminal(
                artifact,
                ("audit.sqlite3", "audit_index.json"),
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_ledger_event_mismatch",
            )

    def test_self_consistent_extra_ledger_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-extra-run-test",
            )
            database = artifact / "audit.sqlite3"
            AuditLedger(database).start_run(
                mode="forged_extra_run",
                run_id="RUN-FORGED-EXTRA",
                request_summary={"forged": True},
            )
            _refresh_manifest_and_terminal(artifact, ("audit.sqlite3",))
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_audit_chain_invalid",
            )

    def test_ledger_schema_rejects_extra_objects_and_same_name_noop_trigger(
        self,
    ) -> None:
        mutations = {
            "extra_table": (
                "CREATE TABLE forged_payload(value TEXT)",
            ),
            "same_name_noop_trigger": (
                "DROP TRIGGER audit_events_no_update",
                "CREATE TRIGGER audit_events_no_update AFTER INSERT ON audit_events "
                "BEGIN SELECT 1; END",
            ),
        }
        for label, statements in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                artifact, authorization_hash = _run_mock_success(
                    directory,
                    f"deepseek-first-live-schema-{label}",
                )
                database = artifact / "audit.sqlite3"
                with contextlib.closing(sqlite3.connect(database)) as connection:
                    for statement in statements:
                        connection.execute(statement)
                    connection.commit()
                _refresh_manifest_and_terminal(artifact, ("audit.sqlite3",))
                with self.assertRaises(
                    first_live.DeepSeekFirstLiveValidationError
                ) as caught:
                    self._verify(
                        ROOT,
                        authorization_hash,
                        _artifact_root=Path(directory),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "deepseek_first_live_audit_chain_invalid",
                )

    def test_self_consistent_evidence_projection_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-evidence-projection-test",
            )
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["observed_states_in_order"] = ["error", "error"]
            _overwrite_canonical(evidence_path, evidence)
            _refresh_manifest_and_terminal(
                artifact,
                ("completion_telemetry.json",),
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_success_evidence_invalid",
            )

    def test_self_consistent_record_output_cap_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-record-cap-drift",
            )
            database = artifact / "audit.sqlite3"
            _rewrite_audit_event_payload(
                database,
                target_event_type="model_response_telemetry_recorded",
                mutate=lambda payload: payload["completion_record"][
                    "output_token_cap"
                ].__setitem__("value", 255),
            )
            ledger = AuditLedger(database)
            exported = ledger.export_run(
                "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
            )
            start_types = {"model_request_started"}
            terminal_types = {
                "model_response_telemetry_recorded",
                "model_response_telemetry_unmapped",
                "model_response_telemetry_rejected",
                "model_request_http_error",
                "model_request_no_response",
                "model_request_cancelled",
                "model_request_outcome_unknown",
            }
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            commitment = index["runs"][0][
                "completion_telemetry_event_commitment"
            ]
            commitment["started"] = [
                {
                    "attempt_index": event["safe_payload"]["attempt_index"],
                    "event_hash": event["event_hash"],
                }
                for event in exported["events"]
                if event["event_type"] in start_types
            ]
            commitment["terminals"] = [
                {
                    "attempt_index": event["safe_payload"]["attempt_index"],
                    "event_hash": event["event_hash"],
                }
                for event in exported["events"]
                if event["event_type"] in terminal_types
            ]
            body = dict(commitment)
            body.pop("commitment_sha256")
            commitment["commitment_sha256"] = sha256_json(body)
            index["runs"][0]["chain_verification"] = ledger.verify_chain(
                "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
            ).to_dict()
            _overwrite_canonical(index_path, index)
            denominator_path = artifact / "runtime_denominator.json"
            denominator = json.loads(
                denominator_path.read_text(encoding="utf-8")
            )
            denominator["records"][0]["output_token_cap"]["value"] = 255
            _overwrite_canonical(denominator_path, denominator)
            evidence_path = artifact / "completion_telemetry.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["runtime_denominator"]["records"][0][
                "output_token_cap"
            ]["value"] = 255
            _overwrite_canonical(evidence_path, evidence)
            _refresh_manifest_and_terminal(
                artifact,
                (
                    "audit.sqlite3",
                    "audit_index.json",
                    "runtime_denominator.json",
                    "completion_telemetry.json",
                ),
            )
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError):
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )

    def test_self_consistent_privacy_and_authority_claim_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-claim-drift-test",
            )
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["authorizes_evaluation"] = True
            _overwrite_canonical(terminal_path, terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_terminal_invalid",
            )

    def test_self_consistent_expired_authorization_timeline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-expired-timeline-test",
            )
            consumption_path = artifact / "consumption.json"
            consumption = json.loads(consumption_path.read_text(encoding="utf-8"))
            consumption["authorization_expires_at_utc"] = (
                "2000-01-01T00:00:00.000Z"
            )
            consumption_bytes = _overwrite_canonical(consumption_path, consumption)
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["authorization_expires_at_utc"] = (
                "2000-01-01T00:00:00.000Z"
            )
            terminal["consumption_receipt_sha256"] = hashlib.sha256(
                consumption_bytes
            ).hexdigest()
            _overwrite_canonical(terminal_path, terminal)
            _refresh_manifest_and_terminal(artifact, ("consumption.json",))
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_receipt_time_order_invalid",
            )

    def test_external_authorization_binding_rejects_coordinated_receipt_rewrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-external-binding-test",
            )
            consumption_path = artifact / "consumption.json"
            consumption = json.loads(consumption_path.read_text(encoding="utf-8"))
            original_binding = consumption["authorization_binding_sha256"]
            expiry = datetime.fromisoformat(
                consumption["authorization_expires_at_utc"].replace("Z", "+00:00")
            ) + timedelta(minutes=1)
            changed_expiry = expiry.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            forged_binding = first_live._authorization_binding(
                authorization_id_sha256=authorization_hash,
                expires_at_utc=changed_expiry,
                execution_commit=consumption["execution_commit"],
                source_integrity_commitment=consumption[
                    "source_integrity_commitment_sha256"
                ],
                pricing_snapshot_date=consumption["pricing_snapshot_date"],
                pricing_source_url=consumption["pricing_source_url"],
                input_price=first_live.Decimal(
                    consumption["input_price_per_million_cny"]
                ),
                output_price=first_live.Decimal(
                    consumption["output_price_per_million_cny"]
                ),
            )
            consumption["authorization_expires_at_utc"] = changed_expiry
            consumption["authorization_binding_sha256"] = forged_binding
            consumption_bytes = _overwrite_canonical(consumption_path, consumption)
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["authorization_expires_at_utc"] = changed_expiry
            terminal["authorization_binding_sha256"] = forged_binding
            terminal["consumption_receipt_sha256"] = hashlib.sha256(
                consumption_bytes
            ).hexdigest()
            _overwrite_canonical(terminal_path, terminal)
            _refresh_manifest_and_terminal(artifact, ("consumption.json",))
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    expected_authorization_binding_sha256=original_binding,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_authorization_binding_mismatch",
            )

    def test_terminal_receipt_after_authorization_deadline_is_rejected(self) -> None:
        now = _now()
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-deadline-receipt-test"
            arguments["_artifact_root"] = Path(directory)
            arguments["_key_loader"] = lambda: None
            terminal = _run_impl(arguments)
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            consumption_path = artifact / "consumption.json"
            consumption = json.loads(consumption_path.read_text(encoding="utf-8"))
            consumed_at = datetime.fromisoformat(
                consumption["consumed_at_utc"].replace("Z", "+00:00")
            )
            deadline = consumed_at + timedelta(seconds=330)
            deadline_text = deadline.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            consumption["authorization_expires_at_utc"] = deadline_text
            consumption_bytes = _overwrite_canonical(consumption_path, consumption)
            terminal_path = artifact / "terminal.json"
            terminal["authorization_expires_at_utc"] = deadline_text
            terminal["completed_at_utc"] = (
                (deadline + timedelta(seconds=1))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            terminal["consumption_receipt_sha256"] = hashlib.sha256(
                consumption_bytes
            ).hexdigest()
            terminal["partial_artifacts"]["consumption.json"] = {
                "bytes": len(consumption_bytes),
                "sha256": hashlib.sha256(consumption_bytes).hexdigest(),
            }
            _overwrite_canonical(terminal_path, terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_receipt_time_order_invalid",
            )

    def test_self_consistent_manifest_privacy_flag_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, authorization_hash = _run_mock_success(
                directory,
                "deepseek-first-live-manifest-privacy-test",
            )
            manifest_path = artifact / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["message_content_persisted"] = True
            manifest_bytes = _overwrite_canonical(manifest_path, manifest)
            terminal_path = artifact / "terminal.json"
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
            _overwrite_canonical(terminal_path, terminal)
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT,
                    authorization_hash,
                    _artifact_root=Path(directory),
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_manifest_invalid",
            )

    def test_success_usage_validator_rejects_per_response_cap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = _run_mock_success(
                directory,
                "deepseek-first-live-cap-recompute-test",
            )
            denominator = json.loads(
                (artifact / "runtime_denominator.json").read_text(encoding="utf-8")
            )
            denominator["records"][1]["usage"]["normalized"][
                "output_tokens"
            ] = 17
            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                first_live._validated_success_usage(
                    denominator["records"],
                    input_price=first_live.Decimal("3"),
                    output_price=first_live.Decimal("9"),
                    error_code="deepseek_first_live_success_evidence_invalid",
                )
            self.assertEqual(
                caught.exception.code,
                "deepseek_first_live_success_evidence_invalid",
            )

    def test_reconciled_usage_requires_attempt_send_record_bijection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = _run_mock_success(
                directory,
                "deepseek-first-live-usage-bijection-test",
            )
            denominator = json.loads(
                (artifact / "runtime_denominator.json").read_text(encoding="utf-8")
            )
            records = denominator["records"]
            self.assertEqual(
                first_live._reconciled_usage_totals(
                    records,
                    started_attempt_indices=(0, 1),
                    sent_attempt_indices=(0, 1),
                    network_observation_complete=True,
                ),
                (10, 17),
            )
            cases = (
                ((0, 1), (0,), records, True),
                ((0, 1), (0, 1), records[:1], True),
                ((0, 1), (0, 1), records, False),
            )
            wrong_index = json.loads(json.dumps(records))
            wrong_index[1]["request_index"] = 0
            cases += (((0, 1), (0, 1), wrong_index, True),)
            incomplete = json.loads(json.dumps(records))
            incomplete[1]["usage"]["complete"] = False
            cases += (((0, 1), (0, 1), incomplete, True),)
            for started, sent, candidate, observed in cases:
                with self.subTest(
                    started=started,
                    sent=sent,
                    observed=observed,
                ):
                    self.assertIsNone(
                        first_live._reconciled_usage_totals(
                            candidate,
                            started_attempt_indices=started,
                            sent_attempt_indices=sent,
                            network_observation_complete=observed,
                        )
                    )

    def test_self_consistent_hash_rewrite_cannot_hide_ledger_type_drift(self) -> None:
        now = _now()
        transport = _MockResponsesTransport(
            [_response_body("completed", 256), _response_body("incomplete", 16)]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "researchops.deepseek_completion_first_live_validation._validate_source_integrity",
            return_value=_source_integrity(),
        ), patch(
            "researchops.model_providers._load_responses_transport",
            return_value=(openai.AsyncOpenAI, OpenAIResponsesModel, transport.client_factory),
        ):
            arguments = _run_arguments(now)
            arguments["authorization_id"] = "deepseek-first-live-self-consistent-tamper"
            arguments["_artifact_root"] = Path(directory)
            terminal = _run_impl(arguments)
            self.assertEqual(terminal["status"], "success")
            authorization_hash = hashlib.sha256(
                arguments["authorization_id"].encode()
            ).hexdigest()
            artifact = Path(directory) / authorization_hash
            database = artifact / "audit.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT sequence, event_type, occurred_at_utc, actor_kind, "
                    "safe_payload_json, prev_hash FROM audit_events "
                    "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                    ("RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",),
                ).fetchone()
                self.assertIsNotNone(row)
                forged_type = first_live.COMPLETION_TELEMETRY_UNMAPPED_EVENT
                forged_hash = _event_hash(
                    run_id="RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                    sequence=row[0],
                    event_type=forged_type,
                    occurred_at_utc=row[2],
                    actor_kind=row[3],
                    safe_payload_json=row[4],
                    prev_hash=row[5],
                )
                connection.execute("DROP TRIGGER audit_events_no_update")
                connection.execute(
                    "UPDATE audit_events SET event_type = ?, event_hash = ? "
                    "WHERE run_id = ? AND sequence = ?",
                    (
                        forged_type,
                        forged_hash,
                        "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                        row[0],
                    ),
                )
                connection.commit()

            verification = AuditLedger(database).verify_chain(
                "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
            )
            self.assertTrue(verification.valid)
            index_path = artifact / "audit_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            run = index["runs"][0]
            run["chain_verification"] = verification.to_dict()
            commitment = run["completion_telemetry_event_commitment"]
            commitment["terminals"][-1]["event_hash"] = forged_hash
            commitment_body = dict(commitment)
            commitment_body.pop("commitment_sha256")
            commitment["commitment_sha256"] = sha256_json(commitment_body)
            index_bytes = _overwrite_canonical(index_path, index)

            manifest_path = artifact / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for filename, payload in (
                ("audit.sqlite3", database.read_bytes()),
                ("audit_index.json", index_bytes),
            ):
                manifest["files"][filename] = {
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            manifest_bytes = _overwrite_canonical(manifest_path, manifest)
            terminal_path = artifact / "terminal.json"
            changed_terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            changed_terminal["manifest_sha256"] = hashlib.sha256(
                manifest_bytes
            ).hexdigest()
            _overwrite_canonical(terminal_path, changed_terminal)

            with self.assertRaises(first_live.DeepSeekFirstLiveValidationError) as caught:
                self._verify(
                    ROOT, authorization_hash, _artifact_root=Path(directory)
                )
            self.assertEqual(
                caught.exception.code, "deepseek_first_live_ledger_event_mismatch"
            )


if __name__ == "__main__":
    unittest.main()
