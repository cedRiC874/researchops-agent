from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _read_token(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 24 or "\n" in value or "\r" in value:
        raise SystemExit("admin token file 无效。")
    return value


def _request(
    *, base: str, token: str, method: str, path: str, body: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    payload = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error_code", "http_error")
        except Exception:
            error = "http_error"
        raise SystemExit(f"Pilot admin request failed: {error}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Pilot admin API 不可用或响应无效。") from exc
    if not isinstance(result, dict):
        raise SystemExit("Pilot admin API 响应必须是 object。")
    return result


def _bootstrap(args, token: str) -> None:
    pack_path = Path(args.pack).resolve()
    root = Path(args.project_root).resolve()
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "title",
        "target_participants",
        "max_provider_runs",
        "candidate_commitment_sha256",
        "provider",
        "tasks",
    }
    if not isinstance(pack, dict) or set(pack) != expected:
        raise SystemExit("Pilot task pack 字段集合无效。")
    payload = {
        "title": pack["title"],
        "protocol_sha256": _sha256(root / "docs" / "EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md"),
        "consent_sha256": _sha256_text(
            root / "services" / "pilot_staging" / "content" / "consent.zh-CN.md"
        ),
        "feedback_schema_sha256": _sha256(
            root
            / "services"
            / "pilot_staging"
            / "contracts"
            / "task_feedback.schema.json"
        ),
        "dataset_manifest_sha256": _sha256(root / "evals" / "v2" / "external_datasets.json"),
        "deployment_git_sha": args.deployment_git_sha,
        "deployment_image_digest": args.deployment_image_digest,
        "candidate_commitment_sha256": pack["candidate_commitment_sha256"],
        "provider": pack["provider"],
        "target_participants": pack["target_participants"],
        "max_provider_runs": pack["max_provider_runs"],
        "tasks": pack["tasks"],
    }
    created = _request(
        base=args.api_base,
        token=token,
        method="POST",
        path="/v1/admin/campaigns",
        body=payload,
    )
    frozen = _request(
        base=args.api_base,
        token=token,
        method="POST",
        path=f"/v1/admin/campaigns/{created['campaign_id']}/freeze",
        body={},
    )
    print(json.dumps(frozen, ensure_ascii=False, indent=2, default=str))


def _invite(args, token: str) -> None:
    result = _request(
        base=args.api_base,
        token=token,
        method="POST",
        path=f"/v1/admin/campaigns/{args.campaign_id}/invites",
        body={"ttl_hours": args.ttl_hours},
    )
    invite_url = args.public_base.rstrip("/") + "/pilot#invite=" + result["invite_token"]
    print("一次性邀请链接（请单独发送给一名参与者）：")
    print(invite_url)
    print("此命令不会把邀请令牌写入文件。")


def _summary(args, token: str) -> None:
    result = _request(
        base=args.api_base,
        token=token,
        method="GET",
        path=f"/v1/admin/campaigns/{args.campaign_id}/summary",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _complete(args, token: str) -> None:
    result = _request(
        base=args.api_base,
        token=token,
        method="POST",
        path=f"/v1/admin/campaigns/{args.campaign_id}/complete",
        body={},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _resolve(args, token: str) -> None:
    result = _request(
        base=args.api_base,
        token=token,
        method="POST",
        path=(
            f"/v1/admin/campaigns/{args.campaign_id}/incidents/"
            f"{args.incident_id}/resolve"
        ),
        body={"resolution": args.resolution},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _incidents(args, token: str) -> None:
    result = _request(
        base=args.api_base,
        token=token,
        method="GET",
        path=f"/v1/admin/campaigns/{args.campaign_id}/incidents",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ResearchOps pilot staging operator CLI")
    parser.add_argument("--api-base", default="http://127.0.0.1:8090")
    parser.add_argument(
        "--admin-token-file",
        default="services/pilot_staging/secrets/admin_token.txt",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--project-root", default=".")
    bootstrap.add_argument(
        "--pack",
        default="services/pilot_staging/content/pilot_pack.public_v1.json",
    )
    bootstrap.add_argument("--deployment-git-sha")
    bootstrap.add_argument("--deployment-image-digest")
    bootstrap.set_defaults(handler=_bootstrap)

    invite = sub.add_parser("invite")
    invite.add_argument("--campaign-id", required=True)
    invite.add_argument("--ttl-hours", type=int, default=72)
    invite.add_argument("--public-base", required=True)
    invite.set_defaults(handler=_invite)

    summary = sub.add_parser("summary")
    summary.add_argument("--campaign-id", required=True)
    summary.set_defaults(handler=_summary)

    complete = sub.add_parser("complete")
    complete.add_argument("--campaign-id", required=True)
    complete.set_defaults(handler=_complete)

    resolve = sub.add_parser("resolve-incident")
    resolve.add_argument("--campaign-id", required=True)
    resolve.add_argument("--incident-id", required=True)
    resolve.add_argument("--resolution", choices=("dismissed", "confirmed"), required=True)
    resolve.set_defaults(handler=_resolve)

    incidents = sub.add_parser("incidents")
    incidents.add_argument("--campaign-id", required=True)
    incidents.set_defaults(handler=_incidents)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    token = _read_token(Path(args.admin_token_file))
    args.handler(args, token)


if __name__ == "__main__":
    main()
