

## Date and Time Worker

Cloudflare Worker that returns the current date and time for a requested timezone.

### Where it is used

Use this worker from your apps, agents, or direct HTTP calls when you need a lightweight datetime endpoint.

### Files

- `datetime-worker.js`: worker logic
- `wrangler.toml`: deployment config and route
- `.env`: local values used by Wrangler
- `.env.example`: template showing which values to fill in

### How to use

GET requests:

```bash
GET /datetime
GET /datetime?timezone=Asia/Karachi
```

POST requests are also supported for agent/tool integrations.

### Local setup

1. Copy `.env.example` to `.env`.
2. Fill in the values for your Cloudflare account and domain.
3. Run:

```bash
wrangler dev
```

Then open:

```bash
http://localhost:8787/datetime?timezone=Asia/Karachi
```

### Deploy

```bash
wrangler deploy
```

### Supported Timezone Formats

| Format | Example |
|--------|---------|
| IANA name | `Asia/Karachi`, `Europe/London`, `America/New_York` |
| UTC | `UTC` |
| GMT offset | `GMT+5`, `GMT+05:00`, `GMT-8` |
| Numeric offset | `+05:00`, `-08:00`, `+5` |

---

## Example Calls

### UTC (default)
```
GET /datetime
GET /datetime?timezone=UTC
```

### Pakistan Standard Time (PKT = UTC+5)
```
GET /datetime?timezone=Asia/Karachi
GET /datetime?timezone=GMT+5
GET /datetime?timezone=+05:00
```

### UK (BST or GMT depending on DST)
```
GET /datetime?timezone=Europe/London
```

### US Eastern
```
GET /datetime?timezone=America/New_York
```

---

## Sample Response

```json
{
  "date": "2026-06-05",
  "day": "Friday",
  "year": "2026",
  "time_24": "19:23:45",
  "time_12": "07:23:45 pm",
  "timezone": "Asia/Karachi",
  "utc_offset": "+05:00",
  "utc_time": "2026-06-05T14:23:45.000Z"
}
```

---

## Test Commands

```bash
# Basic (UTC)
curl "https://<your-worker>.workers.dev/datetime"

# Pakistan time
curl "https://<your-worker>.workers.dev/datetime?timezone=Asia/Karachi"

# GMT shorthand
curl "https://<your-worker>.workers.dev/datetime?timezone=GMT+5"

# Numeric offset
curl "https://<your-worker>.workers.dev/datetime?timezone=%2B05:00"
# (Note: + must be URL-encoded as %2B in some shells)
```

---

## Using in Agents

Call the worker as a tool/function from your agent:

```
GET https://<your-worker>/datetime?timezone=Asia/Karachi
```

The response gives you everything Meezab's Python script produced — no Code Interpreter, no latency.

---

## Local Testing

```bash
wrangler dev
```
Then hit `http://localhost:8787/datetime?timezone=Asia/Karachi`

---

## Logs

```bash
wrangler tail
```
