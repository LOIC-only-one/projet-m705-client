import base64
import functools
import json
import os
import re
from urllib.parse import quote, urlencode, urlsplit

import requests
from flask import Flask, abort, g, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.jinja_env.autoescape = True  # pas actif par défaut pour les .j2
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # x_proto : request.is_secure suit le https de Caddy

API_AUTH = os.environ.get("API_AUTH_URL", "http://api-auth:8000")
API_USER = os.environ.get("API_USER_URL", "http://api-user:8000")
API_CATALOG = os.environ.get("API_CATALOG_URL", "http://api-catalogue:8000")


def appel_api(api, route, donnees=None, methode="POST", jeton=None):
    """Appelle une API en JSON. Renvoie la réponse, ou None si l'API est injoignable."""
    headers = {"X-Forwarded-For": request.remote_addr}  # vraie IP du client, pour le rate limit d'api-auth
    if jeton:
        headers["Authorization"] = "Bearer " + jeton
    try:
        # timeout long : au premier lancement, api-catalogue importe les films depuis TMDB
        return getattr(requests, methode.lower())(api + route, json=donnees, headers=headers, timeout=30)
    except requests.RequestException:
        return None


def erreur_api(reponse_api):
    """Message à afficher et code HTTP, à partir d'une réponse d'erreur { "error": {code, message} }."""
    if reponse_api is None:
        return "Service indisponible.", 503
    try:
        erreur = reponse_api.json()["error"]
    except Exception:
        return "Erreur inattendue.", reponse_api.status_code

    message = erreur["message"]
    if "details" in erreur:
        for champ, erreurs_champ in erreur["details"].get("json", {}).items():
            message += " " + champ + " : " + ", ".join(erreurs_champ)
    return message, reponse_api.status_code


# Les deux jetons sont gardés dans des cookies HttpOnly (illisibles en JavaScript) du front.
def poser_cookies(reponse, jetons):
    options = {"httponly": True, "secure": request.is_secure, "samesite": "Strict"}
    reponse.set_cookie("access_token", jetons["access_token"], max_age=jetons["expires_in"], **options)
    reponse.set_cookie("refresh_token", jetons["refresh_token"], max_age=7 * 24 * 3600, **options)
    return reponse


def effacer_cookies(reponse):
    for nom in ("access_token", "refresh_token"):
        reponse.delete_cookie(nom, httponly=True, secure=request.is_secure, samesite="Strict")
    return reponse


@app.before_request
def rafraichir_jeton():
    """Le cookie access_token disparaît au bout de 15 min : on en redemande un à api-auth avec le refresh_token."""
    # shortcut: deux pages chargées au même instant peuvent toutes deux tenter la rotation ; la 2e déconnecte.
    # Acceptable pour un front sans JavaScript, à revoir si des appels parallèles apparaissent.
    g.nouveaux_jetons = None
    g.deconnecte = False
    if request.endpoint in (None, "static", "health", "logout", "login") \
            or "access_token" in request.cookies or "refresh_token" not in request.cookies:
        return
    reponse_api = appel_api(API_AUTH, "/api/auth/refresh", jeton=request.cookies["refresh_token"])
    if reponse_api is None:
        return  # api-auth injoignable : on réessaiera à la page suivante
    if reponse_api.ok:
        g.nouveaux_jetons = reponse_api.json()
    else:
        g.deconnecte = True  # refresh_token expiré ou révoqué


@app.after_request
def enregistrer_jetons(reponse):
    if g.get("nouveaux_jetons"):
        poser_cookies(reponse, g.nouveaux_jetons)
    if g.get("deconnecte"):
        effacer_cookies(reponse)
    return reponse


def jeton_acces():
    if g.get("nouveaux_jetons"):
        return g.nouveaux_jetons["access_token"]
    return request.cookies.get("access_token")


