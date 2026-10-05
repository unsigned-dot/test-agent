"""Fixtures communes. Aucun test unitaire n'a besoin d'un vrai Tor."""

from __future__ import annotations

import pytest

from tor_api_client import tor
from tor_api_client.config import TorConfig


@pytest.fixture(autouse=True)
def _reset_tor_state():
    tor._reset_rotation_state()
    yield
    tor._reset_rotation_state()


@pytest.fixture
def config() -> TorConfig:
    return TorConfig.from_env(
        {
            "MAX_RETRIES": "3",
            "TOR_NEWNYM_WAIT": "10",
            "BACKOFF_BASE": "1",
            "BACKOFF_MAX": "30",
        }
    )

