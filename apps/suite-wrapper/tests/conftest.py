"""Shared fixtures for the Studio Suite pytest suite.

This suite is what used to be one 700-line function (main.py's old
selftest()) -- see VENV_CONSOLIDATION_PLAN.md's sibling doc history for
context, or just `git log` if this ever becomes a git repo. It's split by
subject into the test_*.py files alongside this conftest, each covering
one CONTRACT.md addendum area, so a single broken feature fails its own
test instead of aborting one giant assertion chain partway through.
"""

import os

import pytest

from backend import favorites, paths
from backend import cardeater_volume_watcher
from backend.suite_api import SuiteApi


@pytest.fixture
def api(monkeypatch, tmp_path):
    """A fresh SuiteApi() per test, with FAVORITES_FILE redirected to a
    tmp_path location BEFORE construction -- SuiteApi.__init__ calls
    favorites.load() immediately, and without this redirect that would
    read (and any save would write) the real user's favorites.json on
    this machine. Function-scoped deliberately: several tests mutate
    api.sources / api.favorites in place, and a fresh instance per test
    is simpler to reason about than resetting shared state by hand.

    CARDEATER_DB is redirected the same way -- SuiteApi.__init__ also
    builds a CardEaterState(paths.CARDEATER_DB) unconditionally, and
    without this redirect EVERY test using this fixture (not just
    Card-Eater-specific ones) would read/write the real
    assets/cardeater.sqlite3 on this machine (confirmed: it already had
    real rows in it from a plain `main.py --selftest` run before this
    fixture existed). cardeater_volume_watcher.start is also stubbed to a
    no-op here: CardEaterState.start_watcher() otherwise spawns a real
    daemon thread polling /Volumes every 1.5s per test, which serves no
    purpose in a unit test and only accumulates across the suite."""
    monkeypatch.setattr(paths, "FAVORITES_FILE",
                         str(tmp_path / "favorites.json"))
    monkeypatch.setattr(paths, "CARDEATER_DB",
                         str(tmp_path / "cardeater.sqlite3"))
    monkeypatch.setattr(cardeater_volume_watcher, "start", lambda registry: None)
    return SuiteApi()


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory stand-in for the `keyring` module's get/set/delete used
    by Blair Brander's Gemini-key storage (api_brander.py), so tests never
    touch the real OS keychain. monkeypatch restores the real functions
    automatically at teardown -- no manual save/restore needed."""
    import keyring

    store = {}

    def fake_get(service, key):
        return store.get((service, key))

    def fake_set(service, key, value):
        store[(service, key)] = value

    def fake_delete(service, key):
        if (service, key) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, key)]

    monkeypatch.setattr(keyring, "get_password", fake_get)
    monkeypatch.setattr(keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring, "delete_password", fake_delete)
    return store
