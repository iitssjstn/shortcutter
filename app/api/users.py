"""Gebruikersbeheer (alleen admin): uitnodigen, (de)activeren, rol, reset, verwijderen."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import INVITE_VALID, valid_username
from app.api.common import redirect, render
from app.core.auth import invalidate_user_sessions, require_admin
from app.core.crypto import token_hash
from app.core.db import get_db
from app.core.models import Invite, Project, User, utcnow
from app.core.paths import project_input, project_output
from app.core.store import active_admin_count, audit

router = APIRouter()


def _link(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + f"/invite/{token}"


def _user(db: Session, uid: int) -> User:
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "Gebruiker niet gevonden.")
    return u


def _is_last_admin(db: Session, u: User) -> bool:
    return u.is_admin and u.is_active and active_admin_count(db) <= 1


@router.get("/users")
def users_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.username)).all()
    invites = db.scalars(
        select(Invite).where(Invite.used_at.is_(None), Invite.expires_at > utcnow()).order_by(Invite.created_at.desc())
    ).all()
    return render(request, "users.html", {"nav": "users", "users": users, "invites": invites})


def _new_invite(db: Session, request: Request, admin: User, username: str, role: str, user_id: int | None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        Invite(
            token_hash=token_hash(token),
            username=username,
            role=role,
            user_id=user_id,
            created_by=admin.id,
            expires_at=utcnow() + INVITE_VALID,
        )
    )
    return _link(request, token)


@router.post("/users/invite")
def invite(
    request: Request,
    username: str = Form(...),
    role: str = Form("user"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = username.strip()
    err = valid_username(username)
    if err:
        return redirect("/users", err, "error")
    if role not in ("user", "admin"):
        role = "user"
    if db.scalar(select(User).where(User.username == username)):
        return redirect("/users", "Die gebruikersnaam bestaat al.", "error")
    for old in db.scalars(select(Invite).where(Invite.username == username, Invite.used_at.is_(None))):
        old.used_at = utcnow()  # oude links vervallen
    link = _new_invite(db, request, admin, username, role, None)
    audit(db, admin, "gebruiker_uitgenodigd", username)
    db.commit()
    users = db.scalars(select(User).order_by(User.username)).all()
    invites = db.scalars(select(Invite).where(Invite.used_at.is_(None), Invite.expires_at > utcnow())).all()
    return render(
        request, "users.html",
        {"nav": "users", "users": users, "invites": invites, "new_link": link, "new_link_user": username, "new_link_kind": "uitnodiging"},
    )


@router.post("/users/{uid}/reset-password")
def reset_password(request: Request, uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, uid)
    for old in db.scalars(select(Invite).where(Invite.user_id == u.id, Invite.used_at.is_(None))):
        old.used_at = utcnow()
    link = _new_invite(db, request, admin, u.username, u.role, u.id)
    audit(db, admin, "wachtwoord_reset_aangemaakt", u.username)
    db.commit()
    users = db.scalars(select(User).order_by(User.username)).all()
    invites = db.scalars(select(Invite).where(Invite.used_at.is_(None), Invite.expires_at > utcnow())).all()
    return render(
        request, "users.html",
        {"nav": "users", "users": users, "invites": invites, "new_link": link, "new_link_user": u.username, "new_link_kind": "resetlink"},
    )


@router.post("/users/invites/{iid}/revoke")
def revoke_invite(iid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    inv = db.get(Invite, iid)
    if inv and inv.used_at is None:
        inv.used_at = utcnow()
        audit(db, admin, "uitnodiging_ingetrokken", inv.username)
        db.commit()
    return redirect("/users", "Link ingetrokken.")


@router.post("/users/{uid}/active")
def set_active(uid: int, active: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, uid)
    make_active = active in ("1", "true", "on")
    if not make_active and _is_last_admin(db, u):
        return redirect("/users", "De laatste actieve beheerder kan niet gedeactiveerd worden.", "error")
    u.is_active = make_active
    if not make_active:
        invalidate_user_sessions(db, u.id)
    audit(db, admin, "gebruiker_geactiveerd" if make_active else "gebruiker_gedeactiveerd", u.username)
    db.commit()
    return redirect("/users", f"{u.username} is {'geactiveerd' if make_active else 'gedeactiveerd'}.")


@router.post("/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, uid)
    if role not in ("user", "admin"):
        raise HTTPException(400, "Onbekende rol.")
    if role != "admin" and _is_last_admin(db, u):
        return redirect("/users", "De laatste actieve beheerder kan niet gedegradeerd worden.", "error")
    u.role = role
    audit(db, admin, "rol_gewijzigd", f"{u.username} -> {role}")
    db.commit()
    return redirect("/users", f"Rol van {u.username} is nu {role}.")


@router.post("/users/{uid}/delete")
def delete_user(uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, uid)
    if _is_last_admin(db, u):
        return redirect("/users", "De laatste actieve beheerder kan niet verwijderd worden.", "error")
    if u.id == admin.id:
        return redirect("/users", "Je kunt jezelf niet verwijderen; laat een andere beheerder dat doen.", "error")
    import shutil

    for p in db.scalars(select(Project).where(Project.owner_id == u.id)):
        shutil.rmtree(project_input(p.id), ignore_errors=True)
        shutil.rmtree(project_output(p.id), ignore_errors=True)
    name = u.username
    invalidate_user_sessions(db, u.id)
    db.delete(u)
    audit(db, admin, "gebruiker_verwijderd", name)
    db.commit()
    return redirect("/users", f"{name} en diens projecten zijn verwijderd.")
