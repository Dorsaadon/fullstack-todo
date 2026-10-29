"""Build exchanges, strategies, risk and engine from a TOML config file."""

from __future__ import annotations

import inspect
import os
import tomllib
from decimal import Decimal
from pathlib import Path

from .engine import Engine
from .exchanges.base import Exchange
from .exchanges.paper import PaperExchange
from .journal import Journal
from .risk import RiskLimits, RiskManager
from .strategies import CrossExchangeArbitrage, MeanReversion, PredictionMarketArbitrage, TriangularArbitrage

STRATEGIES = {
    "cross_exchange_arb": CrossExchangeArbitrage,
    "prediction_market_arb": PredictionMarketArbitrage,
    "triangular_arb": TriangularArbitrage,
    "mean_reversion": MeanReversion,
}


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
        opts = {k: v for k, v in cfg.items() if k not in ("kind", "taker_fee", "paper_balances")}
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
            paper=cfg.get("paper_account", True), feed=cfg.get("feed", "iex"),
        )
    if kind == "polymarket":
        from .exchanges.polymarket import PolymarketExchange

        return PolymarketExchange(
            name,
            private_key=_env(cfg, "private_key_env", live),
            funder=_env(cfg, "funder_env", False),
            signature_type=cfg.get("signature_type"),
            taker_fee=fee,
        )
    raise ConfigError(f"venue {name}: unknown kind {kind!r}")


def _build_strategy(cfg: dict, fees: dict[str, Decimal], exchanges: dict[str, Exchange]):
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
    if cls is CrossExchangeArbitrage and "quote_currency" not in cfg:
        cfg["quote_currency"] = exchanges[cfg["venues"][0]].quote_currency
    for key, p in params.items():
        if key in cfg and "Decimal" in str(p.annotation):
            cfg[key] = Decimal(str(cfg[key]))
    return cls(fees=fees, **cfg)


def build_engine(path: str | Path, live: bool = False) -> Engine:
    cfg = tomllib.loads(Path(path).read_text())
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
    if "synthetic" in cfg:
        from .exchanges.synthetic import SyntheticMarket

        s = cfg["synthetic"]
        market = SyntheticMarket(
            s.get("assets", {}), s.get("outcome_sets"), s.get("seed", 0), s.get("step_s", 1.0), s.get("vol_bps", 5.0)
        )

    exchanges: dict[str, Exchange] = {}
    for name, vcfg in cfg.get("venues", {}).items():
        adapter = _build_live_adapter(name, vcfg, live, market)
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
                quote_currency=adapter.quote_currency,
            )
    if not exchanges:
        raise ConfigError("no [venues] configured")

    fees = {n: ex.taker_fee for n, ex in exchanges.items()}
    strategies = [_build_strategy(s, fees, exchanges) for s in cfg.get("strategies", [])]
    if not strategies:
        raise ConfigError("no [[strategies]] configured")

    r = cfg.get("risk", {})
    limits = RiskLimits(
        **{k: Decimal(str(v)) if isinstance(getattr(RiskLimits, k, None), Decimal) else v for k, v in r.items()}
    )
    risk = RiskManager(limits, assets_of=lambda v, s: exchanges[v].assets(s))
    return Engine(
        exchanges,
        strategies,
        risk,
        journal=Journal(eng_cfg.get("journal")),
        poll_interval_s=eng_cfg.get("poll_interval_s", 2.0),
        book_depth=eng_cfg.get("book_depth", 10),
        unwind_slippage_bps=Decimal(str(eng_cfg.get("unwind_slippage_bps", 50))),
    )
