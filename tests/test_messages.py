from __future__ import annotations

import smtplib
import uuid

import pytest
from sqlalchemy import text

from hrmgmt import mailer as mailer_mod
from hrmgmt import message_templates as tpl
from hrmgmt import permissions as perm
from hrmgmt.mailer import MailError, SmtpMailer
from hrmgmt.provisioning import UserSummary
from tests.support import World, ist


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []
        self.error: str | None = None

    def send(self, *, to, subject, body, html_body=None, images=None):
        if self.error:
            raise MailError(self.error, "x")
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "html": html_body, "images": images}
        )


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 12, 11, 0))
    w.mailer = FakeMailer()
    w.app.state.mailer = w.mailer
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.message_log"))
        conn.execute(text("DELETE FROM hr.message_template"))
        conn.execute(text("DELETE FROM hr.setting"))
    return w


def hr_user(world):
    return world.grant(
        str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ, perm.HR_SETTINGS_MANAGE
    )


def add_user(world, name="Asha Rao", email=None, status="ACTIVE", is_employee=False) -> str:
    uid = str(uuid.uuid4())
    summary = UserSummary(uid, name, email or f"{uid[:8]}@example.com", status, is_employee)
    prov = world.app.state.provisioner
    prov.users = [*getattr(prov, "users", []), summary]
    return uid


def send(world, user, template, ids, **extra):
    return world.client.post(
        "/hr/v1/messages/send",
        json={"template": template, "user_ids": ids, **extra},
        headers=world.headers(user),
    )


# ---- templates in isolation ----------------------------------------------------------------


def test_templates_accept_only_known_placeholders_and_keep_the_login_in_the_welcome_message():
    tpl.validate(tpl.GENERAL, "Hello {{name}}", "Dear {{name}}, see {{app_link}}")
    with pytest.raises(tpl.TemplateError):
        tpl.validate(tpl.GENERAL, "Hi", "Dear {{name}} {{temp_password}}")  # not for GENERAL
    with pytest.raises(tpl.TemplateError):
        tpl.validate(tpl.WELCOME, "Hi", "Dear {{name}}, sign in with {{login_id}}")  # no password
    with pytest.raises(tpl.TemplateError):
        tpl.validate(tpl.WELCOME, "Your {{temp_password}}", "{{login_id}} {{temp_password}}")
    with pytest.raises(tpl.TemplateError):
        tpl.validate(tpl.GENERAL, "Hi", "Dear {{nme}}")
    assert tpl.render("Hi {{ name }}, {{x}}", {"name": "Asha"}) == "Hi Asha, "
    for code, d in tpl.DEFAULTS.items():
        tpl.validate(code, d["subject"], d["body"])


def test_smtp_mailer_sends_once_with_tls_and_maps_failures(monkeypatch):
    calls: list[str] = []

    class FakeSmtp:
        fail: Exception | None = None

        def __init__(self, host, port, timeout):
            calls.append(f"connect {host}:{port}")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append("starttls")

        def ehlo(self):
            calls.append("ehlo")

        def login(self, user, password):
            calls.append("login")
            if FakeSmtp.fail:
                raise FakeSmtp.fail

        def send_message(self, message):
            calls.append("send")
            assert message["To"] == "a@example.com" and message["From"].startswith("Verigence <")
            assert message.get_body(("plain",)).get_content().strip() == "Hello <b>"
            assert "&lt;b&gt;" in message.get_body(("html",)).get_content()

    monkeypatch.setattr(mailer_mod.smtplib, "SMTP", FakeSmtp)
    m = SmtpMailer(host="smtp.example.com", port=587, user="hr@example.com", password="pw")
    m.send(to="a@example.com", subject="S", body="Hello <b>")
    assert calls == ["connect smtp.example.com:587", "starttls", "ehlo", "login", "send"]
    FakeSmtp.fail = smtplib.SMTPAuthenticationError(535, b"no")
    with pytest.raises(MailError) as err:
        m.send(to="a@example.com", subject="S", body="x")
    assert err.value.code == "MAIL_AUTH_FAILED"
    FakeSmtp.fail = OSError("down")
    with pytest.raises(MailError) as err:
        m.send(to="a@example.com", subject="S", body="x")
    assert err.value.code == "MAIL_UNAVAILABLE"