def profil_navbar(jeton):
    """Pseudo et photo de profil (URL) de l'utilisateur connecté, via GET /api/users/<id>.

    Mis en cache dans g : un seul appel par requête, même si plusieurs templates sont rendus.
    En cas d'échec de l'API, la navbar retombe sur l'initiale du pseudo (ou un "?").
    """
    if "profil_navbar" not in g:
        g.profil_navbar = {}
        id_user = id_utilisateur(jeton) if jeton else None
        if id_user:
            reponse_api = appel_api(API_USER, f"/api/users/{quote(str(id_user))}", methode="GET", jeton=jeton)
            if reponse_api is not None and reponse_api.ok:
                g.profil_navbar = reponse_api.json()
    return g.profil_navbar


@app.context_processor
def utilisateur_connecte():
    # "admin" sert seulement à afficher le lien du menu : l'API vérifie le jeton et refuse (403) les non-admins
    jeton = jeton_acces()
    connecte = "refresh_token" in request.cookies and not g.get("deconnecte")
    profil = profil_navbar(jeton) if connecte and jeton else {}
    return {"connecte": connecte,
            "admin": charge_jeton(jeton or "").get("role") == "admin",
            "photo_profil": profil.get("photo_profil"),
            "nom_utilisateur": profil.get("username", "")}


@app.get("/health")
def health():
    return {"status": "ok"}


#### Pages qui utilisent api-catalogue
def appel_catalogue(methode, route, donnees=None):
    """Appel à api-catalogue avec le jeton de l'utilisateur. Renvoie (données, None) ou ({}, message d'erreur)."""
    reponse_api = appel_api(API_CATALOG, route, donnees, methode=methode, jeton=jeton_acces())
    if reponse_api is None or not reponse_api.ok:
        return {}, erreur_api(reponse_api)[0]
    if reponse_api.status_code == 204:  # DELETE : pas de corps
        return {}, None
    return reponse_api.json(), None


def page_precedente():
    """Page d'où vient le formulaire (catalogue filtré, fiche...), seulement si elle est sur ce site."""
    ref = urlsplit(request.referrer or "")
    if ref.netloc != request.host or not ref.path:
        return url_for("catalogue")
    return ref.path + ("?" + ref.query if ref.query else "")


@app.get("/")
def index():
    if not jeton_acces():
        return render_template("index.j2", sections=[], total=0)  # page d'accueil des visiteurs
    donnees, erreur = appel_catalogue("GET", "/api/films?per_page=20")
    films = donnees.get("films", [])
    sections = [films[i:i + 5] for i in range(0, len(films), 5)]  # 4 rangées de 5 films
    return render_template("index.j2", sections=sections, total=donnees.get("total", 0), erreur=erreur)


@app.get("/catalogue")
def catalogue():
    if not jeton_acces():
        return redirect(url_for("login"))
    q = request.args.get("q", "").strip()
    genre = request.args.get("genre", "")
    dispo = request.args.get("dispo") == "1"
    page = max(request.args.get("page", 1, type=int), 1)
    donnees, erreur = appel_catalogue("GET", "/api/films?" + urlencode({"q": q, "genre": genre, "page": page}))
    films = donnees.get("films", [])
    if dispo:  # shortcut: filtré côté front, une page peut donc avoir moins de 20 films ; à passer dans l'API sinon
        films = [film for film in films if film["disponibles"]]
    return render_template("catalogue.j2", films=films, genres=donnees.get("genres", []), q=q, genre=genre,
                           dispo=dispo, page=page, pages=donnees.get("pages", 0), total=donnees.get("total", 0),
                           erreur=erreur)


@app.get("/films/<int:film_id>")
def film(film_id, erreur=None):
    if not jeton_acces():
        return redirect(url_for("login"))
    donnees, erreur_api_film = appel_catalogue("GET", f"/api/films/{film_id}")
    return render_template("film.j2", film=donnees or None, erreur=erreur or erreur_api_film)


@app.get("/videotheque")
def videotheque(erreur=None):
    if not jeton_acces():
        return redirect(url_for("login"))
    exemplaires, erreur_ex = appel_catalogue("GET", "/api/exemplaires")
    emprunts, erreur_emp = appel_catalogue("GET", "/api/emprunts")
    emprunts = emprunts.get("emprunts", [])
    return render_template("videotheque.j2", exemplaires=exemplaires.get("exemplaires", []),
                           en_cours=[e for e in emprunts if not e["date_retour"]],
                           historique=[e for e in emprunts if e["date_retour"]],
                           erreur=erreur or erreur_ex or erreur_emp)


