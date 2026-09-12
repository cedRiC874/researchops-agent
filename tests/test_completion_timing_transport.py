"""httpx2 MockTransport/stream boundaries; zero real network/Provider calls."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx2

from researchops_completion_timing.clock import TimingCaptureError, _TimingClock
from researchops_completion_timing.transport import _TimedHTTPTransport


class TimingTransportTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, *, request_cap=100_000_000, phase_cap=2_000_000_000):
        clock = _TimingClock(request_timeout_ns=request_cap, phase_timeout_ns=phase_cap)
        clock.task_released(); clock.key_loaded()
        return clock, clock.begin_attempt()

    async def test_body_terminal_precedes_automatic_cleanup_and_later_finalization(self):
        current = [1000]
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                current[0] = 1020
                yield b"synthetic-"
                current[0] = 1040
                yield b"body"
            async def aclose(self):
                current[0] = 1120
        with patch("researchops_completion_timing.clock.time.monotonic_ns", side_effect=lambda: current[0]):
            clock, handle = self.ready(request_cap=60, phase_cap=500)
            transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body())), clock=clock, attempt_handle=handle)
            async with httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
                response = await client.post("https://api.deepseek.com/responses", content=b"synthetic")
                self.assertEqual(response.content, b"synthetic-body")
                observed = clock.snapshot()["active_attempt"]
                self.assertEqual(observed["transport_terminal_ns"], 40)
                self.assertEqual(observed["raw_cleanup_terminal_ns"], 120)
                self.assertEqual(transport.observation(), {"send_observed": True, "full_body_observed": True, "raw_cleanup_status": "completed"})
                current[0] = 1200
                clock.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
                self.assertEqual(clock.snapshot()["attempts"][0]["lifetime_ended_ns"], 120)

    async def test_preloaded_mock_response_cannot_skip_observed_stream(self):
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: httpx2.Response(200, json={"synthetic": True})), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.post("https://api.deepseek.com/responses")
            self.assertEqual(response.json(), {"synthetic": True})
            self.assertTrue(transport.observation()["full_body_observed"])
            self.assertEqual(transport.observation()["raw_cleanup_status"], "completed")
            clock.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")

    async def test_exhausted_budget_does_not_read_another_synchronous_body_chunk(self):
        reads = []
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                # Finite, so the pre-fix regression cannot hang the test process.
                for index in range(3):
                    reads.append(index)
                    yield b"synthetic"
            async def aclose(self):
                pass
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body())), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            with patch.object(clock, "remaining_request_ns", return_value=0):
                with self.assertRaisesRegex(TimingCaptureError, "timing_response_read_failed"):
                    await client.post("https://api.deepseek.com/responses")
        self.assertEqual(reads, [])
        self.assertTrue(clock.snapshot()["halted"])
        self.assertFalse(transport.observation()["full_body_observed"])
        self.assertEqual(transport.observation()["raw_cleanup_status"], "completed")

    async def test_phase_expiry_at_dispatch_does_not_invoke_delegate_or_mark_send(self):
        current = [1000]
        delegated = []
        with patch("researchops_completion_timing.clock.time.monotonic_ns", side_effect=lambda: current[0]):
            clock, handle = self.ready(request_cap=50, phase_cap=200)
            transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: delegated.append(True)), clock=clock, attempt_handle=handle)
            current[0] = 1200
            async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
                with self.assertRaisesRegex(TimingCaptureError, "timing_phase_timeout_exceeded"):
                    await client.post("https://api.deepseek.com/responses")
            self.assertEqual(delegated, [])
            self.assertFalse(transport.observation()["send_observed"])
            self.assertIsNone(clock.snapshot()["active_attempt"]["send_started_ns"])

    async def test_response_wrapping_failure_still_closes_the_unreturned_stream(self):
        closed = []
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"synthetic"
            async def aclose(self):
                closed.append(True)
        class BrokenHeaders(httpx2.Response):
            def __getattribute__(self, name):
                if name == "headers" and self.__dict__.get("break_headers", False):
                    raise ValueError("synthetic private exception text")
                return super().__getattribute__(name)
        response = BrokenHeaders(200, stream=Body())
        response.break_headers = True
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: response), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            with self.assertRaisesRegex(TimingCaptureError, "^timing_transport_failed$"):
                await client.post("https://api.deepseek.com/responses")
            self.assertEqual(closed, [True])
            self.assertFalse(transport.observation()["full_body_observed"])
            self.assertEqual(transport.observation()["raw_cleanup_status"], "completed")

    async def test_stalled_headers_are_cancelled_under_request_budget(self):
        calls = []
        async def handler(_):
            calls.append("entered")
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("cancelled")
        clock, handle = self.ready(request_cap=20_000_000)
        transport = _TimedHTTPTransport(httpx2.MockTransport(handler), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            with self.assertRaisesRegex(TimingCaptureError, "timing_transport_failed"):
                await asyncio.wait_for(client.post("https://api.deepseek.com/responses"), timeout=1)
            self.assertEqual(calls, ["entered", "cancelled"])
            self.assertEqual(transport.observation()["raw_cleanup_status"], "not_applicable")
            clock.finish_attempt(handle, terminal_kind="outcome_unknown", raw_cleanup_status="not_applicable")
            with self.assertRaises(TimingCaptureError):
                clock.begin_attempt()

    async def test_stalled_body_is_cancelled_and_cleanup_still_runs(self):
        calls = []
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"synthetic-prefix"
                try:
                    await asyncio.Event().wait()
                finally:
                    calls.append("cancelled")
            async def aclose(self):
                calls.append("closed")
        clock, handle = self.ready(request_cap=20_000_000)
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body())), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            with self.assertRaisesRegex(TimingCaptureError, "timing_response_read_failed"):
                await asyncio.wait_for(client.post("https://api.deepseek.com/responses"), timeout=1)
            self.assertEqual(calls, ["cancelled", "closed"])
            self.assertFalse(transport.observation()["full_body_observed"])
            clock.finish_attempt(handle, terminal_kind="outcome_unknown", raw_cleanup_status="completed")

    async def test_concurrency_lifetime_stays_reserved_during_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"synthetic"
            async def aclose(self):
                entered.set()
                await release.wait()
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body())), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            task = asyncio.create_task(client.post("https://api.deepseek.com/responses"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                with self.assertRaisesRegex(TimingCaptureError, "timing_concurrency_overlap"):
                    clock.begin_attempt()
            finally:
                release.set()
                await asyncio.wait_for(task, timeout=1)
            clock.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
            self.assertTrue(clock.snapshot()["halted"])

    async def test_retries_and_wrong_origin_are_rejected_before_delegate(self):
        calls = []
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda request: (calls.append(True) or httpx2.Response(200, content=b"synthetic"))), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            await client.post("https://api.deepseek.com/responses")
            with self.assertRaisesRegex(TimingCaptureError, "timing_transport_reused"):
                await client.post("https://api.deepseek.com/responses")
            self.assertEqual(calls, [True])
            self.assertTrue(clock.snapshot()["halted"])
        clock, handle = self.ready()
        transport = _TimedHTTPTransport(httpx2.MockTransport(lambda _: calls.append(False)), clock=clock, attempt_handle=handle)
        async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
            with self.assertRaisesRegex(TimingCaptureError, "timing_transport_request_invalid"):
                await client.post("https://example.invalid/responses")
            self.assertEqual(calls, [True])
            self.assertFalse(transport.observation()["send_observed"])


if __name__ == "__main__":
    unittest.main()
