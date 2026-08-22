from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .application import PilotApplication
from .domain import (
    AuthenticationRequired,
    AttemptNotFound,
    CampaignNotFound,
    CampaignNotAvailable,
    CsrfInvalid,
    InviteInvalid,
    OutputWithheld,
    PilotError,
    ProviderBudgetExhausted,
    ProviderUnavailable,
    RateLimitExceeded,
    SessionExpired,
)
from .web import PILOT_CSS, PILOT_HTML, PILOT_JS


SESSION_COOKIE = "researchops_pilot_session"
CSRF_COOKIE = "researchops_pilot_csrf"
MAX_BODY_BYTES = 65_536


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteExchangeRequest(StrictModel):
    invite_token: str = Field(min_length=20, max_length=256)


class ConsentRequest(StrictModel):
    adult_and_voluntary: bool
    experimental_system_understood: bool
    public_data_only: bool
    provider_transfer_understood: bool
    pseudonymous_recording_agreed: bool
    withdrawal_understood: bool
    external_researcher_eligible: bool


class FeedbackRequest(StrictModel):
    understandable: bool
    useful_for_next_step: bool
    confidence: str
    needs_expert_review: bool
    obvious_problem: bool
    missing_information: bool
    safety_concern: bool
    clarification_useful: bool | None = None
    notes: str = Field(default="", max_length=2000)


class CampaignRequest(StrictModel):
    title: str
    protocol_sha256: str
    consent_sha256: str
    feedback_schema_sha256: str
    dataset_manifest_sha256: str
    deployment_git_sha: str | None
    deployment_image_digest: str | None
    candidate_commitment_sha256: str
    provider: dict[str, str]
    target_participants: int
    max_provider_runs: int
    tasks: list[dict[str, Any]]


class InviteRequest(StrictModel):
    ttl_hours: int = 72


class IncidentResolutionRequest(StrictModel):
    resolution: str


@dataclass(slots=True)
class PilotContainer:
    application: PilotApplication
    admin_token: str
    allowed_hosts: tuple[str, ...]
    secure_cookies: bool
    ready_checks: tuple[Callable[[], bool], ...]
    initialize: Callable[[], None] = lambda: None
    close: Callable[[], None] = lambda: None

    def ready(self) -> bool:
        return all(check() for check in self.ready_checks)


class AdminAuth:
    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token
        self._scheme = HTTPBearer(auto_error=False)

    async def __call__(self, request: Request) -> str:
        credentials: HTTPAuthorizationCredentials | None = await self._scheme(request)
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, self._expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "admin_authentication_required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()


