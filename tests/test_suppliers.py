"""The standalone Supplier directory — separate from Material.supplier's
free-text field — with an admin-managed Group option list (raw materials,
vehicle parts & servicing, etc)."""
from app import crud


def test_save_and_edit_supplier_in_place(db):
    supplier = crud.save_supplier(db, {
        "name": "Aggregate Supplies Ltd", "group": "Raw Materials",
        "contact_name": "Pat", "telephone": "01234 567890", "email": "pat@example.com",
        "address": "1 Quarry Road", "notes": "Account #4821",
    })
    db.commit()
    assert supplier.supplier_id is not None

    edited = crud.save_supplier(db, {
        "name": "Aggregate Supplies Ltd (renamed)", "group": "Vehicle Parts & Servicing",
        "contact_name": "Pat", "telephone": "01234 567890", "email": "pat@example.com",
        "address": "1 Quarry Road", "notes": "Account #4821",
    }, supplier_id=supplier.supplier_id)
    db.commit()

    assert edited.supplier_id == supplier.supplier_id
    assert edited.name == "Aggregate Supplies Ltd (renamed)"
    assert edited.group == "Vehicle Parts & Servicing"


def test_list_suppliers_excludes_deactivated(db):
    a = crud.save_supplier(db, {"name": "Supplier A", "group": ""})
    b = crud.save_supplier(db, {"name": "Supplier B", "group": ""})
    db.commit()
    assert {s.supplier_id for s in crud.list_suppliers(db)} == {a.supplier_id, b.supplier_id}

    crud.deactivate_supplier(db, a.supplier_id)
    db.commit()
    assert {s.supplier_id for s in crud.list_suppliers(db)} == {b.supplier_id}
    assert db.get(crud.models.Supplier, a.supplier_id) is not None  # row kept, just inactive


def test_supplier_group_options_add_and_remove(db):
    assert crud.supplier_group_options(db) == []

    crud.add_supplier_group_option(db, "Raw Materials")
    crud.add_supplier_group_option(db, "Vehicle Parts & Servicing")
    db.commit()
    names = [o.name for o in crud.supplier_group_options(db)]
    assert names == ["Raw Materials", "Vehicle Parts & Servicing"]

    option = crud.supplier_group_options(db)[0]
    crud.remove_supplier_group_option(db, option.option_id)
    db.commit()
    assert [o.name for o in crud.supplier_group_options(db)] == ["Vehicle Parts & Servicing"]
    assert len(crud.supplier_group_options(db, active_only=False)) == 2  # row kept
