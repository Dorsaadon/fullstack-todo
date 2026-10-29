"""Build exchanges, strategies, risk and engine from a TOML config file."""

from __future__ import annotations

import dataclasses
import inspect
import os
import time
import tomllib
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path

from .alerts import Alerter
from .engine import Engine
from .exchanges.base import Exchange
from .exchanges.paper import PaperExchange
from .journal import Journal
from .market_hours import MarketCalendar, RollingSessionCalendar
from .recording import Recorder
from .state import StateStore
from .risk import RiskLimits, RiskManager
from .strategies import (
    CrossExchangeArbitrage,
    MeanReversion,
    OpeningRangeBreakout,
    PredictionMarketArbitrage,
    TriangularArbitrage,
)

STRATEGIES = {
    "cross_exchange_arb": CrossExchangeArbitrage,
    "prediction_market_arb": PredictionMarketArbitrage,
    "triangular_arb": TriangularArbitrage,
    "mean_reversion": MeanReversion,
    "opening_range_breakout": OpeningRangeBreakout,
}

DEFAULT_QUOTE = {"ccxt": "USDT", "alpaca": "USD", "polymarket": "USDC"}

VENUE_ONLY_KEYS = (
    "kind", "taker_fee", "paper_balances", "market_hours", "demo_session_minutes", "demo_gap_minutes",
    "allow_short", "short_initial_margin", "borrow_rate_annual",
)


class ConfigError(ValueError):
    pass


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; real environment variables take precedence."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env(cfg: dict, key: str, required: bool) -> str | None:
    var = cfg.get(key)
    if not var:
        return None
    value = os.environ.get(var)
    if required and not value:
        raise ConfigError(f"environment variable {var} is not set")
    return value


def _dec(d: dict | None) -> dict[str, Decimal]:
    return {k: Decimal(str(v)) for k, v in (d or {}).items()}


def _build_live_adapter(name: str, cfg: dict, live: bool, synthetic_market) -> Exchange:
    kind = cfg.get("kind")
    fee = Decimal(str(cfg.get("taker_fee", 0)))
    if kind == "synthetic":
        from .exchanges.synthetic import SyntheticExchange

        if synthetic_market is None:
            raise ConfigError("synthetic venues need a [synthetic] section")
        opts = {k: v for k, v in cfg.items() if k not in VENUE_ONLY_KEYS}
        return SyntheticExchange(name, synthetic_market, **opts)
    if kind == "ccxt":
        from .exchanges.ccxt_exchange import CcxtExchange

        return CcxtExchange(
            name,
            cfg["exchange_id"],
            api_key=_env(cfg, "api_key_env", live),
            secret=_env(cfg, "secret_env", live),
            password=_env(cfg, "password_env", False),
            sandbox=cfg.get("sandbox", False),
            taker_fee=fee,
            quote_currency=cfg.get("quote_currency", "USDT"),
        )
    if kind == "alpaca":
        from .exchanges.alpaca import AlpacaExchange

        # Alpaca needs keys even for market data; a paper-account key is fine.
        return AlpacaExchange(
            name, _env(cfg, "api_key_env", True), _env(cfg, "secret_env", True),
            paper=cfg.get("paper_account", True), feed=cfg.get("feed", "iex"), stream_url=cfg.get("stream_url"),
        )
    if kind == "polymarket":
        from .exchanges.polymarket import PolymarketExchange

        return PolymarketExchange(
            name,
            private_key=_env(cfg, "private_key_env", live),
            funder=_env(cfg, "funder_env", False),
            signature_type=cfg.get("signature_type"),
            taker_fee=fee,
            **({"ws_url": cfg["ws_url"]} if "ws_url" in cfg else {}),
        )
    raise ConfigError(f"venue {name}: unknown kind {kind!r}")


