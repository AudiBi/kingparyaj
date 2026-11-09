# tests/test_api/test_admin_other_lists.py
"""Bureaux, promotions, transactions, journal d'audit, KYC et recherche globale du panel admin."""

import csv
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from sqlalchemy import select

from app.core.timezone import now_utc
from app.models.audit import AuditAction, AuditLog
from app.models.bureau import Bureau
from app.models.enums import PromotionType
from app.models.promotion import Promotion, PromotionStatus, UserPromotion
from app.models.user import KYCStatus, User

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))


async def _promotion(db_session, name="Bonus", code=None, claims=0) -> Promotion:
    promo = Promotion(
        name=name, code=code, type=PromotionType.DEPOSIT_BONUS, config={}, status=PromotionStatus.DRAFT,
        start_date=now_utc(), end_date=now_utc() + timedelta(days=30), created_by="admin", total_claims=claims,
    )
    db_session.add(promo)
    await db_session.commit()
    return promo


# ======================================================================== bureaux

@pytest.mark.asyncio
async def test_bureaus_list_filters_and_export(panel, db_session, make_bureau):
    bureau = await make_bureau()
    bureau.city = "Cap-Haïtien"
    await db_session.commit()
    code, bid = bureau.code, bureau.id
    for query in ("search=Test", "city=Cap-Ha%C3%AFtien", "status=active", "status=inactive", "page=99", "per_page=100"):
        assert (await panel.get(f"/admin/bureaus?{query}")).status_code == 200, query
    assert code in (await panel.get("/admin/bureaus?search=Test")).text
    rows = _rows(await panel.get("/admin/api/bureaus/export?status=active"))
    assert rows[0][0] == "Code" and code in {r[0] for r in rows[1:]}
    assert [r[0] for r in _rows(await panel.get(f"/admin/api/bureaus/export?ids={bid}"))[1:]] == [code]
    assert (await panel.get("/admin/api/bureaus/statistics")).status_code == 200


@pytest.mark.asyncio
async def test_bureau_bulk_toggle_and_delete_protections(panel, db_session, make_bureau, make_agent):
    empty = await make_bureau()
    busy = await make_bureau()
    await make_agent(busy)  # un agent rattaché : suppression refusée
    cash = await make_bureau(Decimal("500"))
    await db_session.commit()
    empty_id, busy_id, cash_id = empty.id, busy.id, cash.id

    off = await panel.post("/admin/api/bureaus/bulk/deactivate", json={"bureau_ids": [empty_id, busy_id]})
    assert off.status_code == 200 and off.json()["done"] == 2, off.text
    await db_session.refresh(empty)
    assert empty.is_active is False
    assert (await panel.post("/admin/api/bureaus/bulk/activate", json={"bureau_ids": [empty_id]})).status_code == 200

    blocked = await panel.post("/admin/api/bureaus/bulk/delete", json={"bureau_ids": [busy_id, cash_id]})
    assert blocked.status_code == 400 and "agent(s) rattaché(s)" in blocked.json()["error"]
    one = await panel.delete(f"/admin/api/bureaus/{busy_id}")
    assert one.status_code == 400 and "Suppression impossible" in one.json()["error"]
    assert (await panel.delete(f"/admin/api/bureaus/{cash_id}")).status_code == 400  # caisse non vide

    mixed = await panel.post("/admin/api/bureaus/bulk/delete", json={"bureau_ids": [empty_id, busy_id]})
    assert mixed.status_code == 200 and mixed.json()["done"] == 1 and len(mixed.json()["skipped"]) == 1
    await db_session.refresh(empty)
    assert empty.is_deleted is True
    assert (await panel.post("/admin/api/bureaus/bulk/zzz", json={"bureau_ids": [busy_id]})).status_code == 400
    assert (await panel.delete("/admin/api/bureaus/inconnu")).status_code == 404


# ======================================================================== promotions

@pytest.mark.asyncio
async def test_promotions_list_filters_and_bad_values(panel, db_session):
    promo = await _promotion(db_session, "Bienvenue", "BIEN10")
    name = promo.name
    for query in ("status=active", "status=zzz", "type=deposit_bonus", "type=zzz", "search=Bienvenue",
                  "start_date=2020-01-01&end_date=2099-12-31"):
        assert (await panel.get(f"/admin/promotions?{query}")).status_code == 200, query
    assert name in (await panel.get("/admin/promotions?search=Bienvenue")).text
    assert (await panel.get("/admin/promotions?start_date=pas-une-date")).status_code == 400


