"""ETA calculation — mocks routing_client's HTTP calls (no live network
access to the public OSRM/Nominatim servers from this environment), which
proves the caching, gating, and best-effort logic without needing that
access. The live network call itself still needs a real-world check once
deployed, same caveat as the POD map images."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import crud, routing_client


def _confirmed_order_with_vehicle(db, site_address="1 Test Rd, Leicester"):
    customer = crud.save_customer(db, {"customer_type": "Commercial", "display_name": "Test Trade Ltd"})
    material = crud.save_material(db, {
        "code": "CEMENT", "name": "Cement", "unit": "kg", "on_hand": 5000,
        "reorder_level": 1000, "reorder_quantity": 2000, "unit_cost": 0.12, "supplier": "Test",
    })
    product = crud.save_product(db, {
        "code": "C30", "name": "C30 Concrete", "description": "", "sell_unit": "m³", "default_unit_price": 142.345,
    }, [{"material_id": material.material_id, "quantity_per_unit": 300, "waste_percent": 2}])
    order = crud.save_order(db, {
        "customer_id": customer.customer_id, "project": "Test job", "site_address": site_address,
        "requested_date": "2026-09-10", "status": "Confirmed", "tax_rate": 20,
    }, [{"product_id": product.product_id, "description": "C30", "quantity": 5, "unit": "m³", "unit_price": 142.345}], "admin")
    driver = crud.create_driver(db, "Dan Driver", "dan", "4821")
    vehicle = crud.save_vehicle(db, "TC01")
    db.flush()
    delivery = crud.create_delivery(db, order.order_id, driver_user_id=driver.user_id, vehicle_id=vehicle.vehicle_id)
    delivery.status = "En Route"
    return order, delivery, vehicle


@pytest.fixture()
def fake_routing(monkeypatch):
    state = {"geocode_result": (52.6336, -1.1362), "eta_seconds": 720, "fail": False}

    def fake_geocode(address):
        if state["fail"]:
            return None
        return state["geocode_result"]

    def fake_eta(origin_lat, origin_lon, dest_lat, dest_lon):
        if state["fail"]:
            return None
        return state["eta_seconds"]

    monkeypatch.setattr(routing_client, "geocode_address", fake_geocode)
    monkeypatch.setattr(routing_client, "get_eta_seconds", fake_eta)
    return state


def test_geocode_is_cached_on_the_order(db, fake_routing):
    order, _delivery, _vehicle = _confirmed_order_with_vehicle(db)
    db.commit()
    assert order.site_latitude is None

    result = crud.get_or_geocode_site_location(db, order)
    db.commit()
    assert result == (52.6336, -1.1362)
    assert float(order.site_latitude) == pytest.approx(52.6336, abs=0.0001)


def test_geocode_second_call_does_not_hit_the_network_again(db, fake_routing):
    """Confirms the cache is actually used — changing the fake's return
    value after the first call must not affect the second."""
    order, _delivery, _vehicle = _confirmed_order_with_vehicle(db)
    db.commit()
    crud.get_or_geocode_site_location(db, order)
    db.commit()

    fake_routing["geocode_result"] = (0.0, 0.0)  # would be very wrong if re-fetched
    result = crud.get_or_geocode_site_location(db, order)
    assert result == pytest.approx((52.6336, -1.1362), abs=0.0001)


def test_refresh_etas_updates_en_route_delivery_with_a_positioned_vehicle(db, fake_routing):
    order, delivery, vehicle = _confirmed_order_with_vehicle(db)
    vehicle.last_latitude = 52.6000
    vehicle.last_longitude = -1.1000
    vehicle.last_position_at = datetime.now(timezone.utc)
    db.commit()

    updated = crud.refresh_etas_for_active_deliveries(db)
    db.commit()

    assert updated == 1
    assert delivery.eta_minutes == 12  # 720 seconds
    assert delivery.eta_updated_at is not None


def test_refresh_etas_skips_delivery_with_no_vehicle_position(db, fake_routing):
    _order, delivery, vehicle = _confirmed_order_with_vehicle(db)
    # vehicle never reported a position
    db.commit()

    updated = crud.refresh_etas_for_active_deliveries(db)
    assert updated == 0
    assert delivery.eta_minutes is None


def test_refresh_etas_skips_stale_vehicle_position(db, fake_routing):
    """A vehicle that hasn't reported in a while shouldn't produce a
    misleading ETA computed from an outdated position."""
    _order, delivery, vehicle = _confirmed_order_with_vehicle(db)
    vehicle.last_latitude = 52.6000
    vehicle.last_longitude = -1.1000
    vehicle.last_position_at = datetime.now(timezone.utc) - timedelta(minutes=25)
    db.commit()

    updated = crud.refresh_etas_for_active_deliveries(db)
    assert updated == 0
    assert delivery.eta_minutes is None


def test_refresh_etas_skips_non_en_route_deliveries(db, fake_routing):
    _order, delivery, vehicle = _confirmed_order_with_vehicle(db)
    delivery.status = "Scheduled"  # not yet En Route
    vehicle.last_latitude = 52.6000
    vehicle.last_longitude = -1.1000
    vehicle.last_position_at = datetime.now(timezone.utc)
    db.commit()

    updated = crud.refresh_etas_for_active_deliveries(db)
    assert updated == 0


def test_refresh_etas_never_raises_on_routing_failure(db, fake_routing):
    """A geocoding or routing failure for one delivery must not crash the
    whole batch — same best-effort principle as the Xero/Traccar syncs."""
    _order, delivery, vehicle = _confirmed_order_with_vehicle(db)
    vehicle.last_latitude = 52.6000
    vehicle.last_longitude = -1.1000
    vehicle.last_position_at = datetime.now(timezone.utc)
    db.commit()
    fake_routing["fail"] = True

    updated = crud.refresh_etas_for_active_deliveries(db)  # must not raise
    assert updated == 0
    assert delivery.eta_minutes is None


def test_todays_jobs_includes_eta(db, fake_routing):
    order, delivery, vehicle = _confirmed_order_with_vehicle(db, site_address="1 Test Rd, Leicester")
    order.requested_date = "2026-09-10"
    vehicle.last_latitude = 52.6000
    vehicle.last_longitude = -1.1000
    vehicle.last_position_at = datetime.now(timezone.utc)
    db.commit()

    crud.refresh_etas_for_active_deliveries(db)
    db.commit()

    jobs = crud.todays_jobs(db, "2026-09-10")
    assert len(jobs) == 1
    assert jobs[0]["eta_minutes"] == 12
    assert jobs[0]["status"] == "En Route"


def test_todays_jobs_eta_is_none_when_not_yet_calculated(db, fake_routing):
    order, _delivery, _vehicle = _confirmed_order_with_vehicle(db)
    order.requested_date = "2026-09-10"
    db.commit()

    jobs = crud.todays_jobs(db, "2026-09-10")
    assert jobs[0]["eta_minutes"] is None
