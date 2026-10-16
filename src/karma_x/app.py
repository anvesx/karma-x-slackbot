"""Signed Slack ingress and a single-process durable delivery loop."""

import asyncio
import fcntl
import hashlib
import logging
import os
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier

from karma_x.config import Config
from karma_x.core import UserError, parse_command, utc_us
from karma_x.service import Service
from karma_x.slack import Slack
from karma_x.store import Store

log = logging.getLogger(__name__)


def create_app(config: Config, service: Service | None = None, run_worker: bool = True) -> FastAPI:
    store = service.store if service else Store(config.database)
    engine = service or Service(
        config, store, Slack(config.bot_token, config.profile_token), lambda: datetime.now(UTC)
    )
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                if not engine.tick():
                    stop.wait(0.25)
            except Exception:
                # Preserve jobs for recovery; log stack without request bodies or tokens.
                log.exception("Worker iteration failed")
                stop.wait(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        config.database.parent.mkdir(parents=True, exist_ok=True)
        lock = (config.database.parent / "worker.lock").open("a")
        thread: threading.Thread | None = None
        try:
            if run_worker:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                store.recover_sends()
                thread = threading.Thread(target=worker, name="karma-x-worker", daemon=True)
                thread.start()
            yield
        finally:
            stop.set()
            if thread:
                thread.join(timeout=20)
            lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    verifier = SignatureVerifier(config.signing_secret)
    admission = asyncio.Semaphore(8)

    def release_admission(task: asyncio.Task[bool]) -> None:
        admission.release()
        if not task.cancelled():
            task.exception()  # Retrieve errors even if the HTTP caller disconnected.

    @app.get("/health")
    def health() -> JSONResponse:
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "bot": "karma-x"})

    @app.post("/slack/commands")
    async def commands(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        valid_timestamp = 1 <= len(timestamp) <= 12 and timestamp.isascii() and timestamp.isdigit()
        if not valid_timestamp or not verifier.is_valid_request(bytes(body), dict(request.headers)):
            return JSONResponse({"error": "invalid_signature"}, status_code=401)
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            return JSONResponse({"error": "invalid_content_type"}, status_code=400)
        try:
            parsed = parse_qs(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=30
            )
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError("duplicate_fields")
            p = {k: v[0] for k, v in parsed.items()}
            for field in ("team_id", "user_id", "channel_id", "text", "response_url", "command"):
                if field not in p:
                    raise ValueError("missing_fields")
            if p["team_id"] != config.team_id or not config.allows_channel(p["channel_id"]):
                return JSONResponse(
                    {
                        "response_type": "ephemeral",
                        "text": "This workspace or channel is not enabled for karma-x.",
                    }
                )
            if p["command"] != "/karma-x":
                raise ValueError("wrong_command")
            command = parse_command(p["text"])
            if command.action == "give" and command.target == p["user_id"]:
                raise UserError("You cannot give yourself karma.")
            key = hashlib.sha256(
                request.headers["x-slack-request-timestamp"].encode() + b":" + body
            ).hexdigest()
            # Never block the event loop on SQLite's busy timeout. Bound both admission
            # and active writes so contention returns before Slack's 3-second deadline.
            try:
                await asyncio.wait_for(admission.acquire(), timeout=0.05)
            except TimeoutError:
                return JSONResponse({"error": "database_busy"}, status_code=503)
            pending = asyncio.create_task(
                asyncio.to_thread(store.enqueue, key, p, utc_us(datetime.now(UTC)))
            )
            pending.add_done_callback(release_admission)
            # A disconnected caller may leave a committed job; signed retries deduplicate it.
            accepted = await asyncio.shield(pending)
        except (ValueError, UnicodeError) as e:
            if isinstance(e, UserError):
                return JSONResponse({"response_type": "ephemeral", "text": str(e)})
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        except sqlite3.OperationalError:
            # Slack may safely retry; no acknowledgment until a durable commit exists.
            return JSONResponse({"error": "database_busy"}, status_code=503)
        return JSONResponse(
            {
                "response_type": "ephemeral",
                "text": "Processing…" if accepted else "This request was already accepted.",
            }
        )

    return app


def main() -> None:
    import uvicorn

    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # HTTPX's INFO request logs contain the credential-bearing Slack response URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    uvicorn.run(
        create_app(Config.from_env()),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8790")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
