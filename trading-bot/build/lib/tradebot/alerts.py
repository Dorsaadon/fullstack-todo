"""Alerts to your phone, and a dead-man's switch for when the bot itself dies.

Channels (any combination, configured in [alerts] with secrets from env vars):
  * Telegram: create a bot with @BotFather, message it once, then get your chat
    id from https://api.telegram.org/bot<token>/getUpdates
  * Slack: an incoming-webhook URL
  * Healthcheck (dead-man's switch, e.g. healthchecks.io): pinged every
    `healthcheck_interval_s` while the trading loop is alive. If pings stop
    (crash, hang, server down), the service alerts you; the bot can't, because
    it's the thing that died. A halt also pings `<url>/fail`.

Alerting never affects trading: messages queue and send in the background,
failures are logged and dropped, and identical messages are suppressed for
`dedupe_s`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

LEVELS = {"info": 10, "warning": 20, "critical": 30}
ICONS = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "summary": "📊"}


class Alerter:
    def __init__(
        self,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        slack_webhook: str | None = None,
        healthcheck_url: str | None = None,
        min_level: str = "warning",
        dedupe_s: float = 300.0,
        healthcheck_interval_s: float = 60.0,
        name: str = "tradebot",
        telegram_api: str = "https://api.telegram.org",
    ):
        if min_level not in LEVELS:
            raise ValueError(f"min_level must be one of {sorted(LEVELS)}")
        self.telegram_url = f"{telegram_api}/bot{telegram_token}/sendMessage" if telegram_token else None
        self.telegram_chat_id = telegram_chat_id
        self.slack_webhook = slack_webhook
        self.healthcheck_url = healthcheck_url.rstrip("/") if healthcheck_url else None
        self.min_level = LEVELS[min_level]
        self.dedupe_s = dedupe_s
        self.healthcheck_interval_s = healthcheck_interval_s
        self.name = name
        self.alive: Callable[[], bool] = lambda: True  # the engine sets this
        self.sent: list[str] = []  # delivered messages, for tests and `state` output
        self._queue: asyncio.Queue | None = None
        self._recent: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    @property
    def has_channels(self) -> bool:
        return bool(self.telegram_url and self.telegram_chat_id) or bool(self.slack_webhook)

    def send(self, level: str, text: str) -> None:
        """Queue an alert. `level` is info/warning/critical, or "summary" (always sent)."""
        log_level = {"critical": logging.CRITICAL, "warning": logging.WARNING}.get(level, logging.INFO)
        log.log(log_level, "alert: %s", text)
        if level != "summary" and LEVELS.get(level, 0) < self.min_level:
            return
        if not self.has_channels or self._queue is None:
            return
        now = time.monotonic()
        if now - self._recent.get(text, float("-inf")) < self.dedupe_s:
            return
        self._recent[text] = now
        message = f"{ICONS.get(level, '')} [{self.name}] {text}"
        try:
            self._queue.put_nowait(("message", message))
        except asyncio.QueueFull:
            log.warning("alert queue full; dropping: %s", text)

    def fail(self, reason: str) -> None:
        """Signal the dead-man's switch that something is wrong (e.g. a halt)."""
        if self.healthcheck_url and self._queue is not None:
            try:
                self._queue.put_nowait(("fail", reason))
            except asyncio.QueueFull:
                pass

    async def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=100)
        self._tasks = [asyncio.create_task(self._sender(), name="alerts")]
        if self.healthcheck_url:
            self._tasks.append(asyncio.create_task(self._pinger(), name="healthcheck"))

    async def close(self, timeout: float = 5.0) -> None:
        """Deliver what's queued (briefly), then stop."""
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout)
            except asyncio.TimeoutError:
                log.warning("gave up delivering %d alerts at shutdown", self._queue.qsize())
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _sender(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind == "fail":
                    await self._request(f"{self.healthcheck_url}/fail", payload.encode())
                else:
                    await self._deliver(payload)
            except Exception as e:  # never let alerting break anything
                log.warning("alert delivery failed: %s", e)
            finally:
                self._queue.task_done()

    async def _deliver(self, message: str) -> None:
        if self.telegram_url and self.telegram_chat_id:
            body = json.dumps({"chat_id": self.telegram_chat_id, "text": message}).encode()
            await self._request(self.telegram_url, body, "application/json")
        if self.slack_webhook:
            await self._request(self.slack_webhook, json.dumps({"text": message}).encode(), "application/json")
        self.sent.append(message)

    async def _pinger(self) -> None:
        while True:
            if self.alive():
                try:
                    await self._request(self.healthcheck_url)
                except Exception as e:
                    log.warning("healthcheck ping failed: %s", e)
            else:
                log.warning("trading loop looks stuck; withholding healthcheck ping")
            await asyncio.sleep(self.healthcheck_interval_s)

    @staticmethod
    async def _request(url: str, body: bytes | None = None, content_type: str | None = None) -> None:
        def go():
            req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
            if content_type:
                req.add_header("Content-Type", content_type)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()

        await asyncio.to_thread(go)
