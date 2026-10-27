"""Push notification tests. We have no real VAPID/push-service credentials
in CI, so these monkeypatch push_client.send_push with a small fake push
service — this still proves the actual logic (a subscription is notified
when a job is scheduled/reassigned to its driver, a dead subscription gets
cleaned up, and a push failure never blocks the scheduling action that
triggered it), just not the real network round-trip, same pattern as the
Traccar/Xero integration tests."""
from app import crud, models, push_client


def _confirmed_order(db):
    customer = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    return crud.save_order(db, {
        "customer_id": customer.customer_id, "project": "Test job", "site_address": "1 Test Rd",
        "requested_date": "2026-09-10", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")


def test_configured_false_without_vapid_keys():
    assert push_client.is_configured() is False


def test_subscribe_then_notify_sends_to_that_subscription(db, monkeypatch):
    monkeypatch.setattr(push_client, "VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setattr(push_client, "VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setattr(push_client, "VAPID_CLAIM_EMAIL", "test@example.com")

    sent = []

    def fake_send_push(endpoint, p256dh, auth, title, body, url="/driver"):
        sent.append((endpoint, title, body))
        return push_client.PushResult(sent=True)

    monkeypatch.setattr(push_client, "send_push", fake_send_push)

    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "p256dh-key", "auth-key")
    db.commit()

    crud.notify_driver_new_job(db, driver.user_id, "New job assigned", "Test Trade Ltd — Test job")

    assert len(sent) == 1
    assert sent[0][0] == "https://push.example/dan-tablet"
    assert sent[0][1] == "New job assigned"


def test_notify_is_a_silent_noop_when_not_configured(db, monkeypatch):
    # VAPID keys are blank by default in tests — is_configured() is False.
    sent = []
    monkeypatch.setattr(push_client, "send_push", lambda *a, **k: sent.append(1))

    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "p256dh-key", "auth-key")
    db.commit()

    crud.notify_driver_new_job(db, driver.user_id, "New job assigned", "Test Trade Ltd — Test job")

    assert sent == []  # never even tried, since push isn't configured


def test_expired_subscription_is_deleted_after_a_failed_push(db, monkeypatch):
    monkeypatch.setattr(push_client, "VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setattr(push_client, "VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setattr(push_client, "VAPID_CLAIM_EMAIL", "test@example.com")
    monkeypatch.setattr(push_client, "send_push", lambda *a, **k: push_client.PushResult(sent=False, expired=True))

    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-old-phone", "p256dh-key", "auth-key")
    db.commit()

    crud.notify_driver_new_job(db, driver.user_id, "New job assigned", "Test Trade Ltd — Test job")

    remaining = db.query(models.PushSubscription).filter_by(driver_user_id=driver.user_id).all()
    assert remaining == []


def test_a_broken_push_never_blocks_scheduling_a_delivery(db, monkeypatch):
    monkeypatch.setattr(push_client, "VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setattr(push_client, "VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setattr(push_client, "VAPID_CLAIM_EMAIL", "test@example.com")

    def exploding_send_push(*args, **kwargs):
        raise RuntimeError("simulated push-service outage")

    monkeypatch.setattr(push_client, "send_push", exploding_send_push)

    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "p256dh-key", "auth-key")
    db.commit()

    # Should not raise, even though send_push blows up internally.
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)
    db.commit()
    assert delivery.driver_user_id == driver.user_id


def test_create_delivery_notifies_assigned_driver(db, monkeypatch):
    monkeypatch.setattr(push_client, "VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setattr(push_client, "VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setattr(push_client, "VAPID_CLAIM_EMAIL", "test@example.com")

    notified = []
    monkeypatch.setattr(crud, "notify_driver_new_job", lambda db, driver_user_id, title, body: notified.append((driver_user_id, title, body)))

    order = _confirmed_order(db)
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id)

    assert len(notified) == 1
    assert notified[0][0] == driver.user_id
    assert "Test Trade Ltd" in notified[0][2]


def test_reassign_delivery_notifies_the_new_driver_not_the_old_one(db, monkeypatch):
    monkeypatch.setattr(push_client, "VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setattr(push_client, "VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setattr(push_client, "VAPID_CLAIM_EMAIL", "test@example.com")

    notified = []
    monkeypatch.setattr(crud, "notify_driver_new_job", lambda db, driver_user_id, title, body: notified.append(driver_user_id))

    order = _confirmed_order(db)
    dan = crud.create_driver(db, "Dan Driver", "dan", "4821")
    sam = crud.create_driver(db, "Sam Driver", "sam", "1234")
    db.commit()

    delivery = crud.create_delivery(db, order.order_id, driver_user_id=dan.user_id)
    db.commit()
    notified.clear()

    crud.reassign_delivery(db, delivery.delivery_id, driver_user_id=sam.user_id, vehicle_id=None)

    assert notified == [sam.user_id]


def test_save_push_subscription_upserts_by_endpoint(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()

    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "key-1", "auth-1")
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "key-2", "auth-2")
    db.commit()

    rows = db.query(models.PushSubscription).filter_by(endpoint="https://push.example/dan-tablet").all()
    assert len(rows) == 1
    assert rows[0].p256dh == "key-2"


def test_remove_push_subscription(db):
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    db.commit()
    crud.save_push_subscription(db, driver.user_id, "https://push.example/dan-tablet", "key", "auth")
    db.commit()

    crud.remove_push_subscription(db, "https://push.example/dan-tablet")
    db.commit()

    assert db.query(models.PushSubscription).count() == 0
