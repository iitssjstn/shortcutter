from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import setup as setup_mod
from app.core.config import Settings
from app.core.db import new_session
from app.core.models import Clip, Project, User, UserSettings
from app.core.passwords import hash_password
from app.main import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.master_key_file = tmp_path / "geen-secret"
    s.run_worker = False
    return s


@pytest.fixture
def app(settings):
    return create_app(settings)


class Client(TestClient):
    """TestClient die automatisch het CSRF-token meestuurt."""

    def csrf(self) -> str:
        if "sc_csrf" not in self.cookies:
            self.get("/healthz")
            self.get("/login")
        return self.cookies.get("sc_csrf", "")

    def post(self, url, *args, csrf: bool = True, **kwargs):
        if csrf:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRF-Token", self.csrf())
            kwargs["headers"] = headers
        return super().post(url, *args, **kwargs)


def make_client(app) -> Client:
    return Client(app, follow_redirects=False)


@pytest.fixture
def client(app) -> Client:
    return make_client(app)


def create_user(username: str, role: str = "user", active: bool = True) -> User:
    db = new_session()
    try:
        u = User(username=username, password_hash=hash_password(PASSWORD), role=role, is_active=active)
        db.add(u)
        db.flush()
        db.add(UserSettings(user_id=u.id))
        db.commit()
        setup_mod._has_users_cache = True
        return u
    finally:
        db.close()


def login(c: Client, username: str, password: str = PASSWORD):
    c.csrf()
    r = c.post("/login", data={"username": username, "password": password})
    return r


@pytest.fixture
def admin_client(app):
    create_user("admin", "admin")
    c = make_client(app)
    assert login(c, "admin").status_code == 303
    return c


@pytest.fixture
def two_users(app):
    """Admin, gebruiker A en gebruiker B, elk met een ingelogde client."""
    create_user("admin", "admin")
    create_user("alice")
    create_user("bob")
    out = {}
    for name in ("admin", "alice", "bob"):
        c = make_client(app)
        assert login(c, name).status_code == 303
        out[name] = c
    return out


def user_id(name: str) -> int:
    db = new_session()
    try:
        from sqlalchemy import select

        return db.scalar(select(User.id).where(User.username == name))
    finally:
        db.close()


def make_project_with_clip(settings: Settings, owner: str, n_clips: int = 1) -> tuple[int, list[int]]:
    """Project met 'gerenderde' clips (nepbestanden) en een transcript."""
    db = new_session()
    try:
        p = Project(owner_id=user_id(owner), source_type="upload", title=f"Project van {owner}", status="done",
                    duration=60.0, source_filename="x.mp4")
        db.add(p)
        db.commit()
        out = settings.output_dir / str(p.id)
        out.mkdir(parents=True, exist_ok=True)
        transcript = {"duration": 60.0, "segments": [{"start": 0, "end": 10, "text": "Hallo wereld dit is een test.",
                      "words": [{"start": i * 1.0, "end": i * 1.0 + 0.8, "word": w} for i, w in
                                enumerate("Hallo wereld dit is een test.".split())]}]}
        (out / "transcript.json").write_text(json.dumps(transcript))
        (out / "thumb.jpg").write_bytes(b"\xff\xd8\xff fake jpg")
        ids = []
        for i in range(1, n_clips + 1):
            (out / f"clip_{i:02d}.mp4").write_bytes(b"FAKE-MP4-FINAL-%d" % i * 100)
            (out / f"clip_{i:02d}_raw.mp4").write_bytes(b"FAKE-MP4-RAW-%d" % i * 100)
            c = Clip(project_id=p.id, owner_id=p.owner_id, index=i, start=0.0, end=5.0, title=f"Clip {i} titel",
                     hook="Een hook", file_path=f"clip_{i:02d}.mp4", raw_path=f"clip_{i:02d}_raw.mp4",
                     ass_path=f"clip_{i:02d}.ass", status="ready", viral_score=6.0,
                     subscores_json=json.dumps({"hook_sterkte": 6, "emotie_of_verrassing": 6,
                                                "begrijpelijkheid_zonder_context": 6, "afronding": 6}))
            db.add(c)
            db.commit()
            ids.append(c.id)
        return p.id, ids
    finally:
        db.close()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """Synthetische liggende video (testsrc2 + sinus) van 8 seconden."""
    d = tmp_path_factory.mktemp("media")
    path = d / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


def fake_transcript(duration: float = 8.0) -> dict:
    words_text = ("Weet je waarom dit werkt? Het is eigenlijk heel simpel. "
                  "Je moet gewoon beginnen. Dat is alles wat telt!").split()
    step = duration / (len(words_text) + 1)
    words = [{"start": round(i * step, 2), "end": round(i * step + step * 0.8, 2), "word": w}
             for i, w in enumerate(words_text)]
    return {"duration": duration, "language": "nl",
            "segments": [{"start": words[0]["start"], "end": words[-1]["end"],
                          "text": " ".join(words_text), "words": words}]}