# Actions (formulaires POST). Succès : retour à la page d'origine. Erreur : la page s'affiche avec le message.
@app.post("/emprunts")
def emprunter():
    film_id = request.form.get("film_id", type=int)
    _, erreur = appel_catalogue("POST", "/api/emprunts", {"film_id": film_id})
    return film(film_id, erreur=erreur) if erreur else redirect(page_precedente())


@app.post("/exemplaires")
def ajouter_exemplaire():
    film_id = request.form.get("film_id", type=int)
    _, erreur = appel_catalogue("POST", "/api/exemplaires", {"film_id": film_id})
    return film(film_id, erreur=erreur) if erreur else redirect(page_precedente())


@app.post("/exemplaires/<id>/supprimer")
def supprimer_exemplaire(id):
    _, erreur = appel_catalogue("DELETE", f"/api/exemplaires/{quote(id)}")
    return videotheque(erreur=erreur) if erreur else redirect(url_for("videotheque"))


@app.post("/emprunts/<id>/prolongation")
def prolonger(id):
    _, erreur = appel_catalogue("POST", f"/api/emprunts/{quote(id)}/prolongation")
    return videotheque(erreur=erreur) if erreur else redirect(url_for("videotheque"))


@app.post("/emprunts/<id>/retour")
def rendre(id):
    _, erreur = appel_catalogue("POST", f"/api/emprunts/{quote(id)}/retour")
    return videotheque(erreur=erreur) if erreur else redirect(url_for("videotheque"))


#### Compte (api-auth et api-user)
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

    return poser_cookies(redirect(url_for("index")), reponse_api.json())


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
    if nouvel_utilisateur["password"] != request.form.get("password_confirmation", ""):
        return render_template(
            "register.j2",
            erreur="Les mots de passe ne correspondent pas.",
            form=request.form,
        ), 400
    reponse_api = appel_api(API_AUTH, "/api/auth/register", nouvel_utilisateur)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("register.j2", erreur=message, form=request.form), code_http

    return redirect(url_for("login", inscrit=1))


@app.post("/logout")
def logout():
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        appel_api(API_AUTH, "/api/auth/logout", jeton=refresh_token)  # révoque le refresh_token côté serveur
    return effacer_cookies(redirect(url_for("index")))


def charge_jeton(jeton):
    # le front n'a pas la clé publique : on lit juste le contenu, c'est l'API qui vérifie la signature
    try:
        charge = jeton.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4)))
    except Exception:
        return {}


def id_utilisateur(jeton):
    return charge_jeton(jeton).get("sub")


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
    champs = ("username", "email", "birthdate", "photo_profil")
    modif_utilisateur = {c: request.form[c] for c in champs if request.form.get(c)}
    reponse_api = appel_api(API_USER, f"/api/users/{id_user}", modif_utilisateur, methode="PATCH", jeton=jeton)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("account.j2", erreur=message, form=request.form), code_http

    return redirect(url_for("account"))


@app.post("/account/password")
def changer_mot_de_passe():
    jeton = jeton_acces()
    id_user = id_utilisateur(jeton) if jeton else None
    if id_user is None:
        return redirect(url_for("login"))
    donnees = {
        "current_password": request.form.get("password", ""),
        "new_password": request.form.get("new_password", ""),
    }
    reponse_api = appel_api(API_USER, f"/api/users/{id_user}/password", donnees, jeton=jeton)
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("account.j2", erreur=message, form=request.form), code_http
    return redirect(url_for("account"))


@app.post("/account/supprimer")
def supprimer_compte():
    jeton = jeton_acces()
    id_user = id_utilisateur(jeton) if jeton else None
    if id_user is None:
        return redirect(url_for("login"))
    reponse_api = appel_api(
        API_USER, f"/api/users/{id_user}", {"current_password": request.form.get("password", "")},
        methode="DELETE", jeton=jeton,
    )
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("account.j2", erreur=message, form=request.form), code_http
    return effacer_cookies(redirect(url_for("index")))


