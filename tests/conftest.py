import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))


def pytest_addoption(parser):
    parser.addoption("--live-web", action="store_true", default=False,
                     help="run tests that fetch a real public website (no credits, no CRM writes)")


def pytest_configure(config):
    config.addinivalue_line("markers", "live_web: hits a real public website; opt-in with --live-web")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live-web"):
        return
    skip = pytest.mark.skip(reason="needs --live-web")
    for item in items:
        if "live_web" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def repo():
    return REPO
