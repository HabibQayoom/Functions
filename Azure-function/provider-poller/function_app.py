import azure.functions as func
import asyncio
import logging
import os
import time
from typing import Optional
from dataclasses import dataclass
import httpx
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import metrics

# ─────────────────────────────────────────────────────────────────────────────
# OpenTelemetry / Application Insights setup
# ─────────────────────────────────────────────────────────────────────────────
configure_azure_monitor()
meter = metrics.get_meter("aios.providers")

status_gauge = meter.create_gauge(
    name="provider_status",
    description="Provider status: 1.0=operational, 0.5=degraded, 0.0=major outage",
)
latency_gauge = meter.create_gauge(
    name="provider_status_check_latency_ms",
    description="Latency of the status check in milliseconds",
)

# ─────────────────────────────────────────────────────────────────────────────
# Instatus configuration
# ─────────────────────────────────────────────────────────────────────────────
INSTATUS_API_KEY = os.environ.get("INSTATUS_API_KEY", "")
INSTATUS_PAGE_ID = os.environ.get("INSTATUS_PAGE_ID", "")
TIMEOUT          = httpx.Timeout(10.0, connect=5.0)
INSTATUS_BASE    = os.environ.get("INSTATUS_BASE", "https://api.instatus.com/v2")

# Provider name → Instatus component ID (from env vars)
INSTATUS_COMPONENT_MAP = {
    "anthropic":  os.environ.get("INSTATUS_COMP_ANTHROPIC", "cmpmfrw1502wbqkja0ybhb51t"),
    "openai":     os.environ.get("INSTATUS_COMP_OPENAI",    "cmpmfrwbf0autnrm0aifcw7dz"),
    "gemini":     os.environ.get("INSTATUS_COMP_GEMINI",    "cmpmfrwk10ppxp2ml7364olql"),
    "deepgram":   os.environ.get("INSTATUS_COMP_DEEPGRAM",  "cmpmfrxif0pbfp2mepncl3bxu"),
    "vapi":       os.environ.get("INSTATUS_COMP_VAPI",      "cmpmubwl7178iqkp19n2hbnmg"),
    "composio":   os.environ.get("INSTATUS_COMP_COMPOSIO",  "cmpmubwvz006ip7jz5ed5cpo4"),
    "twilio":     os.environ.get("INSTATUS_COMP_TWILIO",    "cmpv0dg310541nn9xj9osnh7a"),
}

# AIOS service component IDs on Instatus (from env vars)
INSTATUS_SERVICE_MAP = {
    "aios-production":   os.environ.get("INSTATUS_SVC_AIOS_PRODUCTION", "cmpmhcqie03amqkjo6ko2ythv"),
    "backend-api-check": os.environ.get("INSTATUS_SVC_BACKEND_API",     "cmpmf9lah0ongp2m74a0tpurx"),
    "n8n-automations":   os.environ.get("INSTATUS_SVC_N8N",             "cmpmi43bs0bjnnrm084ms60lj"),
    "trieve-kb":         os.environ.get("INSTATUS_SVC_TRIEVE_KB",       "cmpmi1d6j14fqqkout623g04z"),
    "celery":            os.environ.get("INSTATUS_SVC_CELERY",          "cmpv0kjpw00xrqvmqxaatuwvw"),
}



# Score → Instatus status string
def score_to_instatus(score: float) -> str:
    if score >= 1.0:   return "OPERATIONAL"
    if score >= 0.75:  return "UNDERMAINTENANCE"
    if score >= 0.5:   return "DEGRADEDPERFORMANCE"
    if score >= 0.25:  return "PARTIALOUTAGE"
    return "MAJOROUTAGE"

# ─────────────────────────────────────────────────────────────────────────────
# Provider registry
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Provider:
    name: str
    method: str       # "statuspage" | "instatus" | "http_probe" | "gcp_incidents"
    url: str
    probe_url: Optional[str] = None

