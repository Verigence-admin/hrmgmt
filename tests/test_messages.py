from __future__ import annotations

import smtplib
import uuid

import pytest
from sqlalchemy import text

from hrmgmt import mailer as mailer_mod
from hrmgmt import message_templates as tpl
from hrmgmt import permissions as perm
from hrmgmt.mailer import MailError, SmtpMailer
from tests.support import World, ist


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []
        self.error: str | None = None

    def send(self, *, to, subject, body):
        if self.error:
            raise MailError(self.error, "x")
        self.sent.append({"to": to, "subject": subject, "body": body})


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


def send(world, user, template, ids, **extra):
    return world.client.post(
        "/hr/v1/messages/send",
        json={"template": template, "employee_ids": ids, **extra},
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

        def login(self, user, password):
            calls.append("login")
            if FakeSmtp.fail:
                raise FakeSmtp.fail

        def send_message(self, message):
            calls.append("send")
            assert message["To"] == "a@example.com" and "Verigence HR" in message["From"]
            assert message.get_body(("plain",)).get_content().strip() == "Hello <b>"
            assert "&lt;b&gt;" in message.get_body(("html",)).get_content()

    monkeypatch.setattr(mailer_mod.smtplib, "SMTP", FakeSmtp)
    m = SmtpMailer(host="smtp.example.com", port=587, user="hr@example.com", password="pw")
    m.send(to="a@example.com", subject="S", body="Hello <b>")
    assert calls == ["connect smtp.example.com:587", "starttls", "login", "send"]
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
    assert [t["code"] for t in body["items"]] == ["GENERAL", "WELCOME"] and body[
        "mailConfigured"
    ] is True
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


def test_a_general_message_goes_to_each_chosen_employee_and_is_logged(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr, full_name="Asha Rao")
    r = send(
        world,
        hr,
        "GENERAL",
        [emp["employeeId"]],
        subject="Holiday",
        body="Dear {{name}}, office closed.",
    )
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "SENT"
    assert world.mailer.sent[0]["to"] == emp["personalEmail"]
    assert world.mailer.sent[0]["body"] == "Dear Asha Rao, office closed."
    log = world.client.get("/hr/v1/messages/log", headers=world.headers(hr)).json()["items"]
    assert log[0]["status"] == "SENT" and log[0]["template"] == "GENERAL"


def test_a_welcome_message_sets_a_fresh_password_and_emails_it_but_never_stores_it(
    world, migrated_engine
):
    hr = hr_user(world)
    emp, user = world.employee(hr, full_name="Asha Rao")
    r = send(world, hr, "WELCOME", [emp["employeeId"]])
    assert r.json()["results"][0]["status"] == "SENT", r.text
    ((uid, password),) = world.app.state.provisioner.passwords
    assert uid == user and len(password) >= 12
    mail = world.mailer.sent[0]
    assert password in mail["body"] and f"login-{user[:8]}@example.com" in mail["body"]
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


def test_a_welcome_message_skips_people_without_a_login_or_not_yet_allowed(world, migrated_engine):
    hr = hr_user(world)
    no_login, _ = world.employee(hr)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": no_login["employeeId"]},
        )
    pending, _ = world.employee(hr)
    world.app.state.provisioner.set_password_error = "LOGIN_NOT_ACTIVE"
    r = send(world, hr, "WELCOME", [no_login["employeeId"], pending["employeeId"]]).json()[
        "results"
    ]
    assert [x["code"] for x in r] == ["NO_LOGIN", "LOGIN_NOT_ACTIVE"]
    assert all(x["status"] == "SKIPPED" for x in r) and world.mailer.sent == []
    assert "SuperAdmin" in r[1]["message"]


def test_a_mail_failure_is_reported_once_and_not_retried(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    world.mailer.error = "MAIL_UNAVAILABLE"
    r = send(world, hr, "WELCOME", [emp["employeeId"]]).json()["results"][0]
    assert (
        r["status"] == "FAILED"
        and r["code"] == "MAIL_UNAVAILABLE"
        and "Send it again" in r["message"]
    )
    assert len(world.app.state.provisioner.passwords) == 1  # one attempt, no retry


def test_sending_needs_mail_set_up_the_right_permission_and_a_small_batch(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    assert (
        send(world, world.grant(str(uuid.uuid4())), "GENERAL", [emp["employeeId"]]).status_code
        == 403
    )
    too_many = [str(uuid.uuid4()) for _ in range(6)]
    assert send(world, hr, "GENERAL", too_many).status_code == 422
    # a reworded WELCOME is not allowed: the login must always come from the saved template
    r = send(world, hr, "WELCOME", [emp["employeeId"]], body="x {{login_id}} {{temp_password}}")
    assert r.status_code == 422
    world.app.state.mailer = None
    assert send(world, hr, "GENERAL", [emp["employeeId"]]).status_code == 503
    world.app.state.mailer = world.mailer
    whatsapp = send(world, hr, "GENERAL", [emp["employeeId"]], channel="WHATSAPP")
    assert (
        whatsapp.status_code == 503 and whatsapp.json()["code"] == "MESSAGE_CHANNEL_NOT_AVAILABLE"
    )
