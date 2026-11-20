"""YouTube Analytics per short: Shorts-feed, kijkers na 3 s, gemiddeld bekeken en diagnose."""
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core import crypto
from app.core.db import new_session
from app.core.models import Clip, YoutubeAccount, utcnow
from app.estimation import analytics, insights
from app.integrations import youtube_upload as yt
from tests.conftest import make_project_with_clip, user_id


class FakeAnalytics:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request):
        url = str(request.url)
        if url.startswith(analytics.REPORTS):
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.calls.append(q)
            assert request.headers["authorization"] == "Bearer tok" and q["ids"] == "channel==MINE"
            if q.get("dimensions") == "video":
                ids = q["filters"].split("==", 1)[1].split(",")
                assert sorted(ids) == ["VIDEOAAAAAA", "VIDEOBBBBBB"]
                return httpx.Response(200, json={"rows": [["VIDEOAAAAAA", 500, 12.0, 85.0], ["VIDEOBBBBBB", 300, 5.0, 40.0]]})
            vid = q["filters"].split("==", 1)[1]
            if q["dimensions"] == "insightTrafficSourceType":
                rows = [["SHORTS", 450], ["YT_CHANNEL", 50]] if vid == "VIDEOAAAAAA" else [["SHORTS", 30], ["YT_SEARCH", 270]]
                return httpx.Response(200, json={"rows": rows})
            if q["dimensions"] == "elapsedVideoTimeRatio":
                hi = 0.9 if vid == "VIDEOAAAAAA" else 0.4
                return httpx.Response(200, json={"rows": [[r / 100, hi if r < 50 else hi - 0.2] for r in range(1, 101)]})
        if url.startswith(f"{yt.API}/videos"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404)


@pytest.fixture
def fa(monkeypatch):
    f = FakeAnalytics()
    monkeypatch.setattr(yt, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    monkeypatch.setattr(insights, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    return f


def _account(scopes: str):
    db = new_session()
    db.add(YoutubeAccount(user_id=user_id("admin"), channel_id="UC1", channel_title="K", scopes=scopes,
                          refresh_token_enc=crypto.encrypt("r"), access_token_enc=crypto.encrypt("tok"),
                          expires_at=utcnow() + timedelta(hours=1)))
    db.commit()
    db.close()


def test_analytics_sync_and_diagnosis(admin_client, settings, fa):
    pid, cids = make_project_with_clip(settings, "admin", 2)
    db = new_session()
    for cid, vid in zip(cids, ["VIDEOAAAAAA", "VIDEOBBBBBB"]):
        c = db.get(Clip, cid)
        c.posted_at, c.yt_video_id, c.actual_views = utcnow() - timedelta(days=2), vid, 400
        c.end = 20.0
    db.commit()
    db.close()
    _account(yt.SCOPES)
    db = new_session()
    assert analytics.sync(db, user_id("admin")) == 2
    a, b = db.get(Clip, cids[0]), db.get(Clip, cids[1])
    assert (a.avg_view_pct, a.shorts_feed_share, a.hook_retention) == (85.0, 0.9, 0.9)
    assert (b.avg_view_pct, b.shorts_feed_share, b.hook_retention) == (40.0, 0.1, 0.4)
    assert analytics.diagnose(a) == [("ok", "geen duidelijke zwakke plek")]
    kinds = [t for _, t in analytics.diagnose(b)]
    assert any("Shorts-feed" in t for t in kinds) and any("begin pakt niet" in t for t in kinds)
    assert any("halverwege" in t for t in kinds)
    s = analytics.summary([a, b])
    assert s["n"] == 2 and s["low_feed"] == 1 and s["weak_hook"] == 1
    # de cijfers gaan mee in de lessen voor de AI
    assert "% kijkt nog na 3 s" in insights._line(b) and "uit Shorts-feed" in insights._line(b)
    db.close()
    page = admin_client.get("/stats").text
    assert "Waarom wel of niet verspreid" in page and "nauwelijks getest in de Shorts-feed" in page


def test_needs_reconnect_without_scope(admin_client, settings, fa):
    _account("https://www.googleapis.com/auth/youtube.upload")
    db = new_session()
    assert analytics.sync(db, user_id("admin")) == 0
    db.close()
    assert "Koppel je kanaal opnieuw" in admin_client.get("/stats").text
    assert "extra leesrecht" in admin_client.get("/account").text
    assert "yt-analytics.readonly" in yt.SCOPES
