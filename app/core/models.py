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


class YoutubeAccount(Base):
    """Gekoppeld YouTube-kanaal van een gebruiker (tokens versleuteld)."""

    __tablename__ = "youtube_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    channel_id: Mapped[str] = mapped_column(String(64), default="")
    channel_title: Mapped[str] = mapped_column(String(200), default="")
    refresh_token_enc: Mapped[bytes] = mapped_column(default=b"")
    access_token_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_upload_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TwitchChannel(Base):
    """Twitch-kanaal waarvan een gebruiker clips mag gebruiken (eigen kanaal of met toestemming)."""

    __tablename__ = "twitch_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    login: Mapped[str] = mapped_column(String(40))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    broadcaster_id: Mapped[str] = mapped_column(String(40), default="")
    profile_image_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    relation: Mapped[str] = mapped_column(String(20), default="toestemming")  # eigen | toestemming
    permission_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("owner_id", "login", name="uq_twitch_owner_login"),)


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
    insights_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # lessen uit eigen resultaten
    stats_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hook_teaser: Mapped[bool] = mapped_column(Boolean, default=True)  # nieuwe clips: sterkste moment vooraan
    discord_webhook_enc: Mapped[bytes | None] = mapped_column(nullable=True)  # versleutelde Discord-webhook-URL
    uploads_per_day: Mapped[int] = mapped_column(Integer, default=3)  # voor de aanbevolen uploadtijden
    min_gap_hours: Mapped[int] = mapped_column(Integer, default=3)
    safe_mode: Mapped[bool] = mapped_column(Boolean, default=False)  # familievriendelijk: piepen + vervagen
    safe_visual: Mapped[bool] = mapped_column(Boolean, default=True)  # beelden laten controleren door AI


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
    credit: Mapped[str | None] = mapped_column(String(200), nullable=True)  # naamsvermelding, bijv. twitch.tv/naam
    source_meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # titel/beschrijving/kanaal/game van de bron
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

    @property
    def source_meta(self) -> dict:
        return self._load(self.source_meta_json, {})

    def set_clip_warning(self, index: int, msg: str | None) -> None:
        """Eén waarschuwing per clip (wordt vervangen bij opnieuw renderen)."""
        prefix = f"Clip {index}: "
        w = [x for x in self.warnings if not x.startswith(prefix)]
        if msg:
            w.append(prefix + msg)
        self.warnings_json = json.dumps(w, ensure_ascii=False)

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
    layout_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # eigen indeling uit de editor
    auto_layout_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # laatst automatisch gekozen indeling
    captions_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # eigen ondertitelregels
    caption_opts_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # positie/grootte/kleur/hoofdletters
    yt_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # queued | uploading | done | failed
    yt_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    yt_video_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    yt_options_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # titel, beschrijving, privacy, publishAt
    yt_tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # verborgen YouTube-tags, komma-gescheiden
    hook_at: Mapped[float | None] = mapped_column(Float, nullable=True)  # sterkste moment (brontijd, s)
    teaser: Mapped[bool] = mapped_column(Boolean, default=False)  # sterkste moment kort vooraan tonen
    teaser_len: Mapped[float] = mapped_column(Float, default=0.0)  # lengte van de teaser in de render (s)
    review_state: Mapped[str | None] = mapped_column(String(12), nullable=True)  # None | skipped
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Discord: 'klaar om na te kijken'
    safety_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # wat de familievriendelijke modus deed
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
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    def layout(self) -> dict | None:
        return self._load(self.layout_json, None)

    @property
    def auto_layout(self) -> dict | None:
        return self._load(self.auto_layout_json, None)

    @property
    def safety(self) -> dict:
        return self._load(self.safety_json, {})

    @property
    def custom_captions(self) -> list | None:
        return self._load(self.captions_json, None)

    @property
    def caption_opts(self) -> dict:
        return self._load(self.caption_opts_json, {})

    @property
    def post_text(self) -> str:
        """Beschrijving + hashtags, klaar om te plakken."""
        desc = (self.description or self.hook or "").strip()
        tags = (self.hashtags or "#shorts").strip()
        credit = (self.project.credit or "").strip() if self.project is not None else ""
        parts = [p for p in (desc, f"🎥 {credit}" if credit else "", tags) if p]
        return "\n\n".join(parts)


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