PROVIDERS = [
    # Atlassian Statuspage format
    Provider("anthropic",  "statuspage",    "https://status.anthropic.com/api/v2/summary.json"),
    Provider("openai",     "statuspage",    "https://status.openai.com/api/v2/summary.json"),
    Provider("sendgrid",   "statuspage",    "https://status.sendgrid.com/api/v2/summary.json"),
    Provider("deepgram",   "statuspage",    "https://status.deepgram.com/api/v2/summary.json"),

    # Instatus format (/index.json → .data.attributes.aggregate_state)
    Provider("vapi",       "instatus",      "https://status.vapi.ai/index.json"),
    Provider("cekura",     "instatus",      "https://status.cekura.ai/index.json"),
    Provider("browseruse", "instatus",      "https://status.browser-use.com/index.json"),

    # Google Cloud incidents feed
    Provider("gemini",     "gcp_incidents", "https://status.cloud.google.com/incidents.json"),

    # HTTP probe
    Provider("composio",   "http_probe",    "https://backend.composio.dev"),
    Provider("twilio",     "statuspage",    "https://status.twilio.com/api/v2/summary.json"),
]

# Score → Atlassian Statuspage indicator
STATUS_MAP = {
    "none":        1.0,
    "minor":       0.75,
    "major":       0.5,
    "critical":    0.25,
    "maintenance": 0.5,
}

# Instatus aggregate_state → score
INSTATUS_MAP = {
    "operational":          1.0,
    "degraded_performance": 0.75,
    "partial_outage":       0.5,
    "major_outage":         0.0,
    "under_maintenance":    0.5,
}

# Twilio — status checked via status page (no credentials needed)
# https://status.twilio.com/api/v2/summary.json

# ─────────────────────────────────────────────────────────────────────────────
# Check implementations
# ─────────────────────────────────────────────────────────────────────────────
async def check_statuspage(client: httpx.AsyncClient, p: Provider) -> tuple[float, str, float]:
    started = time.perf_counter()
    try:
        r = await client.get(p.url)
        r.raise_for_status()
        data = r.json()
        indicator = data.get("status", {}).get("indicator", "unknown")
        score = STATUS_MAP.get(indicator, 0.0)
        return score, indicator, (time.perf_counter() - started) * 1000
    except Exception as e:
        logging.warning(f"statuspage check failed for {p.name}: {e}")
        return 0.0, "unreachable", (time.perf_counter() - started) * 1000


async def check_instatus(client: httpx.AsyncClient, p: Provider) -> tuple[float, str, float]:
    started = time.perf_counter()
    try:
        r = await client.get(p.url)
        r.raise_for_status()
        data = r.json()
        state = (
            data.get("data", {})
                .get("attributes", {})
                .get("aggregate_state", "unknown")
        )
        score = INSTATUS_MAP.get(state, 0.5)
        return score, state, (time.perf_counter() - started) * 1000
    except Exception as e:
        logging.warning(f"instatus check failed for {p.name}: {e}")
        return 0.0, "unreachable", (time.perf_counter() - started) * 1000


async def check_http_probe(client: httpx.AsyncClient, p: Provider) -> tuple[float, str, float]:
    started = time.perf_counter()
    try:
        r = await client.get(p.url)
        latency_ms = (time.perf_counter() - started) * 1000
        if 200 <= r.status_code < 300:
            return 1.0, "operational", latency_ms
        return 0.5, f"http_{r.status_code}", latency_ms
    except Exception as e:
        logging.warning(f"http probe failed for {p.name}: {e}")
        return 0.0, "unreachable", (time.perf_counter() - started) * 1000


async def check_gcp_incidents(client: httpx.AsyncClient, p: Provider) -> tuple[float, str, float]:
    started = time.perf_counter()
    try:
        r = await client.get(p.url)
        r.raise_for_status()
        incidents = r.json()
        active = [
            i for i in incidents
            if not i.get("end") and any(
                s in str(i.get("affected_products", ""))
                for s in ["Vertex AI", "Gemini", "AI Platform"]
            )
        ]
        if not active:
            return 1.0, "none", (time.perf_counter() - started) * 1000
        severity = active[0].get("severity", "medium")
        score = {"low": 0.75, "medium": 0.5, "high": 0.25}.get(severity, 0.5)
        return score, severity, (time.perf_counter() - started) * 1000
    except Exception as e:
        logging.warning(f"gcp incidents check failed: {e}")
        return 0.0, "unreachable", (time.perf_counter() - started) * 1000