# ---- the API ---------------------------------------------------------------------------------


def test_templates_are_listed_with_defaults_and_edited_only_by_settings_managers(world):
    hr = hr_user(world)
    body = world.client.get("/hr/v1/messages/templates", headers=world.headers(hr)).json()
    assert [t["code"] for t in body["items"]] == ["GENERAL", "WELCOME"]
    assert body["mailConfigured"] is True and body["channels"] == {"EMAIL": True, "WHATSAPP": False}
    sender_only = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    denied = world.client.put(
        "/hr/v1/messages/templates/GENERAL",
        json={"subject": "S {{name}}", "body": "B {{name}}"},
        headers=world.headers(sender_only),
    )
    assert denied.status_code == 403
    ok = world.client.put(
        "/hr/v1/messages/templates/GENERAL",
        json={"subject": "Notice for {{name}}", "body": "Dear {{name}}, new rules."},
        headers=world.headers(hr),
    )
    assert ok.status_code == 200 and ok.json()["customised"] is True
    bad = world.client.put(
        "/hr/v1/messages/templates/WELCOME",
        json={"subject": "Hi", "body": "no login here {{name}}"},
        headers=world.headers(hr),
    )
    assert bad.status_code == 422 and bad.json()["code"] == "MESSAGE_TEMPLATE_INVALID"


def test_recipients_are_the_users_group_with_employees_tagged(world):
    hr = hr_user(world)
    emp, user = world.employee(hr, full_name="Asha Rao")
    world.app.state.provisioner.users = [
        UserSummary(user, "Asha Rao", "asha@example.com", "ACTIVE", True),
        UserSummary(str(uuid.uuid4()), "Outside Person", "out@example.com", "PENDING", False),
    ]
    body = world.client.get("/hr/v1/messages/recipients", headers=world.headers(hr)).json()
    by_name = {i["displayName"]: i for i in body["items"]}
    assert (
        by_name["Asha Rao"]["employeeId"] == emp["employeeId"] and by_name["Asha Rao"]["isEmployee"]
    )
    assert (
        by_name["Outside Person"]["employeeId"] is None
        and by_name["Outside Person"]["status"] == "PENDING"
    )
    found = world.client.get(
        "/hr/v1/messages/recipients?q=outside", headers=world.headers(hr)
    ).json()
    assert [i["displayName"] for i in found["items"]] == ["Outside Person"]
    assert (
        world.client.get(
            "/hr/v1/messages/recipients", headers=world.headers(world.grant(str(uuid.uuid4())))
        ).status_code
        == 403
    )


def test_a_general_message_goes_to_each_chosen_user_even_without_an_employee_record(world):
    hr = hr_user(world)
    uid = add_user(world, "Asha Rao", "asha@example.com")
    r = send(world, hr, "GENERAL", [uid], subject="Holiday", body="Dear {{name}}, office closed.")
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "SENT"
    assert world.mailer.sent[0]["to"] == "asha@example.com"
    assert world.mailer.sent[0]["body"] == "Dear Asha Rao, office closed."
    log = world.client.get("/hr/v1/messages/log", headers=world.headers(hr)).json()["items"]
    assert (
        log[0]["status"] == "SENT" and log[0]["name"] == "Asha Rao" and log[0]["employeeId"] is None
    )