def _build_calendar(name: str, vcfg: dict, global_cfg: dict, clock=time.time) -> MarketCalendar | None:
    """Trading hours for a venue. Alpaca defaults to US equities; others are 24/7."""
    hours = vcfg.get("market_hours", "us_equities" if vcfg.get("kind") == "alpaca" else None)
    if hours is None:
        return None
    if hours == "us_equities":
        return MarketCalendar(global_cfg.get("extra_holidays", ()), global_cfg.get("extra_early_closes", ()), clock)
    if hours == "demo":
        return RollingSessionCalendar(vcfg.get("demo_session_minutes", 5.0), vcfg.get("demo_gap_minutes", 0.0), clock)
    raise ConfigError(f"venue {name}: market_hours must be 'us_equities' or 'demo'")


def _build_strategy(
    cfg: dict, fees: dict[str, Decimal], exchanges: dict[str, Exchange], calendars: dict[str, MarketCalendar]
):
    cfg = dict(cfg)
    kind = cfg.pop("kind", None)
    cls = STRATEGIES.get(kind)
    if cls is None:
        raise ConfigError(f"unknown strategy kind {kind!r}; choose from {sorted(STRATEGIES)}")
    cfg.setdefault("name", kind)
    params = inspect.signature(cls).parameters
    unknown = set(cfg) - set(params)
    if unknown:
        raise ConfigError(f"strategy {cfg['name']}: unknown options {sorted(unknown)}")
    if "quote_currency" in params and "quote_currency" not in cfg:
        venue = cfg["venues"][0] if "venues" in cfg else cfg["venue"]
        cfg["quote_currency"] = exchanges[venue].quote_currency
    if "calendar" in params:
        if "calendar" in cfg:
            raise ConfigError(f"strategy {cfg['name']}: set market hours on the venue, not the strategy")
        cfg["calendar"] = calendars.get(cfg["venue"]) or MarketCalendar()
    for key, p in params.items():
        if key in cfg and "Decimal" in str(p.annotation):
            cfg[key] = Decimal(str(cfg[key]))
    return cls(fees=fees, **cfg)


def _build_alerter(cfg: dict) -> Alerter:
    def env(key):
        var = cfg.get(key)
        return os.environ.get(var) if var else None

    token, chat = env("telegram_token_env"), env("telegram_chat_id_env")
    if bool(token) != bool(chat):
        raise ConfigError("[alerts] Telegram needs both the bot token and the chat id set")
    return Alerter(
        telegram_token=token,
        telegram_chat_id=chat,
        slack_webhook=env("slack_webhook_env"),
        healthcheck_url=env("healthcheck_url_env"),
        min_level=cfg.get("min_level", "warning"),
        dedupe_s=cfg.get("dedupe_s", 300.0),
        healthcheck_interval_s=cfg.get("healthcheck_interval_s", 60.0),
        name=cfg.get("name", "tradebot"),
    )


