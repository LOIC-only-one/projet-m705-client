import base64
import json
from unittest.mock import patch

import pytest
import requests

from app import app

ID = "6af1d4e3851f2b7046ba4b81"
AUTRE = "6af1d4e3851f2b7046ba4b82"
COMPTE = {
    "id": ID,
    "username": "quentin",
    "email": "q@q.pw",
    "birthdate": "2000-01-02",
    "role": "user",
    "photo_profil": None,
    "created_at": "2026-11-08T10:00:00+00:00",
}


@pytest.fixture
def client():
    return app.test_client()


LISTE = {"users": [
    {"id": ID, "username": "quentin", "email": "q@q.pw"},
    {"id": AUTRE, "username": "<b>marie</b>", "email": "m@q.pw"},
]}


def fausse_reponse_api(code_http, corps=None):
    reponse_api = requests.Response()
    reponse_api.status_code = code_http
    reponse_api._content = json.dumps(corps).encode() if corps is not None else b""
    return reponse_api


def faux_jeton(role="admin", user_id=AUTRE):
    """Jeton non signé : le front ne lit que le contenu, la signature est vérifiée par api-user."""
    def b64(contenu):
        return base64.urlsafe_b64encode(json.dumps(contenu).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'RS256'})}.{b64({'sub': user_id, 'role': role})}.signature"


def connecte(client, role="admin"):
    client.set_cookie("access_token", faux_jeton(role))
    client.set_cookie("refresh_token", "ref")


def test_admin_sans_jeton_redirige_vers_login(client):
    page = client.get("/admin")
    assert page.status_code == 302
    assert page.headers["Location"].endswith("/login")


def test_admin_liste_les_utilisateurs(client):
    connecte(client)
    with patch("app.requests.get", return_value=fausse_reponse_api(200, LISTE)) as faux_get:
        page = client.get("/admin")

    assert page.status_code == 200
    assert faux_get.call_args.args == ("http://api-user:8000/api/users",)
    assert faux_get.call_args.kwargs["headers"]["Authorization"] == "Bearer " + faux_jeton()
    assert f"/admin?user={ID}" in page.text and f"/admin?user={AUTRE}" in page.text
    assert "quentin" in page.text and "q@q.pw" in page.text and "m@q.pw" in page.text
    assert "&lt;b&gt;marie&lt;/b&gt;" in page.text  # échappé
    assert faux_get.call_count == 1  # pas de détail tant qu'aucun utilisateur n'est choisi


def test_admin_clic_sur_un_user_affiche_son_formulaire(client):
    connecte(client)
    reponses = [fausse_reponse_api(200, LISTE), fausse_reponse_api(200, {**COMPTE, "role": "admin"})]
    with patch("app.requests.get", side_effect=reponses) as faux_get:
        page = client.get(f"/admin?user={ID}")

    assert page.status_code == 200
    assert faux_get.call_args.args == (f"http://api-user:8000/api/users/{ID}",)
    assert 'value="quentin"' in page.text
    assert 'value="q@q.pw"' in page.text
    assert 'value="2000-01-02"' in page.text
    assert '<option value="admin" selected>' in page.text
    assert '<option value="user" >' in page.text
    assert f'action="/admin/users/{ID}"' in page.text


def test_admin_user_simple_recoit_404_sans_appeler_l_api(client):
    connecte(client, role="user")
    with patch("app.requests.get") as faux_get, patch("app.requests.patch") as faux_patch, \
            patch("app.requests.delete") as faux_delete:
        assert client.get("/admin").status_code == 404
        assert client.get(f"/admin?user={ID}").status_code == 404
        assert client.post(f"/admin/users/{ID}", data={"role": "admin"}).status_code == 404
        page = client.post(f"/admin/users/{ID}/supprimer")

    assert page.status_code == 404
    assert "Accès refusé" not in page.text
    faux_get.assert_not_called()
    faux_patch.assert_not_called()
    faux_delete.assert_not_called()


def test_admin_id_mal_forme_404(client):
    connecte(client)
    with patch("app.requests.get") as faux_get:
        assert client.get("/admin?user=../health").status_code == 404
        assert client.post("/admin/users/pas-un-id", data={}).status_code == 404
    faux_get.assert_not_called()


def test_admin_user_inconnu_affiche_l_erreur_de_l_api(client):
    connecte(client)
    erreur = {"code": "UTILISATEUR_INTROUVABLE", "message": "Utilisateur introuvable."}
    reponses = [fausse_reponse_api(200, LISTE), fausse_reponse_api(404, {"error": erreur})]
    with patch("app.requests.get", side_effect=reponses):
        page = client.get(f"/admin?user={AUTRE}")

    assert page.status_code == 404
    assert "Utilisateur introuvable." in page.text


def test_admin_jeton_refuse_401_redirige_vers_login(client):
    connecte(client)
    erreur = {"code": "JETON_INVALIDE", "message": "Jeton invalide."}
    with patch("app.requests.get", return_value=fausse_reponse_api(401, {"error": erreur})):
        page = client.get("/admin")

    assert page.status_code == 302
    assert page.headers["Location"].endswith("/login")


def test_admin_api_indisponible(client):
    connecte(client)
    with patch("app.requests.get", side_effect=requests.ConnectionError):
        page = client.get("/admin")

    assert page.status_code == 503


