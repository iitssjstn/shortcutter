"""Dashboard bij veel projecten: pagina's, telling in één query en een lichte poll voor lopende projecten."""
from app.core.db import new_session
from app.core.models import Clip, Job, Project
from tests.conftest import user_id


def _many(n_done: int, n_active: int) -> list[int]:
    db = new_session()
    uid = user_id("admin")
    ids = []
    for i in range(n_done + n_active):
        active = i >= n_done
        p = Project(owner_id=uid, source_type="twitch", title=f"P{i}", status="queued" if active else "done")
        db.add(p)
        db.flush()
        db.add(Clip(project_id=p.id, owner_id=uid, index=1, start=0, end=5, title="c", status="ready", viral_score=5))
        if active:
            db.add(Job(project_id=p.id, owner_id=uid, kind="process"))
        ids.append(p.id)
    db.commit()
    db.close()
    return ids


def test_dashboard_paginates_and_polls_only_active(admin_client):
    ids = _many(70, 3)
    page = admin_client.get("/").text
    assert page.count('class="project-item"') == 30
    assert "Meer laden (43 over)" in page
    # de 3 nieuwste zijn actief: alleen die worden gevolgd
    active = ids[-3:]
    assert f'/partials/projects/active?ids={",".join(str(i) for i in reversed(active))}' in page
    more = admin_client.get("/partials/projects?scope=mine&offset=30").text
    assert more.count('class="project-item"') == 30 and "Meer laden (13 over)" in more
    last = admin_client.get("/partials/projects?scope=mine&offset=60").text
    assert last.count('class="project-item"') == 13 and "Meer laden" not in last
    poll = admin_client.get("/partials/projects/active?ids=" + ",".join(map(str, active))).text
    assert poll.count('hx-swap-oob="true"') == 4  # 3 projecten + de poller zelf
    assert "1 clip(s)" in poll and "e in de wachtrij" in poll
    # klaar: de poller stopt
    db = new_session()
    for pid in active:
        db.get(Project, pid).status = "done"
    db.commit()
    db.close()
    poll = admin_client.get("/partials/projects/active?ids=" + ",".join(map(str, active))).text
    assert 'id="project-poller" hx-swap-oob="true" hidden' in poll and "hx-get" not in poll.split('id="project-poller"')[1]


def test_poll_only_own_projects(two_users):
    from tests.conftest import make_project_with_clip  # noqa: F401
    db = new_session()
    p = Project(owner_id=user_id("alice"), source_type="twitch", title="van alice", status="queued")
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    r = two_users["bob"].get(f"/partials/projects/active?ids={pid}")
    assert "van alice" not in r.text
