"""Internal one-attempt httpx2 transport instrumentation; no admission factory.

The caller must already possess a clock/attempt created behind the future
consumed-authority gate. This module is not enabled by Provider.open_model.
It neither retains nor emits request/body/header values as telemetry.
"""
from __future__ import annotations

import asyncio

import httpx2

from .clock import TimingCaptureError, _TimingClock


class _TimedResponseStream(httpx2.AsyncByteStream):
    def __init__(self, delegate, transport):
        self._delegate = delegate
        self._transport = transport
        self._started = False
        self._closed = False

    async def __aiter__(self):
        if self._started or self._closed:
            self._transport._clock.abort_new_work()
            raise TimingCaptureError("timing_response_stream_reused")
        self._started = True
        iterator = self._delegate.__aiter__()
        try:
            while True:
                remaining = self._transport._clock.remaining_request_ns(self._transport._handle)
                if remaining <= 0:
                    # timeout(0) only schedules cancellation for a future loop
                    # turn. A synchronous async iterator may never yield one.
                    self._transport._clock.abort_new_work()
                    raise TimingCaptureError("timing_request_budget_exhausted")
                async with asyncio.timeout(remaining / 1_000_000_000):
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                yield chunk
            self._transport._body_complete = True
            self._transport._terminal()
        except asyncio.CancelledError:
            self._transport._terminal()
            raise
        except Exception:
            self._transport._terminal()
            raise TimingCaptureError("timing_response_read_failed") from None

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        outcome = "unknown"
        try:
            # Closing a partially-read body is not an accepted full response,
            # but still records the local transport terminal and cleanup.
            self._transport._terminal()
            remaining = self._transport._clock.remaining_phase_ns()
            async with asyncio.timeout(min(remaining / 1_000_000_000, 5.0)):
                await self._delegate.aclose()
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            outcome = "timed_out"
            raise TimingCaptureError("timing_raw_cleanup_timed_out") from None
        except Exception:
            outcome = "failed"
            raise TimingCaptureError("timing_raw_cleanup_failed") from None
        finally:
            self._delegate = None
            self._transport._cleanup(outcome)


class _TimedHTTPTransport(httpx2.AsyncBaseTransport):
    """One underlying send; measures headers+body, then separately raw cleanup.

    No redirects/retries are implemented. A second HTTP request through this
    wrapper is rejected before the delegate. Construction grants no authority.
    The internal delegate is supplied by the future frozen factory, or by offline
    MockTransport tests; it is not a new public Provider injection parameter.
    """

    def __init__(self, delegate, *, clock, attempt_handle):
        if not isinstance(delegate, httpx2.AsyncBaseTransport) or type(clock) is not _TimingClock:
            raise TimingCaptureError("timing_transport_input_invalid")
        self._delegate = delegate
        self._clock = clock
        self._handle = attempt_handle
        self._sent = False
        self._terminal_recorded = False
        self._cleanup_recorded = False
        self._cleanup_status = "unknown"
        self._body_complete = False
        self._closed = False

    def _terminal(self):
        if not self._terminal_recorded:
            self._clock.transport_terminal(self._handle)
            self._terminal_recorded = True

    def _cleanup(self, status):
        if not self._cleanup_recorded:
            self._clock.raw_cleanup_terminal(self._handle, status=status)
            self._cleanup_recorded = True
            self._cleanup_status = status

    def observation(self):
        return {"send_observed": self._sent, "full_body_observed": self._body_complete,
                "raw_cleanup_status": self._cleanup_status}

    async def _discard_unreturned_response(self, response):
        if response is None:
            self._cleanup("not_applicable")
        elif isinstance(response, httpx2.Response) and isinstance(response.stream, httpx2.AsyncByteStream):
            # If wrapping fails after the delegate returned, HTTPX never owns
            # the new response. We must still close the original stream.
            await _TimedResponseStream(response.stream, self).aclose()
        else:
            self._cleanup("unknown")

    async def handle_async_request(self, request):
        if self._closed or self._sent:
            self._clock.abort_new_work()
            raise TimingCaptureError("timing_transport_reused")
        if (request.method != "POST" or request.url.scheme != "https"
            or request.url.host != "api.deepseek.com" or request.url.port not in (None, 443)
            or request.url.raw_path != b"/responses" or request.url.fragment
            or request.url.username or request.url.password):
            self._clock.abort_new_work()
            raise TimingCaptureError("timing_transport_request_invalid")
        remaining = self._clock.send_started(self._handle)
        if type(remaining) is not int or remaining <= 0:
            self._clock.abort_new_work()
            raise TimingCaptureError("timing_dispatch_budget_invalid")
        self._sent = True
        response = None
        try:
            async with asyncio.timeout(remaining / 1_000_000_000):
                response = await self._delegate.handle_async_request(request)
            if not isinstance(response, httpx2.Response) or not isinstance(response.stream, httpx2.AsyncByteStream):
                raise TimingCaptureError("timing_transport_response_invalid")
            # Return a non-preloaded response even when a MockTransport delegate
            # returned cached content. The client must traverse the observed
            # stream; do not rely on an SDK-private _content flag.
            return httpx2.Response(response.status_code, headers=response.headers,
                stream=_TimedResponseStream(response.stream, self), extensions=response.extensions)
        except asyncio.CancelledError:
            self._terminal()
            await self._discard_unreturned_response(response)
            raise
        except Exception:
            self._terminal()
            await self._discard_unreturned_response(response)
            raise TimingCaptureError("timing_transport_failed") from None

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        delegate, self._delegate = self._delegate, None
        try:
            remaining = self._clock.remaining_phase_ns()
            async with asyncio.timeout(min(remaining / 1_000_000_000, 5.0)):
                await delegate.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TimingCaptureError("timing_post_cleanup_failed") from None
