# Provider Poller

Azure Functions app that polls multiple provider status sources every minute, updates the latest in-memory status snapshot, and syncs selected providers to Instatus.

## Where it is used

Use this function app when you want a single polling service for provider health checks and a public status endpoint for other apps to read.

## Entry points

- `poll_providers_timer`: runs every minute and refreshes provider status
- `GET /status`: returns the latest cached provider status
- `GET /diagnostic`: forces a live poll and returns the current results

## Environment file

Put your local values in `.env` in this folder. Do not commit that file.

Create it from `.env.example` if you want a shareable template for GitHub.

Expected variables include:

- `ENVIRONMENT`
- `GRAFANA_URL`
- `GRAFANA_TOKEN`
- `GRAFANA_PROM_UID`
- `INSTATUS_API_KEY`
- `INSTATUS_PAGE_ID`
- `INSTATUS_BASE`
- `INSTATUS_COMP_*`
- `INSTATUS_SVC_*`

## Local run

```bash
func start
```

## Notes

- The timer trigger runs every minute.
- The status endpoint is public.
- The diagnostic endpoint requires function-level access.