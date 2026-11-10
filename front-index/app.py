import base64
import json
import os
from urllib.parse import quote, urlencode, urlsplit

import requests
from flask import Flask, g, redirect, render_template, request, url_for
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


def poser_cookies(reponse, jetons, refresh_token):
    """Cookies de session du front (HttpOnly : inaccessibles au JavaScript)."""
    reponse.set_cookie("access_token", jetons["access_token"], max_age=jetons["expires_in"],
                       httponly=True, secure=request.is_secure, samesite="Strict")
    reponse.set_cookie("refresh_token", refresh_token, max_age=7 * 24 * 3600,
                       httponly=True, secure=request.is_secure, samesite="Strict")
    return reponse


@app.before_request
def rafraichir_jeton():
    """Le cookie access_token disparaît à l'expiration du jeton (15 min) : on en redemande un avec le refresh_token."""
    # shortcut: deux pages chargées au même instant peuvent toutes deux tenter la rotation ; la 2e déconnecte.
    # Acceptable pour un front sans JavaScript, à revoir si des appels parallèles apparaissent.
    g.jetons = None
    if request.endpoint in (None, "static", "health", "logout", "login") \
            or "access_token" in request.cookies or "refresh_token" not in request.cookies:
        return
    reponse_api = appel_api(API_AUTH, "/api/auth/refresh", cookies={"refresh_token": request.cookies["refresh_token"]})
    if reponse_api is not None and reponse_api.ok:
        g.jetons = (reponse_api.json(), reponse_api.cookies["refresh_token"])
    elif reponse_api is not None:
        g.jetons = False  # session expirée ou révoquée : on déconnecte


@app.after_request
def enregistrer_jetons(reponse):
    if g.get("jetons"):
        poser_cookies(reponse, *g.jetons)
    elif g.get("jetons") is False:
        reponse.delete_cookie("refresh_token", httponly=True, secure=request.is_secure, samesite="Strict")
    return reponse


def jeton_acces():
    return g.jetons[0]["access_token"] if g.get("jetons") else request.cookies.get("access_token")


@app.context_processor
def utilisateur_connecte():
    return {"connecte": "refresh_token" in request.cookies and g.get("jetons") is not False}


@app.get("/health")
def health():
    return {"status": "ok"}


def lire_catalogue(route, jeton):
    """GET sur api-catalogue : (données, None) ou ({}, message d'erreur)."""
    reponse_api = appel_api(API_CATALOG, route, methode="GET", jeton=jeton)
    if reponse_api is None or not reponse_api.ok:
        return {}, erreur_api(reponse_api)[0]
    return reponse_api.json(), None


def page_precedente(defaut):
    """Revient sur la page d'où vient le formulaire (filtres compris), seulement si elle est sur ce site."""
    ref = urlsplit(request.referrer or "")
    if ref.netloc != request.host or not ref.path.startswith("/") or ref.path.startswith("//"):
        return defaut
    return ref.path + ("?" + ref.query if ref.query else "")


def action_catalogue(route, page, donnees=None, methode="POST", **args_page):
    """POST/DELETE sur api-catalogue puis retour à la page ; en cas d'erreur, la page s'affiche avec le message."""
    jeton = jeton_acces()
    if not jeton:
        return redirect(url_for("login"))
    reponse_api = appel_api(API_CATALOG, route, donnees, methode=methode, jeton=jeton)
    if reponse_api is None or not reponse_api.ok:
        return page(erreur=erreur_api(reponse_api)[0], **args_page)
    return redirect(page_precedente(url_for(page.__name__, **args_page)))


def action_film(route):
    """Emprunter / déclarer un film, depuis le catalogue ou depuis sa fiche."""
    film_id = request.form.get("film_id", type=int)
    if request.form.get("depuis") == "fiche" and film_id:
        return action_catalogue(route, film, {"film_id": film_id}, film_id=film_id)
    return action_catalogue(route, index, {"film_id": film_id})


@app.get("/")
def index(erreur=None):
    filtres = {"q": request.args.get("q", "").strip(), "genre": request.args.get("genre", "")}
    dispo = request.args.get("dispo") == "1"
    films, genres = [], []
    jeton = jeton_acces()
    if jeton:
        params = urlencode({k: v for k, v in filtres.items() if v})
        donnees, erreur_films = lire_catalogue("/api/films" + ("?" + params if params else ""), jeton)
        films = [f for f in donnees.get("films", []) if f.get("disponibles") or not dispo]
        genres = donnees.get("genres", [])
        erreur = erreur or erreur_films
    return render_template("index.j2", films=films, genres=genres, dispo=dispo, erreur=erreur, **filtres)


@app.get("/films/<int:film_id>")
def film(film_id, erreur=None):
    jeton = jeton_acces()
    if not jeton:
        return redirect(url_for("login"))
    donnees, erreur_film = lire_catalogue(f"/api/films/{film_id}", jeton)
    return render_template("film.j2", film=donnees or None, erreur=erreur or erreur_film)


@app.get("/videotheque")
def videotheque(erreur=None):
    jeton = jeton_acces()
    if not jeton:
        return redirect(url_for("login"))
    exemplaires, erreur_ex = lire_catalogue("/api/exemplaires", jeton)
    emprunts, erreur_emp = lire_catalogue("/api/emprunts", jeton)
    emprunts = emprunts.get("emprunts", [])
    return render_template("videotheque.j2", exemplaires=exemplaires.get("exemplaires", []),
                           en_cours=[e for e in emprunts if not e.get("date_retour")],
                           historique=[e for e in emprunts if e.get("date_retour")],
                           erreur=erreur or erreur_ex or erreur_emp)


@app.post("/exemplaires")
def ajouter_exemplaire():
    return action_film("/api/exemplaires")


@app.post("/exemplaires/<id>/supprimer")
def supprimer_exemplaire(id):
    return action_catalogue(f"/api/exemplaires/{quote(id)}", videotheque, methode="DELETE")


@app.post("/emprunts")
def emprunter():
    return action_film("/api/emprunts")


@app.post("/emprunts/<id>/prolongation")
def prolonger(id):
    return action_catalogue(f"/api/emprunts/{quote(id)}/prolongation", videotheque)


@app.post("/emprunts/<id>/retour")
def rendre(id):
    return action_catalogue(f"/api/emprunts/{quote(id)}/retour", videotheque)


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

    return poser_cookies(redirect(url_for("index")), reponse_api.json(), reponse_api.cookies["refresh_token"])


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
    jeton = jeton_acces()
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
