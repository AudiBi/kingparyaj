# tests/test_api/test_admin_navigation.py
"""
Navigation des panels admin / agent (« allers-retours » dans les menus).

Régressions couvertes, trouvées en parcourant tous les liens avec un navigateur :
- le lien « Tableau de bord » du menu menait au JSON de l'API (nom de route en double)
- les scripts de 29 pages s'exécutaient deux fois (bloc extra_js imbriqué dans content)
- `{{ x|tojson }}` produisait des &#34; et cassait le JavaScript (rapports, statistiques)
- des liens pointaient vers des routes JSON (/admin/api/...) au lieu des pages
- /admin/users/create : template manquant (500)
"""

import glob
import re

import pytest

from tests.test_api.test_admin_pages_smoke import _install_fake_keno_counts, _login


def _view_routes():
    import app.routes.admin as admin_views
    import app.routes.agent as agent_views
    import app.routes.horse_races as horse_views

    routes = {}
    for router in (admin_views.router, agent_views.router, horse_views.agent_router,
                   horse_views.admin_router, horse_views.public_router):
        for route in router.routes:
            routes.setdefault(route.name, route.path)
    return routes


def _api_route_names():
    from app.api.v1 import admin, agent, auth, horse_races, keno, lucky, payments, reports, tickets, users, wallet

    names = set()
    for module in (auth, users, wallet, keno, lucky, tickets, agent, admin, reports, payments, horse_races):
        names.update(route.name for route in module.router.routes)
    return names


def test_api_route_names_do_not_shadow_panel_pages():
    """L'API est montée avant les pages : un nom en commun fait pointer
    url_for(...) des menus vers le JSON de l'API."""
    collisions = set(_view_routes()) & _api_route_names()
    assert not collisions, f"Noms de routes en double (API / pages) : {sorted(collisions)}"


def test_template_links_point_to_pages_not_json_api():
    routes = _view_routes()
    bad = []
    for path in glob.glob("app/templates/**/*.html", recursive=True):
        source = open(path, encoding="utf-8").read()
        for match in re.finditer(r"""href="\{\{\s*url_for\(['"](\w+)['"]""", source):
            target = routes.get(match.group(1), "")
            if "/api/" in target:
                bad.append(f"{path}: {match.group(1)} -> {target}")
    assert not bad, "Liens vers des routes JSON :\n" + "\n".join(bad)


def test_extra_js_block_is_never_nested_in_content():
    """Imbriqué dans {% block content %}, le bloc extra_js est rendu deux fois
    (dans le contenu puis par base.html) : scripts exécutés deux fois."""
    nested = []
    for path in glob.glob("app/templates/**/*.html", recursive=True):
        source = open(path, encoding="utf-8").read()
        depth = 0
        for match in re.finditer(r"{%-?\s*(block\s+(\w+)|endblock)\b[^%]*%}", source):
            if match.group(2):
                if match.group(2) == "extra_js" and depth > 0:
                    nested.append(path)
                depth += 1
            else:
                depth -= 1
    assert not nested, f"extra_js imbriqué : {nested}"


@pytest.mark.parametrize("module", ["app.routes.admin", "app.routes.agent"])
def test_tojson_filter_is_safe_inside_script(module):
    import importlib

    from markupsafe import Markup

    templates = importlib.import_module(module).templates
    rendered = templates.env.from_string("const d = {{ v|tojson }};").render(v={"a": "x</script>&'"})
    assert "&#34;" not in rendered and "</script>" not in rendered
    assert rendered.startswith('const d = {"a": ')
    assert isinstance(templates.env.filters["tojson"]({"a": 1}), Markup)


@pytest.mark.asyncio
async def test_admin_user_create_page_renders(admin_client, admin_user):
    await _login(admin_client)
    response = await admin_client.get("/admin/users/create")
    assert response.status_code == 200, response.text[:500]
    assert "Créer un utilisateur" in response.text


@pytest.mark.asyncio
async def test_sidebar_dashboard_link_targets_html_page(admin_client, admin_user, db_session):
    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get("/admin/users")
    assert response.status_code == 200
    assert "/api/v1/admin/dashboard" not in response.text
    assert re.search(r'href="[^"]*/admin/dashboard"', response.text)
