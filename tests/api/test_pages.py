from datetime import timedelta

from tests.api.seed import (
    add_evidence,
    at,
    fail_chat_symptom,
    finish_check,
    make_run,
    open_incident,
)


def test_root_redirects_to_checks_of_the_only_project(client):
    res = client.get("/")
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/dev/checks"


def test_healthz_is_ok_without_a_project(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_static_assets_are_served_from_the_repo_not_a_cdn(client):
    assert client.get("/static/kg.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200
    page = client.get("/dev/checks").text
    assert "/static/htmx.min.js" in page
    assert "unpkg" not in page and "cdn" not in page.lower()
    assert "fonts.googleapis.com" in page  # 글꼴만 외부


def test_other_project_is_404(client, engine):
    run = make_run(engine)
    assert client.get("/other/checks").status_code == 404
    assert client.get(f"/other/checks/{run}").status_code == 404
    assert client.get(f"/other/checks/{run}/steps").status_code == 404


def test_empty_db_page_renders_with_nav(client):
    res = client.get("/dev/checks")
    assert res.status_code == 200
    assert "점검 이력" in res.text
    assert "dev · minikube" in res.text
    assert "아직 점검 기록이 없습니다" in res.text


def test_shell_shows_last_and_next_check_in_seoul_time(client, engine, clock):
    run = make_run(engine, start=at(0))  # 05:05 UTC = 14:05 KST
    finish_check(engine, run, path="C")
    clock.now = at(60)
    text = client.get("/dev/checks").text
    assert "14:05" in text and "14:10" in text  # 마지막, 다음(5분 주기)


def test_shell_warns_when_checks_stopped(client, engine, clock):
    run = make_run(engine, start=at(0))
    finish_check(engine, run, path="C")
    clock.now = at(0) + timedelta(minutes=16)
    text = client.get("/dev/checks").text
    assert "점검 16분째 안 됨" in text and "마지막 정상" in text


def test_band_counts_open_incidents_and_unknown_of_latest_check(client, engine):
    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="B",
        severity="critical",
        symptoms=[fail_chat_symptom(add_evidence(engine, run))],
    )
    open_incident(engine, run, "critical")
    text = client.get("/dev/checks").text
    assert "긴급 1" in text
    assert "주의 0" in text
    assert "확인하지 못함 0" not in text  # 필터 선택지와 별개로 0건 칩은 숨김


def test_manual_check_cooldown_has_expiry(client, engine, clock):
    from core.db import CheckRunStore

    CheckRunStore(engine).create("ui", clock.now)
    response = client.get("/dev/checks")
    assert 'data-check-wait="61"' in response.text


def test_running_header_polls_until_lock_released(client, engine, clock):
    from datetime import timedelta

    from core.db import CheckLockStore

    locks = CheckLockStore(engine)
    held = locks.acquire("ui", clock.now, timedelta(minutes=4)).holder
    response = client.get("/dev/shell")
    assert 'hx-get="/dev/shell"' in response.text
    assert "점검 중" in response.text
    locks.release(held.token)
    response = client.get("/dev/shell")
    assert "hx-trigger=" not in response.text
    assert "btn-primary" in response.text
