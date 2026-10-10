from fastapi.testclient import TestClient

from sayproof.app import RateLimiter, app


def test_rate_limiter_per_ip_window_and_daily_cap():
    lim = RateLimiter(per_ip=2, window_s=60, per_day=3)
    assert lim.check("a", now=1000) is None
    assert lim.check("a", now=1001) is None
    assert "wait" in lim.check("a", now=1002)
    assert lim.check("a", now=1070) is None          # window has passed
    assert "today's limit" in lim.check("b", now=1071)  # 3 checks used today


def test_check_rejects_long_text_before_calling_apis():
    r = TestClient(app).post("/api/check", json={"text": "x" * 3001})
    assert r.status_code == 422


def test_index_and_health_served():
    c = TestClient(app)
    assert "Sayproof" in c.get("/").text
    assert set(c.get("/api/health").json()) == {"nebius_key", "tavily_key", "model", "speech"}