def test_a_welcome_message_sets_a_fresh_password_and_emails_it_but_never_stores_it(
    world, migrated_engine
):
    hr = hr_user(world)
    uid = add_user(world, "Asha Rao", "asha@example.com")
    r = send(world, hr, "WELCOME", [uid])
    assert r.json()["results"][0]["status"] == "SENT", r.text
    ((set_for, password),) = world.app.state.provisioner.passwords
    assert set_for == uid and len(password) >= 12
    mail = world.mailer.sent[0]
    assert password in mail["body"] and f"login-{uid[:8]}@example.com" in mail["body"]
    assert (
        "Asha Rao" in mail["body"] and "{{" not in mail["body"] and password not in mail["subject"]
    )
    with migrated_engine.connect() as conn:
        dump = " ".join(
            str(r)
            for table in ("hr.message_log", "hr.audit_log", "hr.message_template")
            for r in conn.execute(text(f"SELECT * FROM {table}")).all()
        )
    assert password not in dump


def test_people_who_cannot_be_messaged_are_skipped_with_a_reason(world):
    hr = hr_user(world)
    pending = add_user(world, "Pending Person", status="PENDING")
    suspended = add_user(world, "Suspended Person", status="SUSPENDED")
    no_email = add_user(world, "No Email")
    prov = world.app.state.provisioner
    prov.users = [
        UserSummary(
            u.user_id, u.display_name, None if u.user_id == no_email else u.email, u.status, False
        )
        for u in prov.users
    ]
    prov.set_password_error = "LOGIN_NOT_ACTIVE"
    ids = [pending, suspended, no_email, str(uuid.uuid4())]
    welcome = send(world, hr, "WELCOME", ids[:1]).json()["results"]
    assert welcome[0]["code"] == "LOGIN_NOT_ACTIVE" and "SuperAdmin" in welcome[0]["message"]
    general = send(world, hr, "GENERAL", ids).json()["results"]
    assert [x["code"] for x in general] == [None, "NOT_ACTIVE", "NO_EMAIL", "NOT_FOUND"]
    assert [x["status"] for x in general] == ["SENT", "SKIPPED", "SKIPPED", "SKIPPED"]
    assert len(world.mailer.sent) == 1  # only the pending person got the general note


def test_a_mail_failure_is_reported_once_and_not_retried(world):
    hr = hr_user(world)
    uid = add_user(world)
    world.mailer.error = "MAIL_UNAVAILABLE"
    r = send(world, hr, "WELCOME", [uid]).json()["results"][0]
    assert (
        r["status"] == "FAILED"
        and r["code"] == "MAIL_UNAVAILABLE"
        and "Send it again" in r["message"]
    )
    assert len(world.app.state.provisioner.passwords) == 1  # one attempt, no retry


def test_sending_needs_mail_set_up_the_right_permission_and_a_small_batch(world):
    hr = hr_user(world)
    uid = add_user(world)
    assert send(world, world.grant(str(uuid.uuid4())), "GENERAL", [uid]).status_code == 403
    too_many = [str(uuid.uuid4()) for _ in range(6)]
    assert send(world, hr, "GENERAL", too_many).status_code == 422
    # a reworded WELCOME is not allowed: the login must always come from the saved template
    r = send(world, hr, "WELCOME", [uid], body="x {{login_id}} {{temp_password}}")
    assert r.status_code == 422
    world.app.state.mailer = None
    assert send(world, hr, "GENERAL", [uid]).status_code == 503
    world.app.state.mailer = world.mailer
    whatsapp = send(world, hr, "GENERAL", [uid], channel="WHATSAPP")
    assert (
        whatsapp.status_code == 503 and whatsapp.json()["code"] == "MESSAGE_CHANNEL_NOT_AVAILABLE"
    )


