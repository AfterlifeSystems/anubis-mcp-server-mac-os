from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path_factory, monkeypatch) -> None:
    """Keep every test out of the developer's real ~/.config/neuralnexus-mcp.

    DaemonConfig.save() and Credentials.save_api_key() write to whatever
    NEURALNEXUS_MCP_CONFIG_DIR points at. Without this fixture a test run
    overwrites the real daemon config — that is how a nonexistent /tmp/watch
    folder ended up being announced to the avatar as a shared root.
    """
    monkeypatch.setenv(
        "NEURALNEXUS_MCP_CONFIG_DIR",
        str(tmp_path_factory.mktemp("neuralnexus-mcp-config")),
    )
