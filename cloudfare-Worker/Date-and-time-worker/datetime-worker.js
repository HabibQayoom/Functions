// Cloudflare Worker — Current Date/Time with optional Timezone support
//
// Endpoints:
//   GET  /datetime                          → UTC (default)
//   GET  /datetime?timezone=Asia/Karachi
//   GET  /datetime?timezone=GMT+5
//   POST /datetime                          → Vapi format with toolCallId
//   POST /datetime  { "timezone": "Asia/Karachi" }

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return corsResponse();
    }

    if (url.pathname !== "/datetime") {
      return jsonResponse({ error: "Not found. Use GET or POST /datetime" }, 404);
    }

    if (request.method !== "GET" && request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed. Use GET or POST." }, 405);
    }

    // ── Parse body (POST only) ──────────────────────────────────────────────
    let body = {};
    if (request.method === "POST") {
      try {
        body = await request.json();
      } catch {
        body = {};
      }
    }

    // ── Timezone param ──────────────────────────────────────────────────────
    // GET:  ?timezone=Asia/Karachi
    // POST: body.timezone OR body.message.toolCallList[0].function.parameters.timezone
    // Fix: "GMT 5" → "GMT+5" because + in URLs decodes to space
    let rawTz = "UTC";
    if (request.method === "POST") {
      const tzFromParams = body?.message?.toolCallList?.[0]?.function?.parameters?.timezone;
      const tzFromBody   = body?.timezone;
      rawTz = (tzFromParams || tzFromBody || "UTC").trim().replace(/\s+/g, "+");
    } else {
      rawTz = (url.searchParams.get("timezone") || "UTC").trim().replace(/\s+/g, "+");
    }

    // ── Extract toolCallId (Vapi requirement) ───────────────────────────────
    const toolCallId = body?.message?.toolCallList?.[0]?.id || null;

    const ianaTz = resolveTimezone(rawTz);

    if (!ianaTz) {
      const errMsg = `Invalid timezone: "${rawTz}". Use IANA names (e.g. Asia/Karachi, Europe/London) or offsets (e.g. GMT+5, +05:00).`;
      if (toolCallId) {
        return jsonResponse({ results: [{ toolCallId, result: errMsg }] });
      }
      return jsonResponse({ error: errMsg }, 400);
    }

    try {
      const now = new Date();

      const fmt = (opts) =>
        new Intl.DateTimeFormat("en-GB", { timeZone: ianaTz, ...opts }).format(now);

      // YYYY-MM-DD
      const date = fmt({ year: "numeric", month: "2-digit", day: "2-digit" })
        .split("/")
        .reverse()
        .join("-");

      const day     = fmt({ weekday: "long" });
      const month   = fmt({ month: "long" });
      const year    = fmt({ year: "numeric" });
      const time24  = fmt({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

      // 12hr with uppercase AM/PM
      const time12Raw = fmt({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true });
      const time12    = time12Raw.replace(/am|pm/i, (m) => m.toUpperCase());

      const utcOffset = getUtcOffset(now, ianaTz);

      // ── Plain string result for Vapi ──────────────────────────────────────
      // "Today is Monday June 8 2026, current time is 02:15 PM UTC"
      const dayNum = fmt({ day: "numeric" });
      const resultString = `Today is ${day} ${month} ${dayNum} ${year}, current time is ${time12} ${ianaTz}`;

      // ── POST → Vapi format ────────────────────────────────────────────────
      if (request.method === "POST" && toolCallId) {
        return jsonResponse({
          results: [
            {
              toolCallId,
              result: resultString,
            },
          ],
        });
      }

      // ── GET → standard JSON (for non-Vapi use) ────────────────────────────
      return jsonResponse({
        date,
        day,
        year,
        time_24: time24,
        time_12: time12,
        timezone: ianaTz,
        utc_offset: utcOffset,
        utc_time: now.toISOString(),
        result: resultString,
      });

    } catch (err) {
      return jsonResponse({ error: `Could not compute time: ${err.message}` }, 500);
    }
  },
};

// ── resolveTimezone ──────────────────────────────────────────────────────────
function resolveTimezone(raw) {
  if (isValidIana(raw)) return raw;

  // GMT+5 / GMT+05:00 / GMT-8
  const gmtMatch = raw.match(/^GMT([+-]\d{1,2}(?::\d{2})?)$/i);
  if (gmtMatch) {
    const iana = offsetToEtc(gmtMatch[1]);
    if (iana && isValidIana(iana)) return iana;
  }

  // +05:30 / -08:00 / +5
  const offsetMatch = raw.match(/^([+-])(\d{1,2})(?::(\d{2}))?$/);
  if (offsetMatch) {
    const sign    = offsetMatch[1];
    const hours   = offsetMatch[2];
    const minutes = offsetMatch[3] || "00";
    const iana    = offsetToEtc(`${sign}${hours}:${minutes}`);
    if (iana && isValidIana(iana)) return iana;
  }

  return null;
}

function offsetToEtc(offset) {
  const m = offset.match(/^([+-])(\d{1,2})(?::(\d{2}))?$/);
  if (!m) return null;
  const sign = m[1];
  const h    = parseInt(m[2], 10);
  const mins = parseInt(m[3] || "0", 10);

  if (mins !== 0) {
    const knownHalfHour = {
      "+05:30": "Asia/Kolkata",
      "+05:45": "Asia/Kathmandu",
      "+09:30": "Australia/Darwin",
      "+03:30": "Asia/Tehran",
      "+06:30": "Asia/Rangoon",
    };
    const key = `${sign}${String(h).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
    return knownHalfHour[key] || null;
  }

  // Etc/GMT sign is inverted: GMT+5 → Etc/GMT-5
  const etcSign = sign === "+" ? "-" : "+";
  return `Etc/GMT${etcSign}${h}`;
}

function isValidIana(tz) {
  try {
    Intl.DateTimeFormat(undefined, { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

function getUtcOffset(date, ianaTz) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ianaTz,
    timeZoneName: "shortOffset",
  }).formatToParts(date);

  const offsetPart = parts.find((p) => p.type === "timeZoneName")?.value || "UTC";
  if (offsetPart === "GMT" || offsetPart === "UTC") return "+00:00";

  const m = offsetPart.match(/GMT([+-]\d{1,2})(?::(\d{2}))?/);
  if (!m) return "+00:00";

  const sign = m[1][0];
  const h    = String(Math.abs(parseInt(m[1], 10))).padStart(2, "0");
  const mins = String(parseInt(m[2] || "0", 10)).padStart(2, "0");
  return `${sign}${h}:${mins}`;
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}

function corsResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}
