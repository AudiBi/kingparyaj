# tests/test_api/test_csrf_stable_token.py
"""Régression : le jeton CSRF d'une page doit rester valable après d'autres
appels (avant, il était renouvelé à chaque réponse et les paris Keno/Lucky
envoyés après le rafraîchissement du solde de caisse étaient refusés)."""

import re

import pytest


def _token(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_page_token_survives_other_requests(admin_client):
    page = await admin_client.get("/admin/login")
    token = _token(page.text)

    for _ in range(3):  # ex. rafraîchissements en arrière-plan
        again = await admin_client.get("/admin/login")
        assert _token(again.text) == token  # même jeton, cookie re-signé

    response = await admin_client.post("/admin/_test-protected", headers={"X-CSRFToken": token}, follow_redirects=False)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_wrong_or_missing_token_still_refused(admin_client):
    await admin_client.get("/admin/login")
    for headers in ({}, {"X-CSRFToken": "faux-jeton"}):
        response = await admin_client.post("/admin/_test-protected", headers=headers, follow_redirects=False)
        assert response.status_code == 303 and "csrf_error=1" in response.headers["location"]


@pytest.mark.asyncio
async def test_new_visitor_gets_a_different_token(admin_client):
    first = _token((await admin_client.get("/admin/login")).text)
    admin_client.cookies.clear()
    second = _token((await admin_client.get("/admin/login")).text)
    assert first != second
