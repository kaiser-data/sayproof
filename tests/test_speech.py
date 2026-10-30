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


def test_transcribe_endpoint_returns_text_and_times(monkeypatch):
    t = build_transcript([tok(" Pluto", 0.6), tok(" is", 1.0), tok(" our", 1.2), tok(" ninth", 1.4), tok(" planet.", 1.8)], 2.5)

    async def fake(path):
        return t

    monkeypatch.setattr(app_module.speech, "available", lambda: True)
    monkeypatch.setattr(app_module.speech, "transcribe", fake)
    r = TestClient(app_module.app).post("/api/transcribe", files={"audio": ("a.webm", b"xx", "audio/webm")})
    j = r.json()
    assert r.status_code == 200 and j["transcript"] == "Pluto is our ninth planet."
    assert j["audio_seconds"] == 2.5 and j["marks"][0] == [0, 0.6]


def test_phone_qr_points_at_this_computer(monkeypatch):
    monkeypatch.setattr(app_module.speech, "available", lambda: True)
    monkeypatch.setattr(app_module, "lan_address", lambda: "192.168.1.20")
    monkeypatch.setattr(app_module, "reachable", lambda host, port: True)
    j = TestClient(app_module.app).get("http://localhost:8765/api/phone").json()
    assert j["url"] == "http://192.168.1.20:8765/" and j["svg"].startswith("<svg")


def test_phone_qr_explains_when_server_only_listens_locally(monkeypatch):
    monkeypatch.setattr(app_module.speech, "available", lambda: True)
    monkeypatch.setattr(app_module, "reachable", lambda host, port: False)
    r = TestClient(app_module.app).get("/api/phone")
    assert r.status_code == 404 and "0.0.0.0" in r.json()["detail"]


def test_elevenlabs_words_map_characters_to_times():
    from sayproof.speech import transcript_from_words
    t = transcript_from_words([
        {"text": "Pluto", "type": "word", "start": 0.6, "end": 0.9},
        {"text": " ", "type": "spacing", "start": 0.9, "end": 1.0},
        {"text": "(laughs)", "type": "audio_event", "start": 1.0, "end": 1.5},
        {"text": "is", "type": "word", "start": 1.5, "end": 1.7},
        {"text": " ", "type": "spacing", "start": None, "end": None},
        {"text": "a planet.", "type": "word", "start": 1.8, "end": 2.4},
    ])
    assert t.text == "Pluto is a planet."
    assert t.time_at(t.text.index("is")) == 1.5 and t.time_at(t.text.index("a planet")) == 1.8
    assert t.duration == 2.4


def test_elevenlabs_used_when_parakeet_missing(monkeypatch):
    monkeypatch.setattr(app_module.speech, "_parakeet_installed", lambda: False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert app_module.speech.backend() is None
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert app_module.speech.backend() == "elevenlabs" and app_module.speech.name() == "ElevenLabs"
    assert TestClient(app_module.app).get("/api/health").json()["asr"] == "ElevenLabs"


def test_stored_recording_is_transcribed_from_a_signed_url(monkeypatch):
    t = build_transcript([tok(" Pluto", 0.6), tok(" is", 1.0), tok(" our", 1.2), tok(" ninth", 1.4), tok(" planet.", 1.8)], 2.5)
    seen = {}

    async def signed(path, expires_s=600):
        return f"https://store.example/{path}?token=t"

    async def fake(url):
        seen["url"] = url
        return t

    async def saved(path, text, model):
        seen["saved"] = (path, text)

    monkeypatch.setattr(app_module, "uploads_to_storage", lambda: True)
    monkeypatch.setattr(app_module.storage, "download_url", signed)
    monkeypatch.setattr(app_module.storage, "save_check", saved)
    monkeypatch.setattr(app_module.speech, "transcribe_url", fake)
    c = TestClient(app_module.app)
    path = "0123456789abcdef0123456789abcdef.m4a"
    j = c.post("/api/transcribe-stored", json={"path": path}).json()
    assert j["transcript"] == "Pluto is our ninth planet." and j["marks"][0] == [0, 0.6]
    assert seen == {"url": f"https://store.example/{path}?token=t", "saved": (path, "Pluto is our ninth planet.")}
    assert c.post("/api/transcribe-stored", json={"path": "../secrets.env"}).status_code == 400
