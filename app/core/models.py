"""Databasemodellen."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JsonMixin:
    @staticmethod
    def _load(val: str | None, default: Any) -> Any:
        if not val:
            return default
        try:
            return json.loads(val)
        except ValueError:
            return default


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class SetupLock(Base):
    """Eén rij (id=1) die samen met het eerste account wordt aangemaakt.

    Het primaire-sleutel-constraint zorgt dat twee gelijktijdige setup-pogingen
    nooit allebei kunnen slagen.
    """

    __tablename__ = "setup_lock"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user: Mapped[User] = relationship()


class Invite(Base):
    """Uitnodiging of wachtwoord-reset (user_id gezet = reset van bestaand account)."""

    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="user")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Credential(Base):
    """Versleutelde API-sleutel (of cookies.txt). De waarde verlaat de server nooit."""

    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # llm | youtube | cookies
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    encrypted_value: Mapped[bytes] = mapped_column()
    mask: Mapped[str] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GlobalSetting(Base):
    __tablename__ = "global_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    channel_avg_views: Mapped[int] = mapped_column(Integer, default=1000)
    channel_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    default_num_clips: Mapped[int] = mapped_column(Integer, default=5)
    default_min_dur: Mapped[int] = mapped_column(Integer, default=20)
    default_max_dur: Mapped[int] = mapped_column(Integer, default=55)
    caption_style: Mapped[str] = mapped_column(String(32), default="geel")
    framing_default: Mapped[str] = mapped_column(String(16), default="auto")
    post_language: Mapped[str] = mapped_column(String(8), default="en")


class Project(Base, JsonMixin):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(16))  # youtube | upload
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(300), default="")
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage_message: Mapped[str] = mapped_column(String(300), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    picker_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(120), nullable=True)  # model dat echt antwoordde
    num_clips: Mapped[int] = mapped_column(Integer, default=5)
    min_dur: Mapped[int] = mapped_column(Integer, default=20)
    max_dur: Mapped[int] = mapped_column(Integer, default=55)
    caption_style: Mapped[str] = mapped_column(String(32), default="geel")
    framing: Mapped[str] = mapped_column(String(16), default="auto")
    facecam_box: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "x,y,w,h" fracties, handmatig
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    owner: Mapped[User] = relationship()
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Clip.index"
    )

    @property
    def warnings(self) -> list[str]:
        return self._load(self.warnings_json, [])

    def add_warning(self, msg: str) -> None:
        w = self.warnings
        if msg not in w:
            w.append(msg)
        self.warnings_json = json.dumps(w, ensure_ascii=False)


class Clip(Base, JsonMixin):
    __tablename__ = "clips"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(300), default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[str] = mapped_column(String(300), default="")
    category: Mapped[str] = mapped_column(String(40), default="overig")
    subscores_json: Mapped[str] = mapped_column(Text, default="{}")
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    viral_score: Mapped[float] = mapped_column(Float, default=5.0)
    source: Mapped[str] = mapped_column(String(20), default="llm")  # llm | heuristiek | handmatig
    captions_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    framing_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # resultaat: vertical|crop|blur
    framing_choice: Mapped[str | None] = mapped_column(String(16), nullable=True)  # keuze; None = projectinstelling
    crop_x: Mapped[float | None] = mapped_column(Float, nullable=True)  # handmatige positie 0..1
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|rendering|ready|failed
    render_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ass_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posted_platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    posted_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    est_mid_at_post: Mapped[float | None] = mapped_column(Float, nullable=True)
    est_low_at_post: Mapped[float | None] = mapped_column(Float, nullable=True)
    est_high_at_post: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="clips")

    __table_args__ = (UniqueConstraint("project_id", "index", name="uq_clip_index"),)

    @property
    def subscores(self) -> dict:
        return self._load(self.subscores_json, {})

    @property
    def features(self) -> dict:
        return self._load(self.features_json, {})

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def post_text(self) -> str:
        """Beschrijving + hashtags, klaar om te plakken."""
        desc = (self.description or self.hook or "").strip()
        tags = (self.hashtags or "#shorts").strip()
        return f"{desc}\n\n{tags}" if desc else tags


class Job(Base, JsonMixin):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[int | None] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), nullable=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # process | render_clip
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
