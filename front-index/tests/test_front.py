import base64
import json
from unittest.mock import patch

import pytest
import requests

from app import app


@pytest.fixture
def client():
    return app.test_client()


def fausse_reponse_api(code_http, corps=None, cookies=None):
    reponse_api = requests.Response()
    reponse_api.status_code = code_http
    reponse_api._content = b""
    if corps is not None:
        reponse_api._content = json.dumps(corps).encode()
    if cookies is not None:
        for nom_cookie in cookies:
            reponse_api.cookies.set(nom_cookie, cookies[nom_cookie])
    return reponse_api


@pytest.mark.parametrize("url", ["/", "/login", "/register"])
def test_pages(client, url):
    assert client.get(url).status_code == 200


def test_autoescape():
    with app.test_request_context():
        assert app.jinja_env.from_string("{{ x }}").render(x="<b>") == "&lt;b&gt;"


def test_login(client):
    reponse_api = fausse_reponse_api(200, {"access_token": "acc", "expires_in": 900}, {"refresh_token": "ref"})
    with patch("app.requests.post", return_value=reponse_api) as faux_post:
        page = client.post("/login", data={"email": "a@b.fr", "password": "x"})

    assert page.status_code == 302
    assert faux_post.call_args.kwargs["json"] == {"email": "a@b.fr", "password": "x"}
    access_token = client.get_cookie("access_token")
    refresh_token = client.get_cookie("refresh_token")
    assert access_token.value == "acc"
    assert access_token.http_only and access_token.secure
    assert refresh_token.value == "ref"
    assert refresh_token.http_only and refresh_token.secure


def test_login_mauvais_mdp(client):
    erreur = {"code": "IDENTIFIANTS_INVALIDES", "message": "Email ou mot de passe incorrect."}
    with patch("app.requests.post", return_value=fausse_reponse_api(401, {"error": erreur})):
        page = client.post("/login", data={"email": "a@b.fr", "password": "x"})

    assert page.status_code == 401
    assert "Email ou mot de passe incorrect." in page.text


def test_register_invalide(client):
    erreur = {"code": "BAD REQUEST", "message": "Validation error", "details": {"json": {"password": ["Trop court."]}}}
    with patch("app.requests.post", return_value=fausse_reponse_api(400, {"error": erreur})):
        page = client.post("/register", data={"username": "<b>x</b>", "password": "p"})

    assert page.status_code == 400
    assert "password : Trop court." in page.text
    assert "&lt;b&gt;x&lt;/b&gt;" in page.text


def test_api_down(client):
    with patch("app.requests.post", side_effect=requests.ConnectionError):
        page = client.post("/login", data={"email": "a@b.fr", "password": "x"})

    assert page.status_code == 503


def test_logout(client):
    client.set_cookie("refresh_token", "ref")
    with patch("app.requests.post", return_value=fausse_reponse_api(204)) as faux_post:
        page = client.post("/logout")

    assert page.status_code == 302
    assert faux_post.call_args.kwargs["cookies"] == {"refresh_token": "ref"}
    assert client.get_cookie("refresh_token") is None


JETON = "x." + base64.urlsafe_b64encode(json.dumps({"sub": "abc123"}).encode()).decode().rstrip("=") + ".x"


def test_account_non_connecte(client):
    assert client.get("/account").status_code == 302


def test_account_get(client):
    client.set_cookie("access_token", JETON)
    user = {"id": "abc123", "username": "bob", "email": "b@b.fr", "birthdate": "2000-01-01", "role": "user", "photo_profil": None}
    with patch("app.requests.get", return_value=fausse_reponse_api(200, user)) as faux_get:
        reponse = client.get("/account")
    assert reponse.status_code == 200
    assert b'value="bob"' in reponse.data
    assert faux_get.call_args.args[0].endswith("/api/users/abc123")
    assert faux_get.call_args.kwargs["headers"]["Authorization"] == "Bearer " + JETON


def test_account_patch(client):
    client.set_cookie("access_token", JETON)
    with patch("app.requests.patch", return_value=fausse_reponse_api(200, {})) as faux_patch:
        reponse = client.post("/account", data={"username": "bob2", "email": "", "photo_profil": ""})
    assert reponse.status_code == 302
    assert faux_patch.call_args.kwargs["json"] == {"username": "bob2"}
