"""Live test against the real cluster from your config. Opt-in, because it submits (tiny) jobs:

    METACENTRUM_LIVE=1 uv run pytest -s tests/test_live.py           # batch only
    METACENTRUM_LIVE=interactive uv run pytest -s tests/test_live.py  # plus an interactive job

Requires an open connection (`metacentrum-mcp login`).
"""

import os

import pytest

from metacentrum_mcp.selftest import run

LIVE = os.environ.get("METACENTRUM_LIVE")
pytestmark = pytest.mark.skipif(not LIVE, reason="set METACENTRUM_LIVE=1 to run against the real cluster")


async def test_live_selftest():
    rep = await run(os.environ.get("METACENTRUM_MCP_CONFIG"), interactive=LIVE == "interactive", timeout_min=20)
    failed = [r for r in rep.rows if not r[1]]
    assert not failed, failed
