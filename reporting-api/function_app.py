import azure.functions as func
import asyncio
import json
import logging
import os
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from azure.monitor.opentelemetry import configure_azure_monitor
from pydantic import BaseModel, Field, field_validator

configure_azure_monitor()

ENVIRONMENT     = os.environ.get("ENVIRONMENT", "production")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
AIOS_API_KEY    = os.environ.get("AIOS_API_KEY", "")

ALLOWED_SYSTEMS = {
    "aios-backend", "aios-frontend", "internal-n8n",
    "cloudflare-worker", "provider-poller",
}

# Cache for Slack channel name → ID
_slack_channel_cache: dict = {}


class Alert(BaseModel):
    system_name: Literal[
        "aios-backend", "aios-frontend", "internal-n8n",
        "cloudflare-worker", "provider-poller",
    ]
    severity: Literal["info", "warning", "critical"]
    message: str = Field(min_length=1, max_length=2000)
    channel: Optional[str] = Field(default=None, max_length=100)
    org_id: Optional[str] = Field(default=None, max_length=100)
    agent_id: Optional[str] = Field(default=None, max_length=100)
    trace_id: Optional[str] = Field(default=None, max_length=100)
    timestamp: Optional[str] = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        if v is None:
            return None
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
        except ValueError:
            raise ValueError("timestamp must be ISO 8601")


def authenticate(api_key: str) -> bool:
    """Validate against AIOS_API_KEY using constant-time comparison."""
    if not api_key:
        return False
    if not AIOS_API_KEY:
        return True  # No key configured — accept any value
    if len(api_key) != len(AIOS_API_KEY):
        return False
    result = 0
    for x, y in zip(api_key.encode(), AIOS_API_KEY.encode()):
        result |= x ^ y
    return result == 0


def emit_to_app_insights(alert: Alert, request_id: str) -> None:
    """Write to App Insights REST API directly — proper top-level customDimensions."""
    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    ikey, ingestion_url = "", "https://uksouth-1.in.applicationinsights.azure.com/v2/track"
    for part in conn_str.split(";"):
        if part.startswith("InstrumentationKey="):
            ikey = part[len("InstrumentationKey="):]
        if part.startswith("IngestionEndpoint="):
            ingestion_url = part[len("IngestionEndpoint="):].rstrip("/") + "/v2/track"

    payload = json.dumps([{
        "name": "Microsoft.ApplicationInsights.Message",
        "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "iKey": ikey,
        "data": {
            "baseType": "MessageData",
            "baseData": {
                "ver": 2,
                "message": "aios_alert",
                "severityLevel": 2 if alert.severity == "critical" else 1,
                "properties": {
                    "event_type":  "aios_alert",
                    "request_id":  request_id,
                    "system_name": alert.system_name,
                    "severity":    alert.severity,
                    "message":     alert.message[:500],
                    "channel":     alert.channel or "",
                    "org_id":      alert.org_id or "",
                    "agent_id":    alert.agent_id or "",
                    "trace_id":    alert.trace_id or "",
                    "environment": ENVIRONMENT,
                }
            }
        }
    }]).encode()
    try:
        req = urllib.request.Request(ingestion_url, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        logging.info(f"aios_alert emitted: {alert.system_name} {alert.severity}")
    except Exception as e:
        logging.warning(f"app_insights emit failed: {e}")


async def resolve_slack_channel(name: str, client: httpx.AsyncClient) -> str:
    clean = name.lstrip("#")
    if clean in _slack_channel_cache:
        return _slack_channel_cache[clean]
    try:
        r = await client.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"limit": 200, "types": "public_channel,private_channel"},
            timeout=5.0,
        )
        data = r.json()
        if data.get("ok"):
            for ch in data.get("channels", []):
                _slack_channel_cache[ch["name"]] = ch["id"]
    except Exception as e:
        logging.warning(f"channel_resolve error: {e}")
    return _slack_channel_cache.get(clean, name)


async def post_to_slack(alert: Alert, request_id: str, client: httpx.AsyncClient) -> None:
    if not alert.channel or not SLACK_BOT_TOKEN:
        return
    channel = await resolve_slack_channel(alert.channel, client)
    emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(alert.severity, "⚪")
    lines = [
        f"{emoji} *[{alert.severity.upper()}]* `{alert.system_name}`",
        f"*Message:* {alert.message}",
    ]
    if alert.org_id:
        lines.append(f"*Org:* `{alert.org_id}`")
    if alert.agent_id:
        lines.append(f"*Agent:* `{alert.agent_id}`")
    if alert.trace_id:
        lines.append(f"*Trace:* `{alert.trace_id}`")
    if alert.metadata:
        for k, v in alert.metadata.items():
            lines.append(f"*{k}:* `{v}`")
    lines.append(f"*Request ID:* `{request_id}`")
    text = "\n".join(lines)
    try:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
            json={"channel": channel, "text": text, "mrkdwn": True},
            timeout=5.0,
        )
        resp = r.json()
        if resp.get("ok"):
            logging.info(f"slack_post ok: {channel}")
        else:
            logging.warning(f"slack_post failed: {resp.get('error')} channel={channel}")
    except Exception as e:
        logging.warning(f"slack_post error: {e}")


app = func.FunctionApp()


@app.route(route="alert", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
async def receive_alert(req: func.HttpRequest) -> func.HttpResponse:
    request_id = str(uuid.uuid4())
    try:
        body = req.get_json()
    except ValueError:
        return _err(400, "invalid_json", request_id)
    try:
        alert = Alert.model_validate(body)
    except Exception as e:
        return _err(400, f"validation_error: {e}", request_id)
    if not authenticate(req.headers.get("X-API-Key", "")):
        return _err(401, "unauthorized", request_id)
    if not alert.timestamp:
        alert.timestamp = datetime.now(timezone.utc).isoformat()

    # Write to App Insights
    emit_to_app_insights(alert, request_id)

    # Post to Slack if channel provided
    try:
        async with httpx.AsyncClient() as client:
            await post_to_slack(alert, request_id, client)
    except Exception as e:
        logging.warning(f"slack error (non-fatal): {e}")

    return func.HttpResponse(
        json.dumps({"status": "accepted", "request_id": request_id, "timestamp": alert.timestamp}),
        status_code=202, mimetype="application/json",
    )


@app.route(route="diagnostic", auth_level=func.AuthLevel.FUNCTION, methods=["GET"])
async def diagnostic(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({
        "environment":     ENVIRONMENT,
        "slack_enabled":   bool(SLACK_BOT_TOKEN),
        "allowed_systems": list(ALLOWED_SYSTEMS),
    }, indent=2), mimetype="application/json")


def _err(code, msg, request_id):
    return func.HttpResponse(
        json.dumps({"error": msg, "request_id": request_id}),
        status_code=code, mimetype="application/json",
    )