def id_valide(id):
    """Un id MongoDB (ObjectId) : 24 caractères hexadécimaux."""
    return re.fullmatch(r"[0-9a-f]{24}", id) is not None


def admin_requis(vue):
    """Réserve une page de l'administration aux admins : sans jeton, login ; avec un jeton de simple user, 404.

    Le rôle est lu dans le jeton sans vérifier la signature (le front n'a pas la clé) : ce n'est qu'un filtre
    d'affichage, api-user vérifie le jeton et refuse (403) tout appel d'un non-admin.
    """
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        jeton = jeton_acces()
        if not jeton:
            return redirect(url_for("login"))
        if charge_jeton(jeton).get("role") != "admin":
            abort(404)  # la page n'existe pas pour un non-admin
        return vue(*args, **kwargs)
    return enveloppe


def afficher_admin(selection=None, erreur=None, form=None, code_http=200, modifie=False, supprime=False):
    """Dashboard : liste des utilisateurs (GET /api/users) et, si un id est choisi, son formulaire (GET /api/users/<id>)."""
    jeton = jeton_acces()
    reponse_api = appel_api(API_USER, "/api/users", methode="GET", jeton=jeton)
    if reponse_api is not None and reponse_api.status_code == 401:
        return redirect(url_for("login"))
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return render_template("admin.j2", erreur=message, utilisateurs=[], selection=None, form={}), code_http
    utilisateurs = reponse_api.json().get("users", [])

    if selection and form is None:
        reponse_api = appel_api(API_USER, f"/api/users/{selection}", methode="GET", jeton=jeton)
        if reponse_api is not None and reponse_api.status_code == 401:
            return redirect(url_for("login"))
        if reponse_api is None or not reponse_api.ok:
            erreur, code_http = erreur_api(reponse_api)
            form = {}
        else:
            form = reponse_api.json()
    return render_template("admin.j2", utilisateurs=utilisateurs, selection=selection, form=form or {}, erreur=erreur,
                           modifie=modifie, supprime=supprime, moi=id_utilisateur(jeton)), code_http


@app.get("/admin")
@admin_requis
def admin():
    selection = request.args.get("user", "")
    if selection and not id_valide(selection):
        abort(404)
    return afficher_admin(selection or None, modifie="modifie" in request.args, supprime="supprime" in request.args)


@app.post("/admin/users/<id>")
@admin_requis
def admin_modifier_utilisateur(id):
    if not id_valide(id):
        abort(404)
    jeton = jeton_acces()

    # champs vides non envoyés (l'API refuse une date vide) ; une photo vide, elle, efface la photo
    modif_utilisateur = {c: request.form[c] for c in ("username", "email", "birthdate", "role") if request.form.get(c)}
    modif_utilisateur["photo_profil"] = request.form.get("photo_profil") or None
    reponse_api = appel_api(API_USER, f"/api/users/{id}", modif_utilisateur, methode="PATCH", jeton=jeton)
    if reponse_api is not None and reponse_api.status_code == 401:
        return redirect(url_for("login"))
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return afficher_admin(id, erreur=message, form=request.form, code_http=code_http)

    return redirect(url_for("admin", user=id, modifie=1))


@app.post("/admin/users/<id>/supprimer")
@admin_requis
def admin_supprimer_utilisateur(id):
    if not id_valide(id):
        abort(404)
    jeton = jeton_acces()
    if id == id_utilisateur(jeton):  # un admin ne se supprime pas lui-même : il perdrait son accès
        return afficher_admin(id, erreur="Vous ne pouvez pas supprimer votre propre compte depuis l'administration.",
                              code_http=403)

    reponse_api = appel_api(API_USER, f"/api/users/{id}", methode="DELETE", jeton=jeton)
    if reponse_api is not None and reponse_api.status_code == 401:
        return redirect(url_for("login"))
    if reponse_api is None or not reponse_api.ok:
        message, code_http = erreur_api(reponse_api)
        return afficher_admin(id, erreur=message, code_http=code_http)

    return redirect(url_for("admin", supprime=1))