@pytest.mark.asyncio
async def test_promotions_bulk_and_delete(panel, db_session):
    fresh = await _promotion(db_session, "Neuve", "NEW1")
    used = await _promotion(db_session, "Utilisée", "USED1", claims=3)
    fresh_id, used_id = fresh.id, used.id

    on = await panel.post("/admin/api/promotions/bulk/activate", json={"promo_ids": [fresh_id, used_id]})
    assert on.status_code == 200, on.text
    await db_session.refresh(fresh)
    assert fresh.status == PromotionStatus.ACTIVE
    assert (await panel.post("/admin/api/promotions/bulk/pause", json={"promo_ids": [fresh_id]})).status_code == 200

    refused = await panel.delete(f"/admin/api/promotions/{used_id}")
    assert refused.status_code == 400 and "Mettez-la en pause" in refused.json()["error"]
    mixed = await panel.post("/admin/api/promotions/bulk/delete", json={"promo_ids": [fresh_id, used_id]})
    assert mixed.status_code == 200 and mixed.json()["done"] == 1
    assert (await db_session.execute(select(Promotion).where(Promotion.id == fresh_id))).scalar_one_or_none() is None
    assert (await db_session.execute(select(Promotion).where(Promotion.id == used_id))).scalar_one_or_none() is not None
    assert (await panel.delete("/admin/api/promotions/inconnu")).status_code == 404


# ======================================================================== transactions

@pytest.mark.asyncio
async def test_transactions_list_and_export_filters(panel):
    for query in ("transaction_type=deposit", "transaction_type=zzz", "status=completed", "status=zzz", "method=cash",
                  "method=zzz", "min_amount=10&max_amount=500", "search=ABC", "page=99",
                  "start_date=2020-01-01&end_date=2099-12-31"):
        assert (await panel.get(f"/admin/transactions?{query}")).status_code == 200, query
    assert (await panel.get("/admin/transactions?start_date=oups")).status_code == 400
    for query in ("", "type=deposit", "type=zzz", "status=completed", "status=zzz", "method=zzz"):
        assert (await panel.get(f"/admin/api/transactions/export?{query}")).status_code == 200, query
    assert (await panel.get("/admin/api/transactions/statistics")).status_code == 200
    assert (await panel.get("/admin/api/transactions/inconnue")).status_code == 404


# ======================================================================== journal d'audit

@pytest.mark.asyncio
async def test_audit_list_filters_export_and_leh(panel, db_session, admin_user):
    log = AuditLog(user_id=admin_user.id, action=AuditAction.USER_UPDATED, resource_type="user", resource_id="r1",
                   ip_address="1.2.3.4", reason="=SOMME(A1)", new_values={"a": 1})
    db_session.add(log)
    await db_session.commit()
    log_id = log.id

    for query in ("action=USER_UPDATED", "action=zzz", "resource_type=user", "leh_exported=false", "ip_address=1.2.3.4",
                  "search=r1", "start_date=2020-01-01&end_date=2099-12-31", "page=99"):
        assert (await panel.get(f"/admin/audit/logs?{query}")).status_code == 200, query
    assert (await panel.get("/admin/audit/logs?start_date=oups")).status_code == 400

    rows = _rows(await panel.get("/admin/api/audit/export?action=zzz&search=r1"))
    assert any(r[0] == log_id for r in rows[1:])
    reason = next(r for r in rows[1:] if r[0] == log_id)[8]
    assert reason.startswith("'")  # injection de formule neutralisée
    assert len(_rows(await panel.get(f"/admin/api/audit/export?ids={log_id}"))) == 2

    assert (await panel.get(f"/admin/api/audit/logs/{log_id}")).status_code == 200
    assert (await panel.post(f"/admin/api/audit/logs/{log_id}/leh-export")).status_code == 200
    bulk = await panel.post("/admin/api/audit/logs/bulk/leh-export", json={"log_ids": [log_id]})
    assert bulk.status_code == 200 and "exporté" in bulk.json()["message"]
    assert (await panel.get("/admin/api/audit/statistics")).status_code == 200

    # « Export LEH » de la page : POST avec la période
    leh = await panel.post("/admin/api/audit/export?start_date=2020-01-01&end_date=2099-12-31")
    assert leh.status_code == 200 and leh.headers["content-type"].startswith("text/csv")


# ======================================================================== KYC

