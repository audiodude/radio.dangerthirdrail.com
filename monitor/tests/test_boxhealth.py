import os, sys, urllib.error
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boxhealth

class FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False

@pytest.mark.parametrize("payload", [b"null", b"[]", b"not-json"])
def test_probe_rejects_unusable_health_response(monkeypatch, payload):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", "tok")
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: FakeResp(payload))
    assert boxhealth.probe() is None

def test_probe_unreachable_returns_none(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", "tok")
    def boom(req, timeout=0): raise urllib.error.URLError("nope")
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", boom)
    assert boxhealth.probe() is None

@pytest.mark.parametrize("url,token", [("", "tok"), ("https://box/health", "")])
def test_missing_configuration_never_sends_commands(monkeypatch, url, token):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", url)
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", token)

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("missing credentials must fail closed before sending HTTP")

    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", unexpected_request)
    assert boxhealth.probe() is None
    assert boxhealth.release_opening("10000000-0000-4000-8000-000000000001") is False
    assert boxhealth.restart_opening("10000000-0000-4000-8000-000000000001") is False
