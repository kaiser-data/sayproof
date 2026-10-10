from types import SimpleNamespace

from fastapi.testclient import TestClient

from sayproof import app as app_module
from sayproof.speech import build_transcript


def tok(text, start):
    return SimpleNamespace(text=text, start=start)


def test_transcript_maps_characters_to_times():
    t = build_transcript([tok(" Um,", 0.0), tok(" Pluto", 0.6), tok(" is", 1.0), tok(" our", 1.2),
                          tok(" ninth", 1.4), tok(" planet.", 1.8), tok(" The", 4.2), tok(" Wall", 4.5)], 5.0)
    assert t.text == "Um, Pluto is our ninth planet. The Wall"
    assert t.time_at(t.text.index("Pluto")) == 0.6
    assert t.time_at(t.text.index("The")) == 4.2
    assert t.time_at(None) is None


def test_audio_endpoint_explains_when_parakeet_missing(monkeypatch):
    monkeypatch.setattr(app_module.speech, "available", lambda: False)
    r = TestClient(app_module.app).post("/api/check-audio", files={"audio": ("a.webm", b"xx", "audio/webm")})
    assert r.status_code == 501 and "Paste your text" in r.json()["detail"]
