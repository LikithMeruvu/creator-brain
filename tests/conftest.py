import pytest


@pytest.fixture(autouse=True)
def _no_live_metadata_lookup(monkeypatch):
    """The fetch runner may look up a publish date over the network; tests never touch the network."""
    monkeypatch.setattr("cbrain.fetch.runner._lookup_meta", lambda video_id, proxy: None)