def test_a_test_send_goes_to_one_address_with_a_fake_password_and_touches_no_login(
    world, migrated_engine
):
    hr = hr_user(world)
    r = world.client.post(
        "/hr/v1/messages/test",
        json={"template": "WELCOME", "to": "Me@Example.com"},
        headers=world.headers(hr),
    )
    assert r.status_code == 200 and r.json()["status"] == "SENT"
    mail = world.mailer.sent[0]
    assert mail["to"] == "me@example.com" and mail["subject"].startswith("[TEST] ")
    assert (
        "TEST-ONLY-NOT-A-REAL-PASSWORD" in mail["body"] and "No login was changed" in mail["body"]
    )
    assert not getattr(world.app.state.provisioner, "passwords", [])
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM hr.message_log")).scalar_one() == 0
    bad = world.client.post(
        "/hr/v1/messages/test",
        json={"template": "GENERAL", "to": "not-an-email"},
        headers=world.headers(hr),
    )
    assert bad.status_code == 422
    world.mailer.error = "MAIL_AUTH_FAILED"
    failed = world.client.post(
        "/hr/v1/messages/test",
        json={"template": "GENERAL", "to": "me@example.com"},
        headers=world.headers(hr),
    ).json()
    assert failed["status"] == "FAILED" and failed["code"] == "MAIL_AUTH_FAILED"
    sender_less = world.grant(str(uuid.uuid4()))
    assert (
        world.client.post(
            "/hr/v1/messages/test",
            json={"template": "GENERAL", "to": "me@example.com"},
            headers=world.headers(sender_less),
        ).status_code
        == 403
    )


def test_the_welcome_email_has_a_designed_page_with_pictures_and_everything_is_escaped(world):
    from hrmgmt.email_html import IMAGES, welcome_html

    page = welcome_html(
        {
            "name": "Asha <b>Rao</b>",
            "company": "Verigence",
            "login_id": "a&b@example.com",
            "temp_password": "Xk7@m<Q2>&p",
            "app_link": "https://example.test/apps?x=1&y=2",
        }
    )
    assert "<b>Rao</b>" not in page and "Asha &lt;b&gt;Rao&lt;/b&gt;" in page
    assert "Xk7@m&lt;Q2&gt;&amp;p" in page and "a&amp;b@example.com" in page
    assert 'href="https://example.test/apps?x=1&amp;y=2"' in page
    for cid in IMAGES:
        assert f"cid:{cid}" in page
    assert "Forgot password?" in page and "Resend code" in page


def test_a_welcome_test_send_carries_the_page_and_the_pictures_but_a_general_one_does_not(world):
    keeper = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    welcome = world.client.post(
        "/hr/v1/messages/test",
        json={"template": "WELCOME", "channel": "EMAIL", "to": "me@example.com"},
        headers=world.headers(keeper),
    )
    assert welcome.status_code == 200, welcome.text
    sent = world.mailer.sent[-1]
    assert sent["html"] and "TEST-ONLY-NOT-A-REAL-PASSWORD" in sent["html"]
    assert len(sent["images"]) == 5
    world.client.post(
        "/hr/v1/messages/test",
        json={"template": "GENERAL", "channel": "EMAIL", "to": "me@example.com"},
        headers=world.headers(keeper),
    )
    assert world.mailer.sent[-1]["html"] is None


def test_the_smtp_mailer_builds_a_related_html_part_with_inline_pictures(monkeypatch):
    from email.message import EmailMessage

    from hrmgmt.mailer import SmtpMailer

    captured: list[EmailMessage] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            pass

        def ehlo(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            captured.append(message)

    monkeypatch.setattr("hrmgmt.mailer.smtplib.SMTP", FakeSmtp)
    SmtpMailer(host="h", port=587, user="u", password="p").send(
        to="a@example.com",
        subject="s",
        body="plain",
        html_body='<img src="cid:pic1">',
        images={"pic1": b"\x89PNG\r\n\x1a\n"},
    )
    message = captured[0]
    assert message.get_body(("plain",)).get_content().strip() == "plain"
    html_part = message.get_body(("html",))
    assert "cid:pic1" in html_part.get_content()
    related = [p for p in message.walk() if p.get_content_type() == "image/png"]
    assert len(related) == 1 and related[0]["Content-ID"] == "<pic1>"
