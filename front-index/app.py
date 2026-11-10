import base64
import json
import os

import requests
from flask import Flask, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.jinja_env.autoescape = True  # pas actif par défaut pour les .j2
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # x_proto : request.is_secure suit le https de Caddy

API_AUTH = os.environ.get("API_AUTH_URL", "http://api-auth:8000")
API_USER = os.environ.get("API_USER_URL", "http://api-user:8000")
API_CATALOG = os.environ.get("API_CATALOG_URL", "http://api-catalogue:8000")


def appel_api(api, route, donnees=None, cookies=None, methode="POST", jeton=None):
    # on transmet l'ip du client pour le rate limit de api-auth
    headers = {"X-Forwarded-For": request.remote_addr}
    if jeton:
        headers["Authorization"] = "Bearer " + jeton
    try:
        return getattr(requests, methode.lower())(api + route, json=donnees, cookies=cookies, headers=headers, timeout=5)
    except requests.RequestException:
        return None


def erreur_api(reponse_api):
    if reponse_api is None:
        return "Service d'authentification indisponible.", 503
    try:
        erreur = reponse_api.json()["error"]
    except Exception:
        return "Erreur inattendue.", reponse_api.status_code

    message = erreur["message"]
    if "details" in erreur:
        for champ, erreurs_champ in erreur["details"].get("json", {}).items():
            message += " " + champ + " : " + ", ".join(erreurs_champ)
    return message, reponse_api.status_code


@app.context_processor
def utilisateur_connecte():
    return {"connecte": "refresh_token" in request.cookies}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    films = []
    erreur = None
    jeton = request.cookies.get("access_token")
    if jeton:
        reponse_api = appel_api(API_CATALOG, "/api/films", methode="GET", jeton=jeton)
        if reponse_api is None or not reponse_api.ok:
            erreur, _ = erreur_api(reponse_api)
        else:
            films = reponse_api.json().get("films", [])
    return render_template("index.j2", films=films, erreur_films=erreur)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.j2", inscrit="inscrit" in request.args)

    email = request.form.get("email", "")
    mot_de_passe = request.form.get("password", "")
    reponse_api = appel_api(API_AUTH, "/api/auth/login", {"email": email, "password": mot_de_passe})
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("login.j2", erreur=message, email=email), code_http

    jetons = reponse_api.json()
    redirection = redirect(url_for("index"))
    redirection.set_cookie("access_token", jetons["access_token"], max_age=jetons["expires_in"],
                           httponly=True, secure=request.is_secure, samesite="Strict")
    redirection.set_cookie("refresh_token", reponse_api.cookies["refresh_token"], max_age=7 * 24 * 3600,
                           httponly=True, secure=request.is_secure, samesite="Strict")
    return redirection


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.j2", form={})

    nouvel_utilisateur = {
        "username": request.form.get("username", ""),
        "email": request.form.get("email", ""),
        "password": request.form.get("password", ""),
        "birthdate": request.form.get("birthdate", ""),
    }
    reponse_api = appel_api(API_AUTH, "/api/auth/register", nouvel_utilisateur)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("register.j2", erreur=message, form=request.form), code_http

    return redirect(url_for("login", inscrit=1))


@app.post("/logout")
def logout():
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        appel_api(API_AUTH, "/api/auth/logout", cookies={"refresh_token": refresh_token})

    redirection = redirect(url_for("index"))
    redirection.delete_cookie("access_token", httponly=True, secure=request.is_secure, samesite="Strict")
    redirection.delete_cookie("refresh_token", httponly=True, secure=request.is_secure, samesite="Strict")
    return redirection


def id_utilisateur(jeton):
    # le front n'a pas le secret : on lit juste le "sub", c'est api-user qui vérifie la signature
    try:
        charge = jeton.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4)))["sub"]
    except Exception:
        return None


@app.route("/account", methods=["GET", "POST"])
def account():
    jeton = request.cookies.get("access_token")
    id_user = id_utilisateur(jeton) if jeton else None
    if id_user is None:
        return redirect(url_for("login"))

    if request.method == "GET":
        reponse_api = appel_api(API_USER, f"/api/users/{id_user}", methode="GET", jeton=jeton)
        if reponse_api is None or not reponse_api.ok:
            message, code_http = erreur_api(reponse_api)
            return render_template("account.j2", erreur=message, form={}), code_http
        return render_template("account.j2", form=reponse_api.json())

    # champs vides non envoyés : l'API refuse une date vide et le rôle n'est pas modifiable ici
    champs = ("username", "email", "birthdate", "photo_profil", "password")
    modif_utilisateur = {c: request.form[c] for c in champs if request.form.get(c)}
    reponse_api = appel_api(API_USER, f"/api/users/{id_user}", modif_utilisateur, methode="PATCH", jeton=jeton)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("account.j2", erreur=message, form=request.form), code_http

    return redirect(url_for("account"))
