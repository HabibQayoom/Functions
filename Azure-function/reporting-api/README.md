# Reporting API

Azure Functions HTTP API that accepts alert payloads, writes them to Application Insights, and optionally posts them to Slack.

## Where it is used

Use this function app as the central alert ingestion endpoint for systems like the provider poller or any other service that needs to send alert events.

## Entry points

- `POST /alert`: accepts an alert payload and records it
- `GET /diagnostic`: returns a small runtime diagnostic response

## Environment file

Put your local values in `.env` in this folder. Do not commit that file.

Create it from `.env.example` if you want a shareable template for GitHub.

Expected variables include:

- `ENVIRONMENT`
- `SLACK_BOT_TOKEN`
- `AIOS_API_KEY`
- `APPLICATIONINSIGHTS_CONNECTION_STRING`

## Local run

```bash
func start
```

## Notes

- `POST /alert` accepts anonymous access but still checks `X-API-Key` when the API key is configured.
- Slack posting is optional and only happens when `channel` is present.