class Compilation(Base, JsonMixin):
    """Liggende (16:9) compilatievideo van meerdere Twitch-clips, bijv. 'beste clips van de week'."""

    __tablename__ = "compilations"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    period: Mapped[str] = mapped_column(String(10), default="week")
    ranking: Mapped[str] = mapped_column(String(12), default="countdown")  # countdown | top | none
    intro: Mapped[bool] = mapped_column(Boolean, default=True)
    outro: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|queued|rendering|done|failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage_message: Mapped[str] = mapped_column(String(300), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    yt_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags: Mapped[str | None] = mapped_column(String(300), nullable=True)
    yt_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # queued | uploading | done | failed
    yt_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    yt_video_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    yt_options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    yt_tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)  # gemaakt door de wekelijkse planning
    format: Mapped[str] = mapped_column(String(10), default="landscape")  # landscape (16:9) | short (9:16 top-lijst)

    items: Mapped[list["CompilationItem"]] = relationship(
        back_populates="compilation", cascade="all, delete-orphan", order_by="CompilationItem.position"
    )

    @property
    def chapters(self) -> list:
        return self._load(self.chapters_json, [])

    @property
    def included(self) -> list["CompilationItem"]:
        return [i for i in self.items if i.included]


class CompilationItem(Base):
    __tablename__ = "compilation_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    compilation_id: Mapped[int] = mapped_column(ForeignKey("compilations.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    clip_url: Mapped[str] = mapped_column(String(300))
    slug: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200), default="")
    streamer: Mapped[str] = mapped_column(String(80), default="")
    channel_login: Mapped[str] = mapped_column(String(40), default="")
    broadcaster_id: Mapped[str] = mapped_column(String(40), default="")
    game: Mapped[str | None] = mapped_column(String(120), nullable=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    trim_start: Mapped[float] = mapped_column(Float, default=0.0)
    trim_end: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = tot het einde
    thumbnail_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # indeling in een short-top-lijst: fill (uitsnede, crop_x = positie) of split (facecam + gameplay)
    layout: Mapped[str] = mapped_column(String(10), default="fill")
    crop_x: Mapped[float] = mapped_column(Float, default=0.5)
    cam_box: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "x,y,w,h" fracties; leeg = automatisch
    cam_pos: Mapped[str] = mapped_column(String(8), default="bottom")  # top | bottom

    compilation: Mapped[Compilation] = relationship(back_populates="items")

    @property
    def length(self) -> float:
        end = self.trim_end if self.trim_end is not None else self.duration
        return max(0.0, (end or 0.0) - (self.trim_start or 0.0))


class CompilationSchedule(Base, JsonMixin):
    """Wekelijkse automatische compilatie per gebruiker: maakt (en rendert) een concept, uploadt niets zelf."""

    __tablename__ = "compilation_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    count: Mapped[int] = mapped_column(Integer, default=10)
    ranking: Mapped[str] = mapped_column(String(12), default="countdown")
    intro: Mapped[bool] = mapped_column(Boolean, default=True)
    outro: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_render: Mapped[bool] = mapped_column(Boolean, default=True)
    format: Mapped[str] = mapped_column(String(10), default="landscape")
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0 = maandag
    hour: Mapped[int] = mapped_column(Integer, default=9)  # lokale tijd
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_compilation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def channel_ids(self) -> list[int]:
        return [int(x) for x in self._load(self.channel_ids_json, []) if str(x).isdigit()]


class AutoPilot(Base, JsonMixin):
    """Haalt periodiek nieuwe top-clips op van gekozen Twitch-kanalen en maakt er shorts van."""

    __tablename__ = "autopilots"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    period: Mapped[str] = mapped_column(String(10), default="dag")
    min_views: Mapped[int] = mapped_column(Integer, default=0)
    max_per_run: Mapped[int] = mapped_column(Integer, default=5)
    every_hours: Mapped[int] = mapped_column(Integer, default=12)
    framing: Mapped[str] = mapped_column(String(16), default="split")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def channel_ids(self) -> list[int]:
        return [int(x) for x in self._load(self.channel_ids_json, []) if str(x).isdigit()]
