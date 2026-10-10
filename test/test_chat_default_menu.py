"""
Automatic default chat menu.

A Business / Premium account saves one menu (GET/PUT /api/chat/options/default-menu).
When a customer messages them and the conversation is new or has been quiet
for a day, the backend sends the account's greeting with the menu attached —
the customer sends nothing special. Explicit POST /api/chat/options keeps
working (see test_chat_options.py).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app import models
from app.add_chat_default_menu_table import migrate
from app.routers import chat_routes
from app.services import chat_option_service as svc

MENU = [
    {"title": "Products", "action": "products"},
    {"title": "Today's Deals", "action": "deals"},
    {"title": "Track Order", "action": "track_order"},
    {"title": "Talk to Support", "action": "support"},
]
URL = "/api/chat/options/default-menu"


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def biz(make_user, db):
    u = make_user("bizshop")
    u.account_type = models.AccountType.business
    db.commit()
    return u


@pytest.fixture()
def premium(make_user, db):
    u = make_user("premiumguy")
    plan = models.MembershipPlan(name="Pro", price_amount=9900)
    db.add(plan)
    db.commit()
    db.add(models.UserMembership(
        user_id=u.id, plan_id=plan.id, status=models.MembershipStatus.active,
        current_period_end=datetime.now(timezone.utc) + timedelta(days=10),
    ))
    db.commit()
    return u


@pytest.fixture()
def customer(make_user):
    return make_user("customer")


@pytest.fixture()
def convo(client):
    def _make(creator, *others):
        client.login(creator)
        r = client.post("/api/chat/conversations", json={"participant_ids": [u.id for u in others]})
        assert r.status_code == 201, r.text
        return r.json()["id"]
    return _make


def _save_menu(client, owner, enabled=True, options=MENU, **extra):
    client.login(owner)
    r = client.put(URL, json={"is_enabled": enabled, "options": options, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _send(client, cid, content="Hi", **kw):
    return client.post(f"/api/chat/conversations/{cid}/messages", json={"content": content, **kw})


def _messages(client, cid):
    return client.get(f"/api/chat/conversations/{cid}/messages", params={"limit": 100}).json()["items"]


def _backdate_people_messages(db, cid, hours):
    """Make every human-written message in the conversation `hours` old."""
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    for m in db.query(models.Message).filter_by(conversation_id=cid, is_auto_message=False):
        m.created_at = old
    db.commit()


# ------------------------------------------------------------------ GET / PUT API

@pytest.mark.parametrize("who", ["biz", "premium"])
def test_get_defaults_when_nothing_saved(client, request, who, db):
    owner = request.getfixturevalue(who)
    client.login(owner)
    r = client.get(URL)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "is_enabled": False, "greeting": "How can we help you?", "options": [], "inactivity_hours": 24,
    }
    assert db.query(models.ChatDefaultMenu).count() == 0  # reading creates nothing


@pytest.mark.parametrize("who", ["biz", "premium"])
def test_put_saves_menu_greeting_and_enabled(client, request, who):
    owner = request.getfixturevalue(who)
    client.login(owner)
    r = client.put(URL, json={"is_enabled": True, "greeting": "  Welcome to our shop!  ", "options": MENU})
    assert r.status_code == 200, r.text
    expected = {
        "is_enabled": True, "greeting": "Welcome to our shop!", "options": MENU, "inactivity_hours": 24,
    }
    assert r.json() == expected
    assert client.get(URL).json() == expected


def test_put_is_partial(client, biz):
    _save_menu(client, biz, greeting="Hello!")
    r = client.put(URL, json={"is_enabled": False}).json()
    assert r["is_enabled"] is False and r["greeting"] == "Hello!" and len(r["options"]) == 4
    r = client.put(URL, json={"options": MENU[:2]}).json()
    assert [o["action"] for o in r["options"]] == ["products", "deals"] and r["is_enabled"] is False
    r = client.put(URL, json={"greeting": None}).json()
    assert r["greeting"] == "How can we help you?" and len(r["options"]) == 2  # null = back to default


def test_put_replaces_the_whole_menu_and_keeps_order(client, biz):
    _save_menu(client, biz)
    r = client.put(URL, json={"options": [MENU[3], MENU[0]]}).json()
    assert [o["action"] for o in r["options"]] == ["support", "products"]


def test_maximum_five_options(client, biz):
    client.login(biz)
    five = [{"title": f"Opt {i}", "action": f"a{i}"} for i in range(5)]
    assert client.put(URL, json={"options": five}).status_code == 200
    six = five + [{"title": "Opt 5", "action": "a5"}]
    assert client.put(URL, json={"options": six}).status_code == 400
    assert len(client.get(URL).json()["options"]) == 5  # unchanged


@pytest.mark.parametrize("bad", [
    [{"title": "", "action": "x"}],
    [{"title": "x" * 25, "action": "x"}],
    [{"title": "Fine", "action": ""}],
    [{"title": "Fine", "action": "has space"}],
    [{"title": "Fine"}],
    [{"title": "A", "action": "one"}, {"title": "a", "action": "two"}],    # duplicate title
    [{"title": "A", "action": "one"}, {"title": "B", "action": "ONE"}],    # duplicate action
])
def test_invalid_options_rejected(client, biz, bad):
    client.login(biz)
    assert client.put(URL, json={"options": bad}).status_code == 400
    assert client.get(URL).json()["options"] == []


@pytest.mark.parametrize("greeting", ["", "   ", "x" * 1001])
def test_invalid_greeting_rejected(client, biz, greeting):
    client.login(biz)
    assert client.put(URL, json={"greeting": greeting}).status_code == 400


def test_greeting_at_max_length_ok(client, biz):
    client.login(biz)
    assert client.put(URL, json={"greeting": "x" * 1000}).status_code == 200


def test_cannot_enable_without_options(client, biz, db):
    client.login(biz)
    r = client.put(URL, json={"is_enabled": True})
    assert r.status_code == 400 and "at least one option" in r.json()["message"]
    assert db.query(models.ChatDefaultMenu).filter_by(is_enabled=True).count() == 0
    _save_menu(client, biz)
    # emptying the menu while it is on is refused too, and nothing changes
    assert client.put(URL, json={"options": []}).status_code == 400
    assert len(client.get(URL).json()["options"]) == 4
    assert client.put(URL, json={"options": [], "is_enabled": False}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"is_enabled": None}, {"options": None}, {"is_enabled": "maybe"}])
def test_invalid_bodies_rejected(client, biz, body):
    client.login(biz)
    assert client.put(URL, json=body).status_code == 400


def test_normal_user_cannot_use_default_menu(client, customer):
    client.login(customer)
    assert client.get(URL).status_code == 403
    assert client.put(URL, json={"is_enabled": True, "options": MENU}).status_code == 403


def test_requires_authentication(client):
    client.login(None)
    assert client.get(URL).status_code == 401
    assert client.put(URL, json={"is_enabled": False}).status_code == 401


def test_each_account_has_its_own_menu(client, biz, premium):
    _save_menu(client, biz, greeting="Biz greeting")
    client.login(premium)
    assert client.get(URL).json()["options"] == []
    _save_menu(client, premium, options=MENU[:1], greeting="Premium greeting")
    client.login(biz)
    assert client.get(URL).json()["greeting"] == "Biz greeting"


def test_default_menu_route_is_not_shadowed_by_the_id_routes(client, biz, normal=None):
    client.login(biz)
    assert client.get(URL).status_code == 200  # not a 400 from "{conversation_id}" int parsing
    assert client.put(URL, json={"greeting": "Hi there"}).status_code == 200  # not "{option_id}"


# ------------------------------------------------------------------ automatic attach

def test_new_conversation_intro_includes_saved_default_menu(client, biz, customer, convo):
    _save_menu(client, biz, greeting="Welcome! Choose an option")
    cid = convo(customer, biz)
    client.login(customer)
    thread = _messages(client, cid)
    intro = next(m for m in thread if m["is_auto_message"] and m["sender_id"] == biz.id)
    assert intro["content"] == "Welcome! Choose an option"
    assert [(o["title"], o["action"]) for o in intro["options"]] == [
        (o["title"], o["action"]) for o in MENU
    ]

    # The first customer message returns the existing intro as auto_reply,
    # instead of creating a duplicate greeting.
    sent = _send(client, cid, "Hi").json()
    assert sent["auto_reply"]["id"] == intro["id"]
    assert len(_messages(client, cid)) == 2


def test_first_message_gets_the_businesss_menu_automatically(client, biz, customer, convo, db):
    cid = convo(customer, biz)  # also creates the business's usual welcome DM
    _save_menu(client, biz, greeting="Welcome! How can we help?")

    client.login(customer)
    r = _send(client, cid, "Hi")  # a plain "Hi" - no options supplied

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content"] == "Hi" and body["options"] == [] and body["sender_id"] == customer.id
    auto = body["auto_reply"]
    assert auto["sender_id"] == biz.id and auto["is_auto_message"] is True
    assert auto["content"] == "Welcome! How can we help?"
    assert [(o["title"], o["action"], o["is_enabled"]) for o in auto["options"]] == [
        (o["title"], o["action"], True) for o in MENU
    ]
    assert auto["auto_reply"] is None

    # it is a real message in the thread, after the customer's, with buttons on it
    thread = _messages(client, cid)
    texts = [m["content"] for m in thread]
    assert texts.index("Hi") < texts.index("Welcome! How can we help?") or \
        texts.index("Welcome! How can we help?") < texts.index("Hi")  # (order depends on sort direction)
    greeting = next(m for m in thread if m["id"] == auto["id"])
    assert len(greeting["options"]) == 4 and greeting["is_auto_message"] is True
    assert max(m["id"] for m in thread) == auto["id"]


def test_default_greeting_used_when_none_saved(client, biz, customer, convo):
    cid = convo(customer, biz)
    _save_menu(client, biz)  # no greeting
    client.login(customer)
    assert _send(client, cid).json()["auto_reply"]["content"] == "How can we help you?"


def test_customer_can_tap_an_automatic_option_and_business_sees_analytics(client, biz, customer, convo):
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    auto = _send(client, cid).json()["auto_reply"]
    deals = next(o for o in auto["options"] if o["action"] == "deals")

    tap = client.post(f"/api/chat/messages/{auto['id']}/option", json={"option_id": deals["id"]})
    assert tap.status_code == 200
    assert tap.json() == {
        "message_id": auto["id"], "option_id": deals["id"],
        "title": "Today's Deals", "action": "deals", "selected": True,
    }
    client.login(biz)
    assert client.get(f"/api/chat/options/{deals['id']}/analytics").json()["selection_count"] == 1


def test_businesss_unread_count_is_not_cleared_by_the_greeting(client, biz, customer, convo, follow):
    follow(biz, customer)  # so the thread sits in the business's main inbox, not Message Requests
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    _send(client, cid, "Hi")
    client.login(biz)
    convs = client.get("/api/chat/conversations").json()
    mine = next(c for c in (convs["items"] if isinstance(convs, dict) else convs) if c["id"] == cid)
    assert mine["unread_count"] == 1  # the customer's "Hi" is still unread for the business


def test_greeting_does_not_accept_a_pending_message_request(client, biz, customer, convo, db):
    cid = convo(customer, biz)  # business doesn't follow the customer -> pending for the business
    _save_menu(client, biz)
    client.login(customer)
    assert _send(client, cid).json()["auto_reply"] is not None
    row = db.query(models.ConversationParticipant).filter_by(conversation_id=cid, user_id=biz.id).one()
    assert row.status == models.ParticipantStatus.pending


def test_premium_account_menu_is_sent_too(client, premium, customer, convo):
    cid = convo(customer, premium)
    _save_menu(client, premium, options=MENU[:2], greeting="Premium here")
    client.login(customer)
    auto = _send(client, cid).json()["auto_reply"]
    assert auto["content"] == "Premium here" and len(auto["options"]) == 2


def test_sending_media_first_also_triggers_the_menu(client, biz, customer, convo, monkeypatch):
    monkeypatch.setattr(chat_routes, "save_upload_file", lambda f, folder, **kw: ("/static/chat_media/x.jpg", "image"))
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    r = client.post(f"/api/chat/conversations/{cid}/media", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert r.status_code == 201, r.text
    assert r.json()["auto_reply"]["sender_id"] == biz.id and len(r.json()["auto_reply"]["options"]) == 4


# ------------------------------------------------------------------ when it must NOT be sent

def test_no_menu_when_disabled_or_never_saved(client, biz, customer, convo):
    cid = convo(customer, biz)
    client.login(customer)
    assert _send(client, cid, "one").json()["auto_reply"] is None  # nothing saved
    _save_menu(client, biz, enabled=False)
    client.login(customer)
    assert _send(client, cid, "two").json()["auto_reply"] is None  # saved but off


def test_normal_users_never_send_or_receive_menus(client, customer, make_user, convo, db):
    friend = make_user("friend")
    cid = convo(customer, friend)
    client.login(customer)
    r = _send(client, cid)
    assert r.status_code == 201 and r.json()["auto_reply"] is None
    client.login(friend)
    assert _send(client, cid, "yo").json()["auto_reply"] is None
    assert db.query(models.ChatMessageOption).count() == 0


def test_business_messaging_a_customer_does_not_trigger_anything(client, biz, customer, convo, db):
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(biz)
    r = _send(client, cid, "Hello from the shop")
    assert r.status_code == 201 and r.json()["auto_reply"] is None
    assert db.query(models.ChatMessageOption).count() == 0


def test_menu_stops_when_account_is_no_longer_business_or_premium(client, biz, premium, customer, convo, db):
    cid_b = convo(customer, biz)
    _save_menu(client, biz)
    biz.account_type = models.AccountType.personal  # downgraded; menu row still saved + enabled
    db.commit()
    client.login(customer)
    assert _send(client, cid_b).json()["auto_reply"] is None

    cid_p = convo(customer, premium)
    _save_menu(client, premium)
    m = db.query(models.UserMembership).filter_by(user_id=premium.id).one()
    m.status = models.MembershipStatus.canceled
    db.commit()
    client.login(customer)
    assert _send(client, cid_p).json()["auto_reply"] is None


def test_not_sent_again_within_the_inactivity_window(client, biz, customer, convo, db):
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    assert _send(client, cid, "Hi").json()["auto_reply"] is not None
    assert _send(client, cid, "Anyone there?").json()["auto_reply"] is None
    assert _send(client, cid, "Hello??").json()["auto_reply"] is None
    assert db.query(models.ChatMessageOption).filter(
        models.ChatMessageOption.message.has(is_auto_message=True)
    ).count() == 4  # one menu's worth, not three


def test_sent_again_when_the_conversation_has_been_inactive(client, biz, customer, convo, db):
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    _send(client, cid, "Hi")
    _backdate_people_messages(db, cid, hours=svc.DEFAULT_MENU_INACTIVITY_HOURS + 1)
    again = _send(client, cid, "Back again").json()
    assert again["auto_reply"] is not None and len(again["auto_reply"]["options"]) == 4


def test_not_sent_just_inside_the_window(client, biz, customer, convo, db):
    cid = convo(customer, biz)
    _save_menu(client, biz)
    client.login(customer)
    _send(client, cid, "Hi")
    _backdate_people_messages(db, cid, hours=svc.DEFAULT_MENU_INACTIVITY_HOURS - 1)
    assert _send(client, cid, "Still here").json()["auto_reply"] is None


def test_not_sent_when_the_business_replied_recently(client, biz, customer, convo, db):
    cid = convo(customer, biz)
    client.login(customer)
    _send(client, cid, "Hi")  # menu not saved yet -> nothing sent
    _save_menu(client, biz)
    client.login(biz)
    _send(client, cid, "Hi, a human here")  # the business is actively talking
    client.login(customer)
    assert _send(client, cid, "Great, thanks").json()["auto_reply"] is None


def test_group_conversations_never_get_a_menu(client, biz, customer, make_user, convo):
    third = make_user("third")
    cid = convo(customer, biz, third)
    _save_menu(client, biz)
    client.login(customer)
    r = _send(client, cid)
    assert r.status_code == 201 and r.json()["auto_reply"] is None


def test_business_to_business_uses_the_recipients_menu(client, biz, make_user, convo, db):
    other = make_user("otherbiz")
    other.account_type = models.AccountType.business
    db.commit()
    cid = convo(other, biz)
    _save_menu(client, biz, greeting="From bizshop")
    client.login(other)
    assert _send(client, cid).json()["auto_reply"]["content"] == "From bizshop"


def test_conversation_is_inactive_ignores_auto_and_deleted_messages(client, biz, customer, convo, db):
    cid = convo(customer, biz)  # holds only the business's auto-intro
    client.login(customer)
    hi = _send(client, cid, "Hi").json()
    assert svc.conversation_is_inactive(db, cid, hi["id"]) is True  # the auto-intro doesn't count
    second = _send(client, cid, "again").json()
    assert svc.conversation_is_inactive(db, cid, second["id"]) is False
    client.delete(f"/api/chat/messages/{hi['id']}")  # unsent -> no longer counts as activity
    assert svc.conversation_is_inactive(db, cid, second["id"]) is True


# ------------------------------------------------------------------ robustness / realtime

def test_a_failure_in_the_menu_never_breaks_the_customers_send(client, biz, customer, convo, monkeypatch, db):
    cid = convo(customer, biz)
    _save_menu(client, biz)

    def boom(*a, **k):
        raise RuntimeError("menu exploded")

    monkeypatch.setattr(svc, "active_default_menu", boom)
    client.login(customer)
    r = _send(client, cid, "Hi")
    assert r.status_code == 201 and r.json()["auto_reply"] is None and r.json()["content"] == "Hi"
    assert any(m["content"] == "Hi" for m in _messages(client, cid))


def test_greeting_is_pushed_live_to_both_sides(client, biz, customer, convo, monkeypatch):
    sent = []

    async def fake_send(user_ids, payload):
        sent.append((list(user_ids), payload))

    monkeypatch.setattr(chat_routes.manager, "send_to_users", fake_send)
    cid = convo(customer, biz)
    _save_menu(client, biz, greeting="Live greeting")
    client.login(customer)
    _send(client, cid, "Hi")

    greetings = [(ids, p) for ids, p in sent if p["message"].get("content") == "Live greeting"]
    assert sorted(ids[0] for ids, _ in greetings) == sorted([customer.id, biz.id])
    for ids, payload in greetings:
        assert payload["type"] == "message" and len(payload["message"]["options"]) == 4


# ------------------------------------------------------------------ existing behaviour, docs, migration

def test_explicit_options_still_work(client, biz, customer, convo):
    cid = convo(biz, customer)
    client.login(biz)
    sent = _send(client, cid, "Pick one", options=MENU[:2]).json()
    assert [o["action"] for o in sent["options"]] == ["products", "deals"] and sent["auto_reply"] is None
    mid = sent["id"]
    added = client.post("/api/chat/options", json={"message_id": mid, "options": [MENU[2]]})
    assert added.status_code == 201 and len(added.json()["options"]) == 3
    assert client.get(f"/api/chat/options/{cid}").status_code == 200  # int route still resolves


def test_ordinary_chat_unchanged_without_a_menu(client, customer, make_user, convo):
    friend = make_user("friend")
    cid = convo(customer, friend)
    client.login(customer)
    r = _send(client, cid, "hello")
    assert r.status_code == 201 and r.json()["options"] == [] and r.json()["auto_reply"] is None


def test_swagger_documents_default_menu(client):
    spec = client.get("/openapi.json").json()
    path = spec["paths"][URL]
    for method in ("get", "put"):
        assert path[method]["summary"] and path[method]["description"] and "403" in path[method]["responses"]
    assert "400" in path["put"]["responses"]
    schemas_ = spec["components"]["schemas"]
    assert {"is_enabled", "greeting", "options", "inactivity_hours"} <= schemas_["ChatDefaultMenuOut"]["properties"].keys()
    assert "auto_reply" in schemas_["MessageOut"]["properties"]
    assert "auto_reply" in schemas_["MediaMessageResponse"]["properties"]


def test_migration_creates_table_and_is_idempotent():
    engine = create_engine("sqlite://")
    tables = [t for t in models.Base.metadata.sorted_tables if t.name != "chat_default_menus"]
    models.Base.metadata.create_all(engine, tables=tables)
    assert migrate(engine) == ["chat_default_menus created"]
    assert migrate(engine) == []
