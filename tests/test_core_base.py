import importlib

import core.base


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"tag_name": "v1.5.2"}


def test_latest_version_request_has_timeout(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(core.base.requests, "get", fake_get)
    importlib.reload(core.base)

    assert calls == [(
        "https://api.github.com/repos/rachelos/we-mp-rss/releases/latest",
        {"timeout": 10},
    )]
    assert core.base.LATEST_VERSION == "1.5.2"