def build_engine(path: str | Path, live: bool = False, clock=None, offline: bool = False) -> Engine:
    """Build the engine from a config file.

    `offline=True` (backtests) builds paper venues with no data source, so no
    network, API keys or venue SDKs are needed, and everything that reads the
    time uses `clock`.
    """
    clock = clock or time.time
    cfg = tomllib.loads(Path(path).read_text())
    if offline and live:
        raise ConfigError("offline (backtest) engines can't trade live")
    eng_cfg = cfg.get("engine", {})
    mode = eng_cfg.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError("engine.mode must be 'paper' or 'live'")
    if live != (mode == "live"):
        raise ConfigError(
            "live trading needs BOTH engine.mode = 'live' in the config AND the --live flag"
            if live or mode == "live" else "inconsistent mode"
        )

    market = None
    if "synthetic" in cfg and not offline:
        from .exchanges.synthetic import SyntheticMarket

        s = cfg["synthetic"]
        market = SyntheticMarket(
            s.get("assets", {}), s.get("outcome_sets"), s.get("seed", 0), s.get("step_s", 1.0), s.get("vol_bps", 5.0)
        )

    exchanges: dict[str, Exchange] = {}
    calendars: dict[str, MarketCalendar] = {}
    short_venues: set[str] = set()
    for name, vcfg in cfg.get("venues", {}).items():
        if vcfg.get("allow_short", False):
            short_venues.add(name)
        cal = _build_calendar(name, vcfg, cfg.get("market_calendar", {}), clock)
        if cal is not None:
            calendars[name] = cal
        if offline:
            adapter = None
            quote = vcfg.get("quote_currency") or DEFAULT_QUOTE.get(vcfg.get("kind"), "USD")
        else:
            adapter = _build_live_adapter(name, vcfg, live, market)
            quote = adapter.quote_currency
        if live:
            if vcfg.get("kind") == "synthetic":
                raise ConfigError(f"venue {name}: synthetic venues cannot trade live")
            exchanges[name] = adapter
        else:
            exchanges[name] = PaperExchange(
                name,
                data_source=adapter,
                balances=_dec(vcfg.get("paper_balances")),
                taker_fee=Decimal(str(vcfg.get("taker_fee", 0))),
                quote_currency=quote,
                allow_short=vcfg.get("allow_short", False),
                short_initial_margin=Decimal(str(vcfg.get("short_initial_margin", "0.5"))),
                borrow_rate_annual=Decimal(str(vcfg.get("borrow_rate_annual", 0))),
                clock=clock,
            )
    if not exchanges:
        raise ConfigError("no [venues] configured")

    fees = {n: ex.taker_fee for n, ex in exchanges.items()}
    strategies = [_build_strategy(s, fees, exchanges, calendars) for s in cfg.get("strategies", [])]
    if not strategies:
        raise ConfigError("no [[strategies]] configured")
    names = [s.name for s in strategies]
    if len(set(names)) != len(names):
        raise ConfigError(f"strategy names must be unique (saved state is keyed by name): {names}")
    for s in strategies:
        if getattr(s, "allow_short", False) and s.venue not in short_venues:
            raise ConfigError(f"strategy {s.name} shorts, but venue {s.venue} lacks allow_short = true")

    r = cfg.get("risk", {})
    limits = RiskLimits(
        **{k: Decimal(str(v)) if isinstance(getattr(RiskLimits, k, None), Decimal) else v for k, v in r.items()}
    )
    if offline:
        limits = dataclasses.replace(limits, kill_switch_file=None)  # a stray KILL file shouldn't skew a backtest
    risk = RiskManager(
        limits, assets_of=lambda v, s: exchanges[v].assets(s), clock=clock, calendars=calendars,
        short_venues=short_venues,
    )
    rec = cfg.get("recording", {})
    recorder = None
    if rec.get("enabled", False) and not offline:
        recorder = Recorder(rec.get("path", "data/recordings"), rec.get("min_interval_s", 0.0))
    engine = Engine(
        exchanges,
        strategies,
        risk,
        journal=Journal(None if offline else eng_cfg.get("journal"), clock),
        poll_interval_s=eng_cfg.get("poll_interval_s", 2.0),
        book_depth=eng_cfg.get("book_depth", 10),
        unwind_slippage_bps=Decimal(str(eng_cfg.get("unwind_slippage_bps", 50))),
        data_mode=eng_cfg.get("data_mode", "stream"),
        heartbeat_s=eng_cfg.get("heartbeat_s", 1.0),
        min_tick_interval_s=eng_cfg.get("min_tick_interval_s", 0.05),
        balance_refresh_s=eng_cfg.get("balance_refresh_s", 30.0),
        warmup_timeout_s=eng_cfg.get("warmup_timeout_s", 15.0),
    )
    engine.recorder = recorder
    if not offline:
        engine.state = StateStore(eng_cfg.get("state_file", "data/state.json"))
        engine.state_save_s = eng_cfg.get("state_save_s", 10.0)
        alerts_cfg = cfg.get("alerts", {})
        engine.alerts = _build_alerter(alerts_cfg)
        if alerts_cfg.get("daily_summary_time"):
            engine.daily_summary_time = dtime.fromisoformat(alerts_cfg["daily_summary_time"])
    return engine