CHECK_MAP = {
    "statuspage":    check_statuspage,
    "instatus":      check_instatus,
    "http_probe":    check_http_probe,
    "gcp_incidents": check_gcp_incidents,
}

# ─────────────────────────────────────────────────────────────────────────────
# Instatus sync
# ─────────────────────────────────────────────────────────────────────────────

# Track previous scores to avoid unnecessary API calls
_prev_scores: dict[str, float] = {}

async def sync_to_instatus(
    client: httpx.AsyncClient,
    component_id: str,
    name: str,
    score: float,
) -> None:
    """Update Instatus component status only if it changed."""
    prev = _prev_scores.get(component_id)
    new_status = score_to_instatus(score)

    if prev is not None and score_to_instatus(prev) == new_status:
        return  # No change — skip API call

    try:
        r = await client.patch(
            f"{INSTATUS_BASE}/{INSTATUS_PAGE_ID}/components/{component_id}",
            headers={"Authorization": f"Bearer {INSTATUS_API_KEY}"},
            json={"status": new_status},
            timeout=8.0,
        )
        if r.status_code in (200, 201):
            _prev_scores[component_id] = score
            logging.info(f"instatus_sync: {name} → {new_status}")
        else:
            logging.warning(f"instatus_sync failed for {name}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        logging.warning(f"instatus_sync error for {name}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Polling
# ─────────────────────────────────────────────────────────────────────────────
async def poll_all() -> dict:
    """Poll every provider concurrently, emit metrics, sync to Instatus."""
    summary = {}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        # 1. Poll all providers concurrently
        tasks = [CHECK_MAP[p.method](client, p) for p in PROVIDERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 2. Process results + sync to Instatus
        instatus_tasks = []
        for p, result in zip(PROVIDERS, results):
            if isinstance(result, Exception):
                score, indicator, latency_ms = 0.0, "error", 0.0
                logging.error(f"unhandled exception for {p.name}: {result}")
            else:
                score, indicator, latency_ms = result

            # Emit to App Insights
            attrs = {"provider": p.name, "indicator": indicator, "method": p.method}
            status_gauge.set(score, attrs)
            latency_gauge.set(latency_ms, {"provider": p.name})
            summary[p.name] = {
                "score":      score,
                "indicator":  indicator,
                "latency_ms": round(latency_ms, 1),
                "instatus":   score_to_instatus(score),
            }

            # Queue Instatus sync if component mapped
            if p.name in INSTATUS_COMPONENT_MAP:
                instatus_tasks.append(
                    sync_to_instatus(client, INSTATUS_COMPONENT_MAP[p.name], p.name, score)
                )

        # 3. Check Celery/Flower health and sync to Instatus
        celery_score = await check_celery_health(client)
        status_gauge.set(celery_score, {"provider": "celery", "indicator": "flower", "method": "http_probe"})
        summary["celery"] = {
            "score":    celery_score,
            "instatus": score_to_instatus(celery_score),
        }
        if "celery" in INSTATUS_SERVICE_MAP:
            instatus_tasks.append(
                sync_to_instatus(client, INSTATUS_SERVICE_MAP["celery"], "AIOS Scheduling Platform", celery_score)
            )

        # 4. Sync all Instatus updates concurrently
        if instatus_tasks:
            await asyncio.gather(*instatus_tasks, return_exceptions=True)

    return summary


async def check_celery_health(client: httpx.AsyncClient) -> float:
    """
    Check Celery health via Grafana Prometheus API (queries flower_worker_online).
    Returns 1.0 if all workers online, 0.5 if some down, 0.0 if all down.
    Uses Grafana Cloud Prometheus — reachable from the Azure Function.
    """
    grafana_url  = os.environ.get("GRAFANA_URL", "https://iai.grafana.net")
    grafana_token = os.environ.get("GRAFANA_TOKEN", "")
    prom_uid     = os.environ.get("GRAFANA_PROM_UID", "grafanacloud-prom")

    query = 'sum(flower_worker_online{job="celery-flower"})'
    try:
        r = await client.post(
            f"{grafana_url}/api/ds/query",
            headers={
                "Authorization": f"Bearer {grafana_token}",
                "Content-Type": "application/json",
            },
            json={
                "queries": [{
                    "refId": "A",
                    "datasource": {"type": "prometheus", "uid": prom_uid},
                    "expr": query,
                    "instant": True,
                }],
                "from": "now-2m",
                "to": "now",
            },
            timeout=httpx.Timeout(10.0),
        )
        if r.status_code != 200:
            logging.warning(f"celery health grafana query failed: {r.status_code}")
            return 0.5

        frames = r.json().get("results", {}).get("A", {}).get("frames", [])
        if not frames:
            return 0.5

        vals = frames[0].get("data", {}).get("values", [])
        online = float(vals[1][0]) if len(vals) > 1 and vals[1] else 0.0

        # Expected workers: 8 workers + flower + redbeat = 10 deployments
        # Flower reports per-worker-process online count
        if online >= 8:
            return 1.0   # All workers online
        elif online >= 4:
            return 0.75  # Degraded — some workers down
        elif online > 0:
            return 0.5   # Major degradation
        return 0.0        # All workers offline

    except Exception as e:
        logging.warning(f"celery health check error: {e}")
        return 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Function App entry points
# ─────────────────────────────────────────────────────────────────────────────
app = func.FunctionApp()

# In-memory cache — updated every minute by the timer
_latest_status: dict = {}


@app.timer_trigger(
    schedule="0 */1 * * * *",
    arg_name="timer",
    run_on_startup=True,
    use_monitor=True,
)
async def poll_providers_timer(timer: func.TimerRequest) -> None:
    """Runs every minute. Polls all providers, emits metrics, syncs Instatus."""
    global _latest_status
    if timer.past_due:
        logging.warning("provider poll trigger is past due")
    summary = await poll_all()
    _latest_status = summary
    logging.info(
        "provider_poll_complete",
        extra={"custom_dimensions": {"summary": str(summary)}},
    )


@app.route(route="status", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
async def get_status(req: func.HttpRequest) -> func.HttpResponse:
    """
    Public endpoint — returns latest provider status.
    No auth needed. Updated every 60 seconds.

    Response:
    {
      "updated_at": "2026-06-09T08:00:00Z",
      "providers": {
        "anthropic":  {"score": 1.0, "status": "operational"},
        "openai":     {"score": 1.0, "status": "operational"},
        "twilio":     {"score": 0.75,"status": "minor"},
        ...
      }
    }
    Score: 1.0=operational, 0.75=minor, 0.5=degraded, 0.0=outage
    """
    import json
    from datetime import datetime, timezone

    STATUS_LABELS = {
        1.0:  "operational",
        0.75: "minor",
        0.5:  "degraded",
        0.0:  "outage",
    }

    def score_to_label(score):
        if score >= 1.0:  return "operational"
        if score >= 0.75: return "minor"
        if score > 0:     return "degraded"
        return "outage"

    providers = {}
    for provider, data in _latest_status.items():
        if isinstance(data, dict) and "score" in data:
            score = round(data["score"], 2)
            providers[provider] = {
                "score":  score,
                "status": score_to_label(score),
            }

    response = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers":  providers,
        "status_page": "https://aios.instatus.com",
    }

    return func.HttpResponse(
        json.dumps(response, indent=2),
        status_code=200,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},  # CORS — callable from any app
    )


@app.route(route="diagnostic", auth_level=func.AuthLevel.FUNCTION, methods=["GET"])
async def diagnostic(req: func.HttpRequest) -> func.HttpResponse:
    """Manual trigger — returns live provider status + Instatus sync result."""
    import json
    summary = await poll_all()
    return func.HttpResponse(
        json.dumps(summary, indent=2),
        mimetype="application/json",
    )