def test_admin_modifier_envoie_le_patch_avec_le_role(client):
    connecte(client)
    with patch("app.requests.patch", return_value=fausse_reponse_api(200, COMPTE)) as faux_patch:
        page = client.post(f"/admin/users/{ID}", data={
            "username": "nouveau", "email": "n@q.pw", "birthdate": "", "photo_profil": "", "role": "admin",
        })

    assert page.status_code == 302
    assert page.headers["Location"].endswith(f"/admin?user={ID}&modifie=1")
    assert faux_patch.call_args.args == (f"http://api-user:8000/api/users/{ID}",)
    # date vide non envoyée, photo vide -> None (efface la photo), le rôle est transmis
    assert faux_patch.call_args.kwargs["json"] == {
        "username": "nouveau", "email": "n@q.pw", "role": "admin", "photo_profil": None,
    }
    assert faux_patch.call_args.kwargs["headers"]["Authorization"] == "Bearer " + faux_jeton()


def test_admin_modifier_erreur_garde_la_saisie(client):
    connecte(client)
    erreur = {"code": "COMPTE_EXISTANT", "message": "Email ou nom d'utilisateur déjà utilisé."}
    with patch("app.requests.patch", return_value=fausse_reponse_api(409, {"error": erreur})), \
            patch("app.requests.get", return_value=fausse_reponse_api(200, LISTE)):
        page = client.post(f"/admin/users/{ID}", data={"username": "pris", "email": "q@q.pw", "role": "admin"})

    assert page.status_code == 409
    assert "déjà utilisé" in page.text
    assert 'value="pris"' in page.text
    assert '<option value="admin" selected>' in page.text


def test_admin_modifier_sans_jeton_redirige_vers_login(client):
    with patch("app.requests.patch") as faux_patch:
        page = client.post(f"/admin/users/{ID}", data={"role": "admin"})

    assert page.status_code == 302
    assert page.headers["Location"].endswith("/login")
    faux_patch.assert_not_called()


@pytest.mark.parametrize("role,attendu", [("admin", True), ("user", False)])
def test_variable_admin_pour_le_menu(role, attendu):
    with app.test_request_context("/", headers={"Cookie": "access_token=" + faux_jeton(role)}):
        app.preprocess_request()
        contexte = {}
        app.update_template_context(contexte)
    assert contexte["admin"] is attendu


def test_admin_affiche_le_bouton_supprimer_pour_un_autre_user(client):
    connecte(client)  # l'admin est AUTRE
    reponses = [fausse_reponse_api(200, LISTE), fausse_reponse_api(200, COMPTE)]
    with patch("app.requests.get", side_effect=reponses):
        page = client.get(f"/admin?user={ID}")

    assert f'formaction="/admin/users/{ID}/supprimer"' in page.text
    assert "Supprimer l&#39;utilisateur" in page.text or "Supprimer l'utilisateur" in page.text
    assert "Enregistrer les modifications" in page.text


def test_admin_pas_de_bouton_supprimer_sur_son_propre_compte(client):
    connecte(client)
    reponses = [fausse_reponse_api(200, LISTE), fausse_reponse_api(200, {**COMPTE, "id": AUTRE})]
    with patch("app.requests.get", side_effect=reponses):
        page = client.get(f"/admin?user={AUTRE}")

    assert page.status_code == 200
    assert "/supprimer" not in page.text
    assert "Enregistrer les modifications" in page.text


def test_admin_supprimer_appelle_delete_puis_confirme(client):
    connecte(client)
    with patch("app.requests.delete", return_value=fausse_reponse_api(204)) as faux_delete:
        page = client.post(f"/admin/users/{ID}/supprimer")

    assert page.status_code == 302
    assert page.headers["Location"].endswith("/admin?supprime=1")
    assert faux_delete.call_args.args == (f"http://api-user:8000/api/users/{ID}",)
    assert faux_delete.call_args.kwargs["headers"]["Authorization"] == "Bearer " + faux_jeton()

    liste = fausse_reponse_api(200, {"users": [LISTE["users"][1]]})
    with patch("app.requests.get", return_value=liste):
        suite = client.get("/admin?supprime=1")
    assert "Utilisateur supprimé." in suite.text


def test_admin_supprimer_erreur_de_l_api_affichee(client):
    connecte(client)
    erreur = {"code": "UTILISATEUR_INTROUVABLE", "message": "Utilisateur introuvable."}
    with patch("app.requests.delete", return_value=fausse_reponse_api(404, {"error": erreur})), \
            patch("app.requests.get", side_effect=[fausse_reponse_api(200, LISTE),
                                                    fausse_reponse_api(404, {"error": erreur})]):
        page = client.post(f"/admin/users/{ID}/supprimer")

    assert page.status_code == 404
    assert "Utilisateur introuvable." in page.text


def test_admin_ne_peut_pas_se_supprimer_lui_meme(client):
    connecte(client)  # l'admin est AUTRE
    reponses = [fausse_reponse_api(200, LISTE), fausse_reponse_api(200, {**COMPTE, "id": AUTRE})]
    with patch("app.requests.delete") as faux_delete, patch("app.requests.get", side_effect=reponses):
        page = client.post(f"/admin/users/{AUTRE}/supprimer")

    assert page.status_code == 403
    assert "propre compte" in page.text
    faux_delete.assert_not_called()


def test_admin_supprimer_sans_jeton_login_et_id_invalide_404(client):
    with patch("app.requests.delete") as faux_delete:
        page = client.post(f"/admin/users/{ID}/supprimer")
        assert page.status_code == 302 and page.headers["Location"].endswith("/login")
        connecte(client)
        assert client.post("/admin/users/pas-un-id/supprimer").status_code == 404
    faux_delete.assert_not_called()


def test_admin_supprimer_jeton_refuse_401_redirige_vers_login(client):
    connecte(client)
    erreur = {"code": "JETON_INVALIDE", "message": "Jeton invalide."}
    with patch("app.requests.delete", return_value=fausse_reponse_api(401, {"error": erreur})):
        page = client.post(f"/admin/users/{ID}/supprimer")

    assert page.status_code == 302
    assert page.headers["Location"].endswith("/login")