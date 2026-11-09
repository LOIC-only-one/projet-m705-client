import os

import requests
from flask import Flask, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.jinja_env.autoescape = True  # pas actif par défaut pour les .j2
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

API_AUTH = os.environ.get("API_AUTH_URL", "http://api-auth:8000")
API_USER = os.environ.get("API_USER_URL", "http://api-user:8000")
API_CATALOG = os.environ.get("API_CATALOG_URL", "http://api-catalogue:8000")


def appel_api(route, donnees=None, cookies=None):
    # on transmet l'ip du client pour le rate limit de api-auth
    headers = {"X-Forwarded-For": request.remote_addr}
    try:
        return requests.post(API_AUTH + route, json=donnees, cookies=cookies, headers=headers, timeout=5)
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
    return render_template("index.j2")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.j2", inscrit="inscrit" in request.args)

    email = request.form.get("email", "")
    mot_de_passe = request.form.get("password", "")
    reponse_api = appel_api("/api/auth/login", {"email": email, "password": mot_de_passe})
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("login.j2", erreur=message, email=email), code_http

    jetons = reponse_api.json()
    redirection = redirect(url_for("index"))
    redirection.set_cookie("access_token", jetons["access_token"], max_age=jetons["expires_in"],
                           httponly=True, secure=True, samesite="Strict")
    redirection.set_cookie("refresh_token", reponse_api.cookies["refresh_token"], max_age=7 * 24 * 3600,
                           httponly=True, secure=True, samesite="Strict")
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
    reponse_api = appel_api("/api/auth/register", nouvel_utilisateur)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("register.j2", erreur=message, form=request.form), code_http

    return redirect(url_for("login", inscrit=1))


@app.post("/logout")
def logout():
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        appel_api("/api/auth/logout", cookies={"refresh_token": refresh_token})

    redirection = redirect(url_for("index"))
    redirection.delete_cookie("access_token", httponly=True, secure=True, samesite="Strict")
    redirection.delete_cookie("refresh_token", httponly=True, secure=True, samesite="Strict")
    return redirection
