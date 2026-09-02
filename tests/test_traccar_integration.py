"""Traccar integration tests. No live Traccar server to test against yet,
so these monkeypatch traccar_client's HTTP calls with fake device/position
data — this proves the actual matching logic (Traccar's internal device id
vs. the human-friendly identifier typed into Traccar Client) and that a
Traccar outage never breaks anything else, ahead of having a real server
to point at."""
from datetime import datetime, timezone

import pytest

from app import crud, traccar_client


@pytest.fixture()
def fake_traccar(monkeypatch):
    state = {"devices": [], "positions": [], "fail": False}

    def fake_get_devices(base_url, username, password):
        if state["fail"]:
            raise traccar_client.TraccarError("simulated outage")
        return state["devices"]

    def fake_get_positions(base_url, username, password):
        if state["fail"]:
            raise traccar_client.TraccarError("simulated outage")
        return state["positions"]

    monkeypatch.setattr(traccar_client, "get_devices", fake_get_devices)
    monkeypatch.setattr(traccar_client, "get_positions", fake_get_positions)
    return state


def test_sync_matches_device_by_unique_id_not_internal_id(db, fake_traccar):
    """Positions are keyed by Traccar's internal numeric id, not the
    friendly identifier (e.g. 'TC01') typed into Traccar Client — this is
    the trickiest part of the integration to get right."""
    vehicle = crud.save_vehicle(db, "TC01", "8-wheel mixer", traccar_device_id="TC01")
    db.commit()

    fake_traccar["devices"] = [{"id": 42, "uniqueId": "TC01"}]
    fake_traccar["positions"] = [{"deviceId": 42, "latitude": 52.6336, "longitude": -1.1362, "fixTime": "2026-09-01T08:30:00Z"}]

    updated = crud.sync_vehicle_positions(db, "https://traccar.example.com", "user", "pass")
    db.commit()

    assert updated == 1
    assert float(vehicle.last_latitude) == pytest.approx(52.6336, abs=0.0001)
    assert float(vehicle.last_longitude) == pytest.approx(-1.1362, abs=0.0001)
    assert vehicle.last_position_at.year == 2026


def test_sync_ignores_vehicles_without_a_traccar_device_id(db, fake_traccar):
    crud.save_vehicle(db, "TC02", "6-wheel mixer")  # no traccar_device_id set
    db.commit()

    fake_traccar["devices"] = [{"id": 1, "uniqueId": "SOMETHING-ELSE"}]
    fake_traccar["positions"] = [{"deviceId": 1, "latitude": 1.0, "longitude": 1.0, "fixTime": "2026-09-01T08:30:00Z"}]

    updated = crud.sync_vehicle_positions(db, "https://traccar.example.com", "user", "pass")
    assert updated == 0


def test_sync_ignores_devices_with_no_matching_vehicle(db, fake_traccar):
    crud.save_vehicle(db, "TC01", traccar_device_id="TC01")
    db.commit()

    fake_traccar["devices"] = [{"id": 99, "uniqueId": "SOME-OTHER-TRUCK"}]
    fake_traccar["positions"] = [{"deviceId": 99, "latitude": 1.0, "longitude": 1.0, "fixTime": "2026-09-01T08:30:00Z"}]

    updated = crud.sync_vehicle_positions(db, "https://traccar.example.com", "user", "pass")
    assert updated == 0


def test_sync_never_raises_on_traccar_outage(db, fake_traccar):
    """A Traccar server being down (or not set up yet) must never crash
    anything else in the app — same principle as the Xero push functions."""
    crud.save_vehicle(db, "TC01", traccar_device_id="TC01")
    db.commit()
    fake_traccar["fail"] = True

    updated = crud.sync_vehicle_positions(db, "https://traccar.example.com", "user", "pass")
    assert updated == 0  # no exception raised


def test_vehicle_can_be_created_without_traccar_at_all(db, fake_traccar):
    """Adding a vehicle with no Traccar device id at all — the everyday
    case before tracking is set up — must work exactly as before."""
    vehicle = crud.save_vehicle(db, "tc03", "small mixer")
    db.commit()
    assert vehicle.traccar_device_id is None
    assert vehicle.last_latitude is None