@pytest.mark.asyncio
async def test_kyc_documents_upload_view_delete(panel, db_session, make_user, tmp_path, monkeypatch):
    monkeypatch.setenv("KYC_UPLOAD_DIR", str(tmp_path))
    player = await make_user()
    await db_session.commit()
    uid = player.id

    assert (await panel.get(f"/admin/users/{uid}/kyc")).status_code == 200
    ok = await panel.post(f"/admin/api/users/{uid}/kyc/upload", data={"doc_type": "id", "comment": "recto"},
                          files={"file": ("carte.png", PNG, "image/png")})
    assert ok.status_code == 200, ok.text
    assert "carte.png" in (await panel.get(f"/admin/users/{uid}/kyc")).text

    await db_session.refresh(player)
    import json
    doc_id = json.loads(player.kyc_documents)[0]["id"]
    view = await panel.get(f"/admin/users/{uid}/kyc/document/{doc_id}")
    assert view.status_code == 200 and view.headers["content-type"] == "image/png"

    bad_type = await panel.post(f"/admin/api/users/{uid}/kyc/upload", data={"doc_type": "zzz"},
                                files={"file": ("a.png", PNG, "image/png")})
    assert bad_type.status_code == 400
    fake = await panel.post(f"/admin/api/users/{uid}/kyc/upload", data={"doc_type": "id"},
                            files={"file": ("virus.png", b"MZ" + b"0" * 50, "image/png")})
    assert fake.status_code == 400 and "Format non accepté" in fake.json()["error"]
    big = await panel.post(f"/admin/api/users/{uid}/kyc/upload", data={"doc_type": "id"},
                           files={"file": ("big.png", PNG + b"0" * (5 * 1024 * 1024), "image/png")})
    assert big.status_code == 400 and "volumineux" in big.json()["error"]

    gone = await panel.delete(f"/admin/api/users/{uid}/kyc/document/{doc_id}")
    assert gone.status_code == 200
    assert (await panel.get(f"/admin/users/{uid}/kyc/document/{doc_id}")).status_code == 404
    assert (await panel.delete(f"/admin/api/users/{uid}/kyc/document/{doc_id}")).status_code == 404


@pytest.mark.asyncio
async def test_kyc_upload_reports_unwritable_storage(panel, db_session, make_user, tmp_path, monkeypatch):
    """Disque non inscriptible : message clair (503) et aucun document enregistré."""
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    monkeypatch.setenv("KYC_UPLOAD_DIR", str(blocker / "kyc"))  # un fichier à la place du dossier
    player = await make_user()
    await db_session.commit()
    uid = player.id
    r = await panel.post(f"/admin/api/users/{uid}/kyc/upload", data={"doc_type": "id"},
                         files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 503 and "Stockage des documents indisponible" in r.json()["error"]
    await db_session.refresh(player)
    assert not player.kyc_documents


@pytest.mark.asyncio
async def test_kyc_verify_reject_reset_remind(panel, db_session, make_user):
    player = await make_user()
    await db_session.commit()
    uid = player.id

    assert (await panel.post(f"/admin/api/users/{uid}/kyc/remind")).status_code == 200
    assert (await panel.post(f"/admin/api/users/{uid}/kyc/reject", json={"reason": "Photo floue"})).status_code == 200
    await db_session.refresh(player)
    assert player.kyc_status == KYCStatus.REJECTED
    assert (await panel.post(f"/admin/api/users/{uid}/kyc/verify")).status_code == 200
    await db_session.refresh(player)
    assert player.kyc_status == KYCStatus.VERIFIED
    done = await panel.post(f"/admin/api/users/{uid}/kyc/remind")
    assert done.status_code == 400 and "déjà validé" in done.json()["error"]
    assert (await panel.post(f"/admin/api/users/{uid}/kyc/reset")).status_code == 200
    await db_session.refresh(player)
    assert player.kyc_status == KYCStatus.PENDING
    assert (await panel.post("/admin/api/users/inconnu/kyc/remind")).status_code == 404


@pytest.mark.asyncio
async def test_block_and_unblock_user_from_the_edit_page(panel, db_session, make_user):
    player = await make_user()
    await db_session.commit()
    uid = player.id
    assert (await panel.post(f"/admin/api/users/{uid}/block", json={"reason": "Fraude"})).status_code == 200
    await db_session.refresh(player)
    assert player.is_active is False and player.lock_reason == "Fraude"
    assert (await panel.post(f"/admin/api/users/{uid}/unblock")).status_code == 200
    await db_session.refresh(player)
    assert player.is_active is True


# ======================================================================== recherche globale

@pytest.mark.asyncio
async def test_global_search(panel, db_session, make_agent, make_ticket, make_bureau):
    agent = await make_agent()
    info = await make_ticket(agent, balance=Decimal("10"))
    await db_session.commit()
    number = info["ticket_number"]
    bureau = await db_session.get(Bureau, agent.bureau_id)
    code = bureau.code

    page = await panel.get(f"/admin/search?q={number}")
    assert page.status_code == 200 and number in page.text
    assert code in (await panel.get(f"/admin/search?q={code}")).text
    assert "Aucun résultat" in (await panel.get("/admin/search?q=zzzzzzzz")).text
    assert "au moins 2" in (await panel.get("/admin/search?q=a")).text
    assert (await panel.get("/admin/search")).status_code == 200
