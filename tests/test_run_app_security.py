"""run-app must bind Streamlit to localhost and disable telemetry."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_config_disables_telemetry_and_binds_localhost():
    cfg_path = REPO_ROOT / ".streamlit" / "config.toml"
    assert cfg_path.exists(), ".streamlit/config.toml must be committed"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["browser"]["gatherUsageStats"] is False
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["server"]["headless"] is True


def test_run_app_launch_line_pins_address_and_stats():
    script = (REPO_ROOT / "scripts" / "run-app").read_text(encoding="utf-8")
    assert "--server.address 127.0.0.1" in script
    assert "--browser.gatherUsageStats false" in script
    assert "--server.headless true" in script
