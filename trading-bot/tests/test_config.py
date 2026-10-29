import asyncio
from pathlib import Path

import pytest

from tradebot.config import ConfigError, build_engine

DEMO = Path(__file__).parent.parent / "config" / "demo.toml"


def test_demo_config_builds_and_runs(no_kill_file):
    engine = build_engine(DEMO)
    assert len(engine.strategies) == 5
    snap = asyncio.run(engine.snapshot())
    assert len(snap.books) == 10
    asyncio.run(engine.tick())


def test_live_requires_flag_and_config_agreement(tmp_path):
    cfg = DEMO.read_text()
    with pytest.raises(ConfigError, match="BOTH"):
        build_engine(DEMO, live=True)
    live_cfg = tmp_path / "live.toml"
    live_cfg.write_text(cfg.replace('mode = "paper"', 'mode = "live"'))
    with pytest.raises(ConfigError, match="BOTH"):
        build_engine(live_cfg, live=False)
    with pytest.raises(ConfigError, match="synthetic venues cannot trade live"):
        build_engine(live_cfg, live=True)


def test_unknown_strategy_option_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(DEMO.read_text().replace("min_edge_bps = 5\nmax_size = 0.05", "min_edge_bps = 5\nmax_sise = 0.05"))
    with pytest.raises(ConfigError, match="max_sise"):
        build_engine(bad)
