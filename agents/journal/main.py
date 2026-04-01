"""Journal service — aiohttp web app on port 18798."""

import json
import logging
import sys

from aiohttp import web

from db import init_db, insert_entry, get_entries_by_date
from parser import parse_message

PORT = 18798
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("journal")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def log_entry(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    message = body.get("message", "").strip()
    user_id = body.get("user_id", "")

    if not message:
        raise web.HTTPBadRequest(reason="'message' field is required")

    try:
        entry, confirmation = parse_message(message)
    except ValueError as exc:
        raise web.HTTPUnprocessableEntity(reason=str(exc))

    # Store user_id in entities if provided
    if user_id:
        try:
            entities = json.loads(entry.get("entities") or "{}")
        except Exception:
            entities = {}
        entities["user_id"] = user_id
        entry["entities"] = json.dumps(entities)

    try:
        insert_entry(entry)
    except Exception as exc:
        log.exception("DB insert failed")
        raise web.HTTPInternalServerError(reason=f"DB error: {exc}")

    log.info("Logged entry id=%s type=%s", entry["id"], entry["entry_type"])
    return web.json_response({"confirmation": confirmation, "id": entry["id"]})


async def entries(request: web.Request) -> web.Response:
    date = request.rel_url.query.get("date", "")
    if not date:
        raise web.HTTPBadRequest(reason="'date' query parameter is required (YYYY-MM-DD)")
    rows = get_entries_by_date(date)
    return web.json_response({"date": date, "entries": rows})


# ---------------------------------------------------------------------------
# App factory + startup
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    init_db()
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/log", log_entry)
    app.router.add_get("/entries", entries)
    return app


if __name__ == "__main__":
    app = create_app()
    log.info("Journal service starting on port %d", PORT)
    web.run_app(app, host="127.0.0.1", port=PORT)