def create_app(container: PilotContainer) -> FastAPI:
    admin_auth = AdminAuth(container.admin_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        container.initialize()
        try:
            yield
        finally:
            container.close()

    app = FastAPI(
        title="ResearchOps Pilot Staging",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(container.allowed_hosts))

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > MAX_BODY_BYTES
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"error_code": "request_too_large"},
                )
        total = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"error_code": "request_too_large"},
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)  # Starlette caches the bounded body for downstream parsing.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if container.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(PilotError)
    async def pilot_error_handler(_, exc: PilotError):
        code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, (AuthenticationRequired, SessionExpired, CsrfInvalid, InviteInvalid)):
            code = status.HTTP_401_UNAUTHORIZED
        elif isinstance(exc, (CampaignNotFound, AttemptNotFound)):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, (CampaignNotAvailable, OutputWithheld, ProviderBudgetExhausted)):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, RateLimitExceeded):
            code = status.HTTP_429_TOO_MANY_REQUESTS
        elif isinstance(exc, ProviderUnavailable):
            code = status.HTTP_503_SERVICE_UNAVAILABLE
        headers = {"Retry-After": "60"} if code == 429 else None
        return JSONResponse(
            status_code=code,
            content={"error_code": exc.code},
            headers=headers,
        )

    def participant(
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ):
        session = container.application.authenticate(session_token)
        container.application.enforce_rate_limit(
            principal_key=session.participant_id, route_key="read", limit=120
        )
        return session

    def participant_write(
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ):
        session = container.application.authenticate(
            session_token, csrf_token=csrf_token or ""
        )
        container.application.enforce_rate_limit(
            principal_key=session.participant_id, route_key="write", limit=30
        )
        return session

    @app.get("/health/live", include_in_schema=False)
    def live() -> Mapping[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready(response: Response) -> Mapping[str, str]:
        if not container.ready():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ready"}

    @app.get("/pilot", response_class=HTMLResponse, include_in_schema=False)
    def pilot_page() -> str:
        return PILOT_HTML

    @app.get("/pilot/style.css", response_class=PlainTextResponse, include_in_schema=False)
    def pilot_css() -> Response:
        return Response(PILOT_CSS, media_type="text/css; charset=utf-8")

    @app.get("/pilot/app.js", response_class=PlainTextResponse, include_in_schema=False)
    def pilot_js() -> Response:
        return Response(PILOT_JS, media_type="text/javascript; charset=utf-8")

    @app.post("/v1/pilot/auth/session")
    def exchange_invite(request: InviteExchangeRequest, response: Response) -> Mapping[str, Any]:
        container.application.enforce_rate_limit(
            principal_key=hashlib.sha256(b"anonymous-invite-exchange").hexdigest(),
            route_key="invite_exchange_global",
            limit=30,
        )
        container.application.enforce_rate_limit(
            principal_key=hashlib.sha256(request.invite_token.encode()).hexdigest(),
            route_key="invite_exchange",
            limit=5,
        )
        result = container.application.exchange_invite(request.invite_token)
        session_token = str(result.pop("session_token"))
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            secure=container.secure_cookies,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=8 * 60 * 60,
        )
        response.set_cookie(
            CSRF_COOKIE,
            str(result["csrf_token"]),
            secure=container.secure_cookies,
            httponly=False,
            samesite="strict",
            path="/",
            max_age=8 * 60 * 60,
        )
        return result

    @app.delete("/v1/pilot/auth/session")
    def logout(
        response: Response,
        session=Depends(participant_write),
        session_token: str = Cookie(alias=SESSION_COOKIE),
    ) -> Mapping[str, str]:
        container.application.logout(session_token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return {"status": "logged_out"}

    @app.post("/v1/pilot/consent")
    def consent(request: ConsentRequest, session=Depends(participant_write)):
        return container.application.record_consent(session, request.model_dump())

    @app.get("/v1/pilot/state")
    def state_endpoint(session=Depends(participant)):
        return container.application.state(session)

    @app.post("/v1/pilot/attempts/{attempt_id}/reveal")
    def reveal(attempt_id: str, session=Depends(participant_write)):
        return container.application.reveal(session, attempt_id)

    @app.get("/v1/pilot/attempts/{attempt_id}")
    def attempt_status(attempt_id: str, session=Depends(participant)):
        return container.application.attempt_status(session, attempt_id)

    @app.post("/v1/pilot/attempts/{attempt_id}/exclude")
    def exclude_failed_attempt(attempt_id: str, session=Depends(participant_write)):
        return container.application.exclude_failed_attempt(session, attempt_id)

    @app.post("/v1/pilot/attempts/{attempt_id}/skip")
    def skip_attempt(attempt_id: str, session=Depends(participant_write)):
        return container.application.skip_attempt(session, attempt_id)

    @app.post("/v1/pilot/attempts/{attempt_id}/feedback")
    def feedback(
        attempt_id: str,
        request: FeedbackRequest,
        session=Depends(participant_write),
    ):
        payload = request.model_dump()
        if payload["clarification_useful"] is None:
            payload.pop("clarification_useful")
        return container.application.record_feedback(session, attempt_id, payload)

    @app.post("/v1/pilot/withdraw")
    def withdraw(
        response: Response,
        session=Depends(participant_write),
        session_token: str = Cookie(alias=SESSION_COOKIE),
    ):
        result = container.application.withdraw(session, session_token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return result

    @app.post("/v1/admin/campaigns", dependencies=[Depends(admin_auth)])
    def create_campaign(request: CampaignRequest):
        return container.application.create_campaign(request.model_dump())

    @app.post(
        "/v1/admin/campaigns/{campaign_id}/freeze",
        dependencies=[Depends(admin_auth)],
    )
    def freeze_campaign(campaign_id: str):
        return container.application.freeze_campaign(campaign_id)

    @app.post(
        "/v1/admin/campaigns/{campaign_id}/invites",
        dependencies=[Depends(admin_auth)],
    )
    def create_invite(campaign_id: str, request: InviteRequest):
        return container.application.create_invite(campaign_id, ttl_hours=request.ttl_hours)

    @app.post(
        "/v1/admin/campaigns/{campaign_id}/complete",
        dependencies=[Depends(admin_auth)],
    )
    def complete_campaign(campaign_id: str):
        return container.application.complete_campaign(campaign_id)

    @app.post(
        "/v1/admin/campaigns/{campaign_id}/incidents/{incident_id}/resolve",
        dependencies=[Depends(admin_auth)],
    )
    def resolve_incident(
        campaign_id: str, incident_id: str, request: IncidentResolutionRequest
    ):
        return container.application.resolve_incident(
            campaign_id, incident_id, resolution=request.resolution
        )

    @app.get(
        "/v1/admin/campaigns/{campaign_id}/summary",
        dependencies=[Depends(admin_auth)],
    )
    def campaign_summary(campaign_id: str):
        return container.application.summary(campaign_id)

    @app.get(
        "/v1/admin/campaigns/{campaign_id}/incidents",
        dependencies=[Depends(admin_auth)],
    )
    def list_incidents(campaign_id: str):
        return container.application.list_incidents(campaign_id)

    return app
