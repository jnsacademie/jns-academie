import base64
import hmac
from html import escape as esc
import io
import json
import os
import re
import secrets
import unicodedata
from datetime import date, datetime

import pandas as pd
import streamlit as st

NOM = "Jns Académie"
PAYS = "RÉPUBLIQUE DÉMOCRATIQUE DU CONGO"
UNIVERSITE = "UNIVERSITÉ DE KOLWEZI"
FACULTE = "Faculté des Sciences : GEOLOGIE"   # faculté par défaut (si non précisée pour l'étudiant)
MOT_DE_PASSE_PAR_DEFAUT = "Malgré tout jns12"   # utilisé seulement si aucun secret MOT_DE_PASSE n'est défini


CLE_ADMIN_PAR_DEFAUT = "jns-proprietaire"   # à remplacer par le secret CLE_ADMIN


def lire_cle_admin():
    """Clé secrète à mettre dans l'adresse (?admin=CLE) pour voir l'accès propriétaire."""
    try:
        valeur = st.secrets["CLE_ADMIN"]
        if valeur:
            return str(valeur)
    except Exception:
        pass
    return os.environ.get("JNS_CLE_ADMIN", CLE_ADMIN_PAR_DEFAUT)


def param_url(nom):
    try:
        v = st.query_params.get(nom, "")
    except AttributeError:
        v = st.experimental_get_query_params().get(nom, [""])
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v)


def lire_mot_de_passe():
    """Mot de passe admin : secret Streamlit MOT_DE_PASSE, sinon variable d'environnement, sinon défaut."""
    try:
        valeur = st.secrets["MOT_DE_PASSE"]
        if valeur:
            return str(valeur)
    except Exception:
        pass
    return os.environ.get("JNS_MOT_DE_PASSE", MOT_DE_PASSE_PAR_DEFAUT)

FICHIERS = {
    "etudiants": ["matricule", "nom", "promotion", "code", "faculte", "en_ordre", "postnom", "prenom"],   # en_ordre : "1" = frais payés
    "cours": ["intitule", "promotion", "heures", "credits", "faculte"],   # pas de code : le cours = son intitulé
    "notes": ["matricule", "cours", "moyenne", "examen"],  # cours = intitulé ; moyenne /10 + examen /10
    "profils": ["matricule", "sexe", "date_naissance", "lieu_naissance",
                "nationalite", "adresse", "telephone"],
    "facultes": ["faculte", "code", "promotions", "universite"],
    "universites": ["universite", "sigle", "pays", "adresse", "email", "telephone"],
    "deliberations": ["matricule", "annee", "session", "promotion", "faculte", "numero",
                      "moyenne", "credits", "total_credits", "mention", "decision",
                      "date", "publie", "details"],
}
CLES = {"etudiants": ["matricule"], "cours": ["intitule", "promotion", "faculte"],
        "notes": ["matricule", "cours"], "profils": ["matricule"],
        "facultes": ["faculte"], "universites": ["universite"], "deliberations": ["matricule", "annee", "session"]}
SESSIONS = ["Première session", "Rattrapage première session",
            "Deuxième session", "Rattrapage deuxième session"]
CODE_FACULTE_PAR_DEFAUT = "51/kz"   # code de la faculté par défaut (Géologie)
DOSSIER_APP = os.path.dirname(os.path.abspath(__file__))
PARAMS = "parametres.json"
DOSSIER_PHOTOS = "photos"
os.makedirs(DOSSIER_PHOTOS, exist_ok=True)

# Le total de crédits n'est plus fixe (60, 55, ...) : il est calculé en additionnant
# les crédits de tous les cours de la promotion.
SEUIL_RATIO = 0.75   # part minimale des crédits à valider pour passer (45/60 = 75 %)


def total_credits(d):
    """Total des crédits de la promotion = somme des crédits de tous ses cours."""
    return float(pd.to_numeric(d["credits"], errors="coerce").fillna(0).sum())


def seuil_credits(d):
    """Minimum de crédits validés pour passer, proportionnel au total de la promotion."""
    return SEUIL_RATIO * total_credits(d)



# ---------- Stockage : base Supabase (permanente) ou fichiers locaux (tests) ----------
def lire_secret(nom, defaut=""):
    try:
        v = st.secrets[nom]
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get("JNS_" + nom, defaut)


def base_en_ligne():
    return bool(lire_secret("SUPABASE_URL") and lire_secret("SUPABASE_KEY"))


def _db_requete(methode, params=None, json_corps=None, extra=None):
    import requests
    cle = lire_secret("SUPABASE_KEY")
    entetes = {"apikey": cle, "Content-Type": "application/json"}
    if cle.startswith("eyJ"):          # ancienne clé service_role (JWT)
        entetes["Authorization"] = "Bearer " + cle
    entetes.update(extra or {})
    url = lire_secret("SUPABASE_URL").rstrip("/") + "/rest/v1/kv"
    try:
        r = requests.request(methode, url, headers=entetes, params=params, json=json_corps, timeout=20)
        r.raise_for_status()
        return r
    except Exception as err:
        st.error("Base de données inaccessible ou mal configurée. Vérifiez les secrets "
                 "SUPABASE_URL et SUPABASE_KEY, la table « kv » et que le projet n'est pas en pause.")
        st.caption(f"Détail technique : {err}")
        st.stop()


@st.cache_data(ttl=10, show_spinner=False)
def db_lire(nom):
    lignes = _db_requete("GET", params={"nom": f"eq.{nom}", "select": "contenu"}).json()
    return lignes[0]["contenu"] if lignes else None


def db_ecrire(nom, contenu):
    _db_requete("POST", params={"on_conflict": "nom"}, json_corps={"nom": nom, "contenu": contenu},
                extra={"Prefer": "resolution=merge-duplicates"})
    st.cache_data.clear()


def db_supprimer(nom):
    _db_requete("DELETE", params={"nom": f"eq.{nom}"})
    st.cache_data.clear()


def charger(nom):
    df = None
    if base_en_ligne():
        contenu = db_lire(nom)
        if contenu and contenu.strip():
            df = pd.read_csv(io.StringIO(contenu), dtype=str).fillna("")
    elif os.path.exists(f"{nom}.csv"):
        df = pd.read_csv(f"{nom}.csv", dtype=str).fillna("")
    if df is None:
        return pd.DataFrame(columns=FICHIERS[nom])
    for c in FICHIERS[nom]:
        if c not in df.columns:
            df[c] = ""
    if nom == "etudiants" and "frais_dus" in df.columns:    # anciennes données : frais -> « en ordre »
        dus = pd.to_numeric(df["frais_dus"], errors="coerce").fillna(0)
        pay = pd.to_numeric(df["frais_payes"], errors="coerce").fillna(0) if "frais_payes" in df.columns else dus * 0
        vide = df["en_ordre"].astype(str).str.strip() == ""
        df.loc[vide & (dus > 0) & (pay >= dus), "en_ordre"] = "1"
        df = df.drop(columns=[c for c in ("frais_dus", "frais_payes") if c in df.columns])
    return df


def est_en_ordre(e):
    return str(e.get("en_ordre", "")).strip() == "1"


def sauver(nom, df):
    if base_en_ligne():
        db_ecrire(nom, df.to_csv(index=False))
    else:
        df.to_csv(f"{nom}.csv", index=False)


def fusionner(df, nouveau, cles):
    return pd.concat([df, nouveau], ignore_index=True).drop_duplicates(subset=cles, keep="last")


def params():
    base = {"deliberation_publiee": False, "bloquer_impaye": True, "annee": "2025-2026"}
    if base_en_ligne():
        contenu = db_lire("parametres")
        if contenu:
            base.update(json.loads(contenu))
    elif os.path.exists(PARAMS):
        with open(PARAMS, encoding="utf-8") as f:
            base.update(json.load(f))
    return base


def sauver_params(p):
    if base_en_ligne():
        db_ecrire("parametres", json.dumps(p))
    else:
        with open(PARAMS, "w", encoding="utf-8") as f:
            json.dump(p, f)


def trouver_logo():
    """Logo de l'université : base en ligne si disponible, sinon logo.png / logo.jpg à côté du programme."""
    if base_en_ligne():
        contenu = db_lire("logo")
        if contenu:
            donnees = base64.b64decode(contenu)
            chemin = os.path.join(DOSSIER_APP, "logo_base.png")
            if not os.path.exists(chemin) or open(chemin, "rb").read() != donnees:
                with open(chemin, "wb") as f:
                    f.write(donnees)
            return chemin
    for nom in ("logo.png", "logo.jpg", "logo.jpeg"):
        chemin = os.path.join(DOSSIER_APP, nom)
        if os.path.exists(chemin):
            return chemin
    return None


def sauver_logo(fichier):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(fichier)).convert("RGBA")
    img.thumbnail((800, 800))
    tampon = io.BytesIO()
    img.save(tampon, "PNG")
    supprimer_logo()
    if base_en_ligne():
        db_ecrire("logo", base64.b64encode(tampon.getvalue()).decode())
    else:
        with open(os.path.join(DOSSIER_APP, "logo.png"), "wb") as f:
            f.write(tampon.getvalue())


def supprimer_logo():
    for nom in ("logo.png", "logo.jpg", "logo.jpeg", "logo_base.png"):
        chemin = os.path.join(DOSSIER_APP, nom)
        if os.path.exists(chemin):
            os.remove(chemin)
    if base_en_ligne():
        db_supprimer("logo")


def nom_complet(e):
    """Nom + post-nom + prénom (les parties vides sont ignorées)."""
    return " ".join(str(e.get(c, "") or "").strip() for c in ("nom", "postnom", "prenom")
                    if str(e.get(c, "") or "").strip())


def sauver_universites(df):
    """Enregistre les universités et fige la liste : l'université par défaut ne revient plus ensuite."""
    p = params()
    if not p.get("seed_fait"):
        if charger("facultes").empty:            # enregistre la faculté par défaut avant de figer
            sauver("facultes", facultes_df())
        p["seed_fait"] = True
        sauver_params(p)
    sauver("universites", df)


def universites_df():
    """Universités enregistrées ; Unikol est proposée par défaut tant que rien n'a été enregistré."""
    df = charger("universites")
    if df.empty and not params().get("seed_fait"):
        df = pd.DataFrame([[UNIVERSITE, "UNIKOL", PAYS, "", "", ""]], columns=FICHIERS["universites"])
    return df


def _cle_logo(nom):
    return "logo_" + re.sub(r"[^\w\-]", "_", _norm(nom))


def logo_universite(nom):
    """Chemin du logo de cette université (None si aucun). Unikol retombe sur l'ancien logo global."""
    cle = _cle_logo(nom)
    chemin = os.path.join(DOSSIER_APP, cle + ".png")
    if base_en_ligne():
        contenu = db_lire(cle)
        if contenu:
            donnees = base64.b64decode(contenu)
            if not os.path.exists(chemin) or open(chemin, "rb").read() != donnees:
                with open(chemin, "wb") as f:
                    f.write(donnees)
            return chemin
    elif os.path.exists(chemin):
        return chemin
    if nom == UNIVERSITE:
        return trouver_logo()
    return None


def sauver_logo_universite(nom, fichier):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(fichier)).convert("RGBA")
    img.thumbnail((800, 800))
    tampon = io.BytesIO()
    img.save(tampon, "PNG")
    cle = _cle_logo(nom)
    if base_en_ligne():
        db_ecrire(cle, base64.b64encode(tampon.getvalue()).decode())
    with open(os.path.join(DOSSIER_APP, cle + ".png"), "wb") as f:
        f.write(tampon.getvalue())


def supprimer_logo_universite(nom):
    chemin = os.path.join(DOSSIER_APP, _cle_logo(nom) + ".png")
    if os.path.exists(chemin):
        os.remove(chemin)
    if base_en_ligne():
        db_supprimer(_cle_logo(nom))


def infos_universite(e):
    """Université de l'étudiant (par sa faculté) : nom, sigle, pays, logo."""
    fac_df = facultes_df()
    cible = faculte_de(e).lower()
    nom = UNIVERSITE
    for _, r in fac_df.iterrows():
        if faculte_de({"faculte": r["faculte"]}).lower() == cible:
            nom = r["universite"] or UNIVERSITE
            break
    u = universites_df()
    ligne = u[u["universite"] == nom]
    sigle, pays, adresse, email, tel = ((ligne.iloc[0]["sigle"], ligne.iloc[0]["pays"], ligne.iloc[0]["adresse"],
                                         ligne.iloc[0]["email"], ligne.iloc[0]["telephone"])
                                        if not ligne.empty else ("", PAYS, "", "", ""))
    return {"nom": nom, "sigle": sigle, "pays": pays or PAYS, "adresse": adresse, "email": email,
            "telephone": tel, "logo": logo_universite(nom)}


def faculte_de(e):
    """Faculté de l'étudiant (sans article) ; faculté par défaut si non renseignée."""
    f = str(e.get("faculte", "") or "").strip()
    if f.lower().startswith("la "):
        f = f[3:].strip()
    return f or FACULTE


def faculte_entete(fac):
    return "La " + fac if fac.startswith("Faculté") else fac


def a_la_faculte(fac):
    return "à la " + fac if fac.startswith("Faculté") else "à " + fac


# ---------- Facultés, matricules et codes personnels ----------
def liste_promos(texte):
    return [x.strip() for x in str(texte).split(",") if x.strip()]


def facultes_df():
    """Facultés enregistrées (nom, code, promotions). Si aucune, la faculté par défaut est proposée."""
    df = charger("facultes")
    if df.empty and not params().get("seed_fait"):
        et = charger("etudiants")
        promos = []
        if not et.empty:
            promos = sorted({r["promotion"] for _, r in et.iterrows()
                             if r["promotion"] and faculte_de(r) == FACULTE})
        df = pd.DataFrame([[FACULTE, CODE_FACULTE_PAR_DEFAUT, ", ".join(promos), UNIVERSITE]],
                          columns=FICHIERS["facultes"])
    df = df.copy()
    df.loc[df["universite"].astype(str).str.strip() == "", "universite"] = UNIVERSITE
    return df


def code_faculte(fac_df, nom):
    cible = faculte_de({"faculte": nom}).lower()
    for _, r in fac_df.iterrows():
        if faculte_de({"faculte": r["faculte"]}).lower() == cible:
            return str(r["code"]).strip()
    return ""


def ajouter_promotion(nom_fac, promo):
    """Ajoute la promotion à la liste de la faculté si elle n'y est pas encore."""
    fac_df = facultes_df()
    cible = faculte_de({"faculte": nom_fac}).lower()
    for i, r in fac_df.iterrows():
        if faculte_de({"faculte": r["faculte"]}).lower() == cible:
            promos = liste_promos(r["promotions"])
            if promo not in promos:
                fac_df.at[i, "promotions"] = ", ".join(promos + [promo])
                sauver("facultes", fac_df)
            return


def suffixe_annee(annee):
    """'2024-2025' -> '245' : les 2 derniers chiffres de la 1re année + le dernier chiffre de la 2e."""
    ans = re.findall(r"\d{4}", str(annee))
    if len(ans) >= 2:
        a, b = ans[0], ans[1]
    else:
        a = ans[0] if ans else str(date.today().year)
        b = str(int(a) + 1)
    return a[-2:] + b[-1]


def prefixe_matricule(code_fac, annee):
    return f"{code_fac}{suffixe_annee(annee)}"


def prochain_numero(existants, prefixe):
    nums = []
    for m in existants["matricule"]:
        m = str(m)
        if m.lower().startswith(prefixe.lower()) and m[len(prefixe):].isdigit():
            nums.append(int(m[len(prefixe):]))
    return (max(nums) if nums else 0) + 1


def nouveau_matricule(existants, code_fac, annee, numero=None):
    """Matricule = code faculté + année académique (ex. 245) + numéro sur la liste d'inscription.
    Ex. 51/kz245415. Retourne (matricule, numero) ou (None, numero) si le numéro est déjà pris."""
    prefixe = prefixe_matricule(code_fac, annee)
    if numero:
        mat = f"{prefixe}{int(numero):03d}"
        deja = existants["matricule"].astype(str).str.lower().tolist()
        return (None, int(numero)) if mat.lower() in deja else (mat, int(numero))
    n = prochain_numero(existants, prefixe)
    return f"{prefixe}{n:03d}", n


def generer_code(longueur=6):
    """Mot de passe personnel aléatoire (sans caractères ambigus : 0/O, 1/I/L)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(longueur))


def completer_identifiants(new, existant, annee, numeros=None, avis=None):
    """Remplit les matricules et codes vides d'une liste d'étudiants. Retourne (liste, erreurs).
    numeros : {ligne: rang sur la liste d'inscription} (1er nom = 001, 2e = 002, etc.). Si ce numéro est déjà
    pris par un autre étudiant (ex. autre promotion de la même année), le prochain numéro libre est utilisé
    et un message est ajouté à la liste « avis »."""
    fac_df = facultes_df()
    tout = existant[["matricule"]].copy()
    erreurs = []
    new = new.copy()
    for i, r in new.iterrows():
        if not str(r["code"]).strip():
            new.at[i, "code"] = generer_code()
        if not str(r["matricule"]).strip():
            code_f = code_faculte(fac_df, r["faculte"])
            if not code_f:
                erreurs.append(f"{nom_complet(r)} : faculté « {faculte_de(r)} » absente de la liste des facultés.")
                continue
            rang = (numeros or {}).get(i)
            mat, _ = nouveau_matricule(tout, code_f, annee, rang)
            if mat is None:   # numéro de la liste déjà pris : on prend le prochain libre
                mat, _ = nouveau_matricule(tout, code_f, annee)
                if avis is not None:
                    avis.append(f"{nom_complet(r)} : le numéro {rang:03d} était déjà pris, matricule {mat} attribué.")
            new.at[i, "matricule"] = mat
            tout = pd.concat([tout, pd.DataFrame([{"matricule": mat}])], ignore_index=True)
    return new, erreurs



def data_uri(chemin):
    if not chemin or not os.path.exists(chemin):
        return ""
    mime = "image/png" if chemin.lower().endswith(".png") else "image/jpeg"
    with open(chemin, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


# Couleurs de la carte : bleu ciel, texte bleu marine
CIEL, CIEL_CLAIR, MARINE_CARTE, BLEU_LABEL = "#87ceeb", "#bfe8fa", "#0b2545", "#0b4f8a"


def code_barres_uri(valeur):
    """Code-barres Code128 du matricule (SVG en data-URI). Chaîne vide si indisponible."""
    try:
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode import createBarcodeDrawing
        dessin = createBarcodeDrawing("Code128", value=str(valeur), barHeight=34, barWidth=1.1,
                                      humanReadable=False, quiet=False)
        svg = renderSVG.drawToString(dessin)
        return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()
    except Exception:
        return ""


def carte_html(e, profil, annee, photo, univ=None):
    """Recto de la carte d'étudiant (bleu ciel) affichée dans l'application."""
    from html import escape as esc
    univ = univ or infos_universite(e)
    logo, ph = data_uri(univ["logo"]), data_uri(photo)
    img_logo = (f'<img src="{logo}" style="height:46px;width:auto;">' if logo else "")
    img_photo = (f'<img src="{ph}" style="width:100%;height:100%;object-fit:cover;">' if ph
                 else '<div style="color:#64748b;font-size:12px;text-align:center;'
                      'padding-top:55px;">PHOTO</div>')

    def ligne(libelle, valeur):
        return (f'<div><span style="color:{BLEU_LABEL};font-weight:700;">{libelle} :</span> '
                f'{esc(str(valeur) if str(valeur).strip() else "-")}</div>')

    naissance = f"{profil['date_naissance'] or '-'} à {profil['lieu_naissance'] or '-'}"
    morceaux = [
        '<div style="max-width:440px;border-radius:16px;overflow:hidden;margin-bottom:14px;'
        f'box-shadow:0 4px 16px rgba(0,0,0,.25);font-family:Arial,sans-serif;color:{MARINE_CARTE};'
        f'background:linear-gradient(160deg,{CIEL_CLAIR} 0%,{CIEL} 100%);">',
        f'<div style="background:#fff;padding:10px 14px;display:flex;align-items:center;'
        f'gap:12px;border-bottom:4px solid {MARINE_CARTE};">', img_logo,
        f'<div><div style="color:{MARINE_CARTE};font-weight:700;font-size:15px;">'
        f'{esc(univ["nom"])}</div>'
        f'<div style="color:{BLEU_LABEL};font-weight:700;font-size:11px;letter-spacing:2px;">'
        "CARTE D'ÉTUDIANT</div></div></div>",
        '<div style="display:flex;gap:16px;padding:16px;">',
        f'<div style="width:100px;height:128px;flex-shrink:0;border:3px solid {MARINE_CARTE};'
        f'border-radius:8px;background:#f1f5f9;overflow:hidden;">{img_photo}</div>',
        '<div style="flex:1;font-size:13px;line-height:1.8;">',
        f'<div style="font-size:17px;font-weight:700;margin-bottom:6px;">{esc(nom_complet(e).upper())}</div>',
        f'<div style="display:inline-block;background:{MARINE_CARTE};color:#fff;font-weight:700;'
        f'padding:2px 10px;border-radius:20px;margin-bottom:4px;">{esc(str(e["matricule"]))}</div>',
        ligne("Promotion", e["promotion"]),
        ligne("Né(e) le", naissance), ligne("Nationalité", profil["nationalite"]),
        "</div></div>",
        f'<div style="background:{MARINE_CARTE};color:#fff;padding:7px 14px;font-size:12px;'
        'font-weight:700;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;">'
        f'<span>{esc(faculte_de(e))}</span><span>Année académique {esc(str(annee))}</span></div>',
        "</div>",
    ]
    return "".join(morceaux)


def carte_verso_html(e, profil, annee, univ=None):
    """Verso de la carte : fond bleu ciel, bordure noire, coordonnées de l'université."""
    from html import escape as esc
    univ = univ or infos_universite(e)
    logo = data_uri(univ["logo"])
    img_logo = (f'<img src="{logo}" style="height:34px;width:auto;">' if logo else "")

    def contact(libelle, valeur):
        v = str(valeur or "").strip()
        return (f'<div><span style="color:{BLEU_LABEL};font-weight:700;">{libelle} :</span> '
                f'{esc(v) if v else "-"}</div>')

    barres = code_barres_uri(e["matricule"])
    img_barres = (f'<img src="{barres}" style="height:38px;max-width:100%;">' if barres else "")
    return "".join([
        '<div style="max-width:440px;border-radius:16px;overflow:hidden;box-sizing:border-box;'
        f'border:3px solid #000;font-family:Arial,sans-serif;color:{MARINE_CARTE};'
        f'background:linear-gradient(200deg,{CIEL_CLAIR} 0%,{CIEL} 100%);">',
        '<div style="background:#000;color:#fff;padding:7px 14px;display:flex;align-items:center;gap:10px;'
        'font-size:12px;font-weight:700;letter-spacing:1px;">',
        f'<span style="background:#fff;border-radius:6px;padding:2px 4px;display:flex;">{img_logo}</span>'
        if img_logo else "",
        f'<span>{esc(univ["nom"])}</span></div>',
        '<div style="padding:14px 16px;font-size:12.5px;line-height:1.7;">',
        f'<div style="font-weight:700;letter-spacing:1px;margin-bottom:4px;color:{BLEU_LABEL};">'
        'COORDONNÉES DE L\'UNIVERSITÉ</div>',
        contact("Adresse", univ.get("adresse")), contact("Email", univ.get("email")),
        contact("Tél.", univ.get("telephone")),
        '<div style="border-top:1px solid #000;margin:10px 0 8px;"></div>',
        '<div style="font-size:11.5px;line-height:1.5;">Cette carte est strictement personnelle et '
        "incessible. Elle doit être présentée à toute demande de l'administration. En cas de perte ou si "
        "vous trouvez cette carte, merci de la rapporter à l'adresse ci-dessus.</div>",
        '<div style="display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-top:12px;">',
        '<div style="flex:1;"><div style="border-bottom:1px solid #000;height:30px;"></div>'
        '<div style="font-size:10.5px;margin-top:2px;">Signature du titulaire</div></div>',
        '<div style="flex:1;"><div style="border-bottom:1px solid #000;height:30px;"></div>'
        "<div style=\"font-size:10.5px;margin-top:2px;\">Signature du recteur</div></div></div>",
        f'<div style="text-align:center;margin-top:12px;background:#fff;border-radius:6px;padding:6px;">'
        f'{img_barres}<div style="font-size:11px;letter-spacing:2px;font-weight:700;">'
        f'{esc(str(e["matricule"]))}</div></div>',
        "</div>",
        f'<div style="background:#000;color:#fff;padding:6px 14px;font-size:11px;font-weight:700;'
        f'text-align:center;">Valable pour l\'année académique {esc(str(annee))}</div>',
        "</div>"])


def nombre(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return 0.0


def _cle_photo(mat):
    return re.sub(r"[^\w\-]", "_", str(mat))


def chemin_photo(mat):
    chemin = os.path.join(DOSSIER_PHOTOS, _cle_photo(mat) + ".jpg")
    if not os.path.exists(chemin) and base_en_ligne():
        contenu = db_lire("photo_" + _cle_photo(mat))
        if contenu:
            with open(chemin, "wb") as f:
                f.write(base64.b64decode(contenu))
    return chemin


def sauver_photo(mat, fichier):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(fichier)).convert("RGB")
    img.thumbnail((480, 600))
    chemin = os.path.join(DOSSIER_PHOTOS, _cle_photo(mat) + ".jpg")
    img.save(chemin, "JPEG", quality=88)
    if base_en_ligne():
        with open(chemin, "rb") as f:
            db_ecrire("photo_" + _cle_photo(mat), base64.b64encode(f.read()).decode())


def profil_de(mat):
    pr = charger("profils")
    ligne = pr[pr["matricule"].str.lower() == str(mat).lower()]
    base = {c: "" for c in FICHIERS["profils"]}
    if not ligne.empty:
        base.update(ligne.iloc[0].to_dict())
    return base


def cours_de(co, promo, fac):
    """Cours d'une promotion dans UNE faculté (les cours sans faculté valent pour toutes)."""
    cible = faculte_de({"faculte": fac}).lower()
    ok = co["faculte"].map(lambda f: (not str(f).strip()) or faculte_de({"faculte": f}).lower() == cible)
    return co[(co["promotion"] == promo) & ok.astype(bool)]


def bilan(mat, promo, co, no, fac=""):
    """Cours de la promotion + note de l'étudiant, moyenne pondérée, crédits."""
    c = cours_de(co, promo, fac).copy()
    c["_k"] = c["intitule"].map(_norm)
    n = no[no["matricule"] == mat].copy()
    n["_k"] = n["cours"].map(_norm)
    n = n.drop_duplicates("_k", keep="last")[["_k", "moyenne", "examen"]]
    d = c.merge(n, on="_k", how="left").drop(columns=["_k"])
    for c in ("credits", "heures"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    d["moyenne"] = pd.to_numeric(d["moyenne"], errors="coerce")   # /10 (interros, TP, TD...)
    d["examen"] = pd.to_numeric(d["examen"], errors="coerce")     # /10
    d["note"] = (d["moyenne"] + d["examen"]).round(2)                        # cote du cours /20 (vide si un des deux manque)
    d["total"] = d["note"] * d["credits"]
    d["statut"] = d["note"].apply(
        lambda x: "—" if pd.isna(x) else ("Validé" if x >= 10 else "Non validé"))
    notes = d.dropna(subset=["note"])
    cr = notes["credits"].sum()
    moyenne = (notes["note"] * notes["credits"]).sum() / cr if cr else None
    acquis = d.loc[d["note"] >= 10, "credits"].sum()
    return d, moyenne, acquis, d["credits"].sum()


def complet(d):
    """Vrai si toutes les cotes /20 des cours de la promotion sont disponibles."""
    return len(d) > 0 and bool(d["note"].notna().all())


def mention(acquis, d):
    if not complet(d):
        return "—"
    return "Excellent" if acquis >= seuil_credits(d) else "Insuffisant"


def decision(acquis, d):
    if not complet(d):
        return "Non disponible"
    return "ADMIS" if acquis >= seuil_credits(d) else "DÉFAILLANT"


def couleur_note(v):
    """< 10 : rouge (non validé) ; = 10 : bleu (validé) ; > 10 : noir (couleur par défaut)."""
    if pd.isna(v):
        return ""
    if v < 10:
        return "color: red; font-weight: bold"
    if v == 10:
        return "color: blue; font-weight: bold"
    return ""


def colorer(df, col):
    sty = df.style
    appliquer = sty.map if hasattr(sty, "map") else sty.applymap
    sty = appliquer(couleur_note, subset=[col])
    return sty.format({"Crédits": "{:g}", "Moyenne /10": "{:.2f}", "Examen /10": "{:.2f}",
                       col: "{:.2f}"}, na_rep="-")


# ---------- Archivage des délibérations ----------
COLONNES_DETAIL = ["intitule", "credits", "heures", "moyenne", "examen", "note"]


def archiver_deliberation(et, co, no, annee, session, publie):
    """Photographie des résultats de chaque étudiant, conservée même si les notes changent ensuite."""
    horodatage = datetime.now().strftime("%Y-%m-%d %H:%M")
    lignes = []
    for rang, (_, e) in enumerate(et.iterrows(), start=1):
        d, moy, acq, tot = bilan(e["matricule"], e["promotion"], co, no, faculte_de(e))
        det = d[COLONNES_DETAIL].astype(object)
        det = det.where(det.notna(), None)
        lignes.append([e["matricule"], str(annee), session, e["promotion"], faculte_de(e),
                       f"{rang:03d}", "" if moy is None else f"{moy:.2f}", f"{acq:g}", f"{tot:g}",
                       mention(acq, d), decision(acq, d), horodatage, "1" if publie else "0",
                       json.dumps(det.to_dict("records"), ensure_ascii=False)])
    return pd.DataFrame(lignes, columns=FICHIERS["deliberations"])


def bilan_depuis_archive(r):
    """Reconstitue (d, moyenne, acquis, total_credits) à partir d'une délibération enregistrée."""
    brut = json.loads(r["details"]) if str(r["details"]).strip() else []
    d = pd.DataFrame(brut, columns=COLONNES_DETAIL)
    for c in COLONNES_DETAIL[1:]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["total"] = d["note"] * d["credits"]
    moyenne = None if not str(r["moyenne"]).strip() else nombre(r["moyenne"])
    return d, moyenne, nombre(r["credits"]), nombre(r["total_credits"])


# ---------- PDF : bulletin et carte ----------
def bulletin_pdf(e, profil, d, moyenne, acquis, total_cr, annee, numero="", session="", univ=None, date_doc=""):
    """Bulletin coloré avec le logo de l'université. Ni photo, ni informations personnelles (sexe, naissance, adresse...)."""
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    marine = colors.HexColor("#1f3a5f")
    sarcelle = colors.HexColor("#0f766e")
    dore = colors.HexColor("#f2b705")
    ciel = colors.HexColor("#e6f0f8")
    menthe = colors.HexColor("#e3f4f1")
    creme = colors.HexColor("#fff6d6")

    def x(v):
        return escape(str(v)) if str(v).strip() else "-"

    s = getSampleStyleSheet()
    buf = io.BytesIO()
    univ = univ or infos_universite(e)

    def decor(canvas, doc_):
        """Bandes de couleur en haut et en bas de chaque page + cadre fin."""
        larg, haut = A4
        canvas.saveState()
        # Filigrane : promotion de l'étudiant, en diagonale derrière le contenu
        texte_f = str(e["promotion"]).upper()
        taille = 70
        largeur_t = stringWidth(texte_f, "Helvetica-Bold", taille)
        if largeur_t > 430:
            taille = taille * 430 / largeur_t
        canvas.saveState()
        canvas.setFillColor(marine)
        canvas.setFillAlpha(0.12)
        canvas.setFont("Helvetica-Bold", taille)
        canvas.translate(larg / 2, haut / 2)
        canvas.rotate(45)
        canvas.drawCentredString(0, 0, texte_f)
        canvas.restoreState()
        canvas.setFillColor(marine)
        canvas.rect(0, haut - 14, larg, 14, fill=1, stroke=0)
        canvas.setFillColor(dore)
        canvas.rect(0, haut - 18, larg, 4, fill=1, stroke=0)
        canvas.setFillColor(sarcelle)
        canvas.rect(0, 0, larg, 14, fill=1, stroke=0)
        canvas.setFillColor(dore)
        canvas.rect(0, 14, larg, 3, fill=1, stroke=0)
        canvas.setStrokeColor(marine)
        canvas.setLineWidth(0.8)
        canvas.rect(28, 28, larg - 56, haut - 64, fill=0, stroke=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(larg / 2, 4.5, f"page {doc_.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=42, rightMargin=42,
                            topMargin=34, bottomMargin=40, title=f"Bulletin {e['matricule']}")
    blanc_centre = ParagraphStyle("bc", parent=s["Normal"], alignment=TA_CENTER, textColor=colors.white,
                                  fontName="Helvetica-Bold", fontSize=10, leading=14)
    blanc_grand = ParagraphStyle("bg", parent=blanc_centre, fontSize=14, leading=18)
    dore_centre = ParagraphStyle("dc", parent=blanc_centre, textColor=dore, fontSize=11)
    titre = ParagraphStyle("titre", parent=s["Normal"], alignment=TA_CENTER, textColor=colors.white,
                           fontName="Helvetica-Bold", fontSize=15, leading=20)
    sous_titre = ParagraphStyle("st", parent=titre, textColor=dore, fontSize=10, leading=13)
    texte = ParagraphStyle("texte", parent=s["Normal"], alignment=TA_JUSTIFY, fontSize=10.5, leading=15)
    entete_tab = ParagraphStyle("et", parent=s["Normal"], alignment=TA_CENTER, textColor=colors.white,
                                fontName="Helvetica-Bold", fontSize=9, leading=11)

    fac = faculte_de(e)
    civilite = {"Masculin": "Monsieur", "Féminin": "Madame"}.get(profil["sexe"], "Monsieur/Madame")
    largeur = A4[0] - 84   # largeur utile

    # --- Bandeau d'en-tête : logo (cadre blanc) + textes officiels (fond bleu marine)
    textes = [Paragraph(escape(univ["pays"].upper()), blanc_centre), Paragraph(escape(univ["nom"]), blanc_grand),
              Paragraph(escape(faculte_entete(fac)), dore_centre)]
    logo = univ["logo"]
    if logo:
        larg_l, haut_l = ImageReader(logo).getSize()
        img = Image(logo, width=62 * larg_l / haut_l, height=62)
        bandeau = Table([[img, textes]], colWidths=[100, largeur - 100])
        bandeau.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.white), ("BACKGROUND", (1, 0), (1, 0), marine),
            ("ALIGN", (0, 0), (0, 0), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 1.5, dore), ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    else:
        bandeau = Table([[textes]], colWidths=[largeur])
        bandeau.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), marine),
                                     ("BOX", (0, 0), (-1, -1), 1.5, dore),
                                     ("TOPPADDING", (0, 0), (-1, -1), 10),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))

    # --- Titre sur fond sarcelle
    lib_session = f" · {escape(session)}" if session else ""
    bloc_titre = Table([[[Paragraph(f"BULLETIN DE COTES N° {x(numero) if numero else ''}", titre),
                          Paragraph(f"Année académique {x(annee)}{lib_session}", sous_titre)]]],
                       colWidths=[largeur])
    bloc_titre.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), sarcelle),
                                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))

    el = [bandeau, Spacer(1, 8), bloc_titre, Spacer(1, 10),
          Paragraph(
              f"{civilite} <b>{x(nom_complet(e).upper())}</b>, matricule <b>{x(e['matricule'])}</b>, "
              f"a obtenu à l'issue de l'année académique <b>{x(annee)}</b> les cotes "
              f"ci-dessous reprises aux examens portant sur les matières prévues au programme "
              f"de <b>{x(e['promotion'])}</b> {escape(a_la_faculte(fac))}.", texte),
          Spacer(1, 10)]

    # Pas de bloc d'identité sur le bulletin (sexe, naissance, nationalité, adresse, téléphone retirés)
    el += [Spacer(1, 4)]

    def fmt(v):
        return "-" if pd.isna(v) else f"{v:.2f}"

    styles_cotes = []
    lignes = [[Paragraph(h, entete_tab) for h in
               ("Matière suivie", "Crédits", "Moy. /10", "Exam. /10", "Total /20")]]
    for i, (_, r) in enumerate(d.iterrows(), start=1):
        if not pd.isna(r["note"]) and r["note"] < 10:
            styles_cotes += [("TEXTCOLOR", (4, i), (4, i), colors.red),
                             ("FONTNAME", (4, i), (4, i), "Helvetica-Bold")]
        elif not pd.isna(r["note"]) and r["note"] == 10:
            styles_cotes += [("TEXTCOLOR", (4, i), (4, i), colors.blue),
                             ("FONTNAME", (4, i), (4, i), "Helvetica-Bold")]
        lignes.append([Paragraph(escape(str(r["intitule"])), s["Normal"]),
                       f"{r['credits']:g}", fmt(r["moyenne"]), fmt(r["examen"]),
                       fmt(r["note"])])   # Total /20 = moyenne + examen
    lignes.append(["TOTAL", f"{total_cr:g}", "", "", ""])
    tableau = Table(lignes, colWidths=[largeur - 45 - 52 * 3, 45, 52, 52, 52],
                    repeatRows=1)
    tableau.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), marine),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [None, menthe]),
        ("BACKGROUND", (0, -1), (-1, -1), ciel),
        ("TEXTCOLOR", (0, -1), (-1, -1), marine),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9fb3c8")),
        ("LINEBELOW", (0, 0), (-1, 0), 2, sarcelle),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ] + styles_cotes))
    el += [tableau, Spacer(1, 16)]

    dec = "Non disponible"
    if complet(d):
        dec = "ADMIS" if acquis >= seuil_credits(d) else "DÉFAILLANT"
    couleur_dec = {"ADMIS": colors.HexColor("#15803d"),
                   "DÉFAILLANT": colors.HexColor("#b91c1c")}.get(dec, marine)
    # crédits des cours cotés mais non validés (cote < 10/20)
    non_valides = float(pd.to_numeric(d.loc[d["note"] < 10, "credits"], errors="coerce").fillna(0).sum())
    resume = [["Moyenne annuelle", "-" if moyenne is None else f"{moyenne:.2f} / 20"],
              ["Mention", mention(acquis, d) if complet(d) else "Non disponible"],
              ["Décision", dec],
              ["Pourcentage", "-" if moyenne is None else f"{moyenne / 20 * 100:.1f} %"],
              [f"Crédits validés sur {total_cr:g}", f"{acquis:g} / {total_cr:g}"],
              ["Crédits non validés", f"{non_valides:g} / {total_cr:g}"]]
    res = Table(resume, colWidths=[200, 200], hAlign="CENTER")
    res.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), sarcelle), ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("BACKGROUND", (1, 0), (1, -1), creme),
        ("TEXTCOLOR", (1, 0), (1, -1), marine),
        ("TEXTCOLOR", (1, 2), (1, 2), couleur_dec),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (1, 2), (1, 2), 12),
        ("GRID", (0, 0), (-1, -1), 0.5, marine), ("BOX", (0, 0), (-1, -1), 1.5, dore),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    date_txt = date_fr(date_doc, False) if date_doc else date.today().strftime("%d/%m/%Y")
    fin = ParagraphStyle("fin", parent=s["Italic"], alignment=TA_LEFT, fontSize=8,
                         textColor=colors.HexColor("#555555"))
    el += [res, Spacer(1, 26),
           Paragraph(f"Document généré par {escape(NOM)} le {date_txt}", fin)]
    doc.build(el, onFirstPage=decor, onLaterPages=decor)
    return buf.getvalue()


def carte_pdf(e, profil, annee, photo=None, univ=None):
    """Carte en PDF de 2 pages : recto (bleu ciel) puis verso (bleu ciel, bordure noire)."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    univ = univ or infos_universite(e)
    larg, haut = 85.6 * mm, 54 * mm
    marine = colors.HexColor(MARINE_CARTE)
    bleu_label = colors.HexColor(BLEU_LABEL)
    ciel = colors.HexColor(CIEL)
    ciel_clair = colors.HexColor(CIEL_CLAIR)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(larg, haut))

    def fond_degrade():
        n = 60
        for i in range(n):
            t = i / (n - 1)
            c.setFillColorRGB(ciel_clair.red * (1 - t) + ciel.red * t,
                              ciel_clair.green * (1 - t) + ciel.green * t,
                              ciel_clair.blue * (1 - t) + ciel.blue * t)
            c.rect(0, haut * (1 - (i + 1) / n), larg, haut / n + 0.3, fill=1, stroke=0)

    # ===== RECTO =====
    fond_degrade()
    c.setFillColor(colors.white)
    c.rect(0, haut - 12 * mm, larg, 12 * mm, fill=1, stroke=0)
    c.setFillColor(marine)
    c.rect(0, haut - 13.2 * mm, larg, 1.2 * mm, fill=1, stroke=0)
    x_texte = 4 * mm
    logo = univ["logo"]
    if logo:
        c.drawImage(logo, 3 * mm, haut - 11 * mm, width=10 * mm, height=10 * mm,
                    preserveAspectRatio=True, anchor="c", mask="auto")
        x_texte = 15 * mm
    c.setFillColor(marine)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(x_texte, haut - 6 * mm, univ["nom"][:40])
    c.setFillColor(bleu_label)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(x_texte, haut - 10 * mm, "CARTE D'ÉTUDIANT")

    # Photo avec cadre marine
    c.setFillColor(marine)
    c.rect(3.2 * mm, 8.2 * mm, 23.6 * mm, 29.6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.rect(4 * mm, 9 * mm, 22 * mm, 28 * mm, fill=1, stroke=0)
    if photo and os.path.exists(photo):
        c.drawImage(photo, 4 * mm, 9 * mm, width=22 * mm, height=28 * mm,
                    preserveAspectRatio=True, anchor="c")
    else:
        c.setFillColor(colors.grey)
        c.setFont("Helvetica", 6)
        c.drawCentredString(15 * mm, 22 * mm, "PHOTO")

    nom = nom_complet(e).upper()
    c.setFillColor(marine)
    c.setFont("Helvetica-Bold", 9 if len(nom) <= 24 else 7.5)
    c.drawString(30 * mm, 35.5 * mm, nom[:34])
    naissance = f"{profil['date_naissance'] or '-'}"
    mat_txt = str(e["matricule"])
    c.setFillColor(marine)
    c.roundRect(30 * mm, 29.2 * mm, 7 * mm + c.stringWidth(mat_txt, "Helvetica-Bold", 7.5), 4.2 * mm,
                2 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(33.5 * mm, 30.6 * mm, mat_txt)
    infos = [("Promotion : ", e["promotion"]),
             ("Né(e) le : ", naissance), ("Nationalité : ", profil["nationalite"] or "-")]
    y = 25.6 * mm
    for libelle, valeur in infos:
        c.setFillColor(bleu_label)
        c.setFont("Helvetica-Bold", 6.8)
        c.drawString(30 * mm, y, libelle)
        decal = c.stringWidth(libelle, "Helvetica-Bold", 6.8)
        c.setFillColor(marine)
        c.setFont("Helvetica", 7.2)
        c.drawString(30 * mm + decal, y, str(valeur)[:28])
        y -= 4.6 * mm

    c.setFillColor(marine)
    c.rect(0, 0, larg, 6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 6.3)
    c.drawString(4 * mm, 2.2 * mm, f"Année {annee}")
    c.drawRightString(larg - 4 * mm, 2.2 * mm, faculte_de(e)[:42])
    c.showPage()

    # ===== VERSO =====
    fond_degrade()
    c.setFillColor(colors.black)
    c.rect(0, haut - 8 * mm, larg, 8 * mm, fill=1, stroke=0)
    x_texte = 4 * mm
    if logo:
        c.setFillColor(colors.white)
        c.roundRect(2.5 * mm, haut - 7.2 * mm, 6.4 * mm, 6.4 * mm, 1 * mm, fill=1, stroke=0)
        c.drawImage(logo, 3 * mm, haut - 6.7 * mm, width=5.4 * mm, height=5.4 * mm,
                    preserveAspectRatio=True, anchor="c", mask="auto")
        x_texte = 11 * mm
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 7.2)
    c.drawString(x_texte, haut - 5.2 * mm, univ["nom"][:46])

    y = haut - 12.5 * mm
    c.setFillColor(bleu_label)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(4 * mm, y, "COORDONNÉES DE L'UNIVERSITÉ")
    y -= 3.6 * mm
    for libelle, valeur in (("Adresse : ", univ.get("adresse")), ("Email : ", univ.get("email")),
                            ("Tél. : ", univ.get("telephone"))):
        texte = str(valeur or "").strip() or "-"
        decal = c.stringWidth(libelle, "Helvetica-Bold", 6.4)
        lignes = simpleSplit(texte, "Helvetica", 6.6, larg - 8 * mm - decal)[:2]
        c.setFillColor(bleu_label)
        c.setFont("Helvetica-Bold", 6.4)
        c.drawString(4 * mm, y, libelle)
        c.setFillColor(marine)
        c.setFont("Helvetica", 6.6)
        for k, l in enumerate(lignes):
            c.drawString(4 * mm + decal, y, l)
            if k < len(lignes) - 1:
                y -= 3 * mm
        y -= 3.5 * mm

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.4)
    c.line(4 * mm, y + 1.5 * mm, larg - 4 * mm, y + 1.5 * mm)
    c.setFillColor(marine)
    c.setFont("Helvetica", 5.3)
    avis = ("Carte strictement personnelle et incessible, à présenter à toute demande de "
            "l'administration. En cas de perte ou de découverte, la rapporter à l'adresse ci-dessus.")
    yy = y - 1.2 * mm
    for l in simpleSplit(avis, "Helvetica", 5.3, larg - 8 * mm)[:3]:
        c.drawString(4 * mm, yy, l)
        yy -= 2.4 * mm

    # Signature, cachet, code-barres
    ys = 15.2 * mm
    c.setLineWidth(0.4)
    c.line(4 * mm, ys, 30 * mm, ys)
    c.line(34 * mm, ys, 60 * mm, ys)
    c.setFont("Helvetica", 5)
    c.drawString(4 * mm, ys - 2 * mm, "Signature du titulaire")
    c.drawString(34 * mm, ys - 2 * mm, "Signature du recteur")
    try:
        from reportlab.graphics import renderPDF
        from reportlab.graphics.barcode import createBarcodeDrawing
        bc = createBarcodeDrawing("Code128", value=str(e["matricule"]), barHeight=7 * mm,
                                  barWidth=0.28 * mm, humanReadable=False, quiet=False)
        c.setFillColor(colors.white)
        c.roundRect(62 * mm, 9.3 * mm, 21 * mm, 10.5 * mm, 1 * mm, fill=1, stroke=0)
        facteur = min(1, (19 * mm) / bc.width)
        bc.scale(facteur, 1)
        bc.width *= facteur
        renderPDF.draw(bc, c, 62 * mm + (21 * mm - bc.width) / 2, 12 * mm)
    except Exception:
        pass
    c.setFillColor(colors.black)
    c.rect(0, 0, larg, 6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(4 * mm, 2.2 * mm, str(e["matricule"]))
    c.drawRightString(larg - 4 * mm, 2.2 * mm, f"Valable pour l'année académique {annee}")

    # Bordure noire du verso
    c.setStrokeColor(colors.black)
    c.setLineWidth(2.2)
    c.rect(0.6, 0.6, larg - 1.2, haut - 1.2, fill=0, stroke=1)
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------- Style et navigation ----------
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Public+Sans:wght@400;500;600;700&display=swap');
[data-testid="stApp"] { background:#f1f2f4; color:#2b2f36; font-family:'Public Sans',sans-serif; }
header[data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"], #MainMenu, footer { display:none !important; }
.block-container { padding:0 0 3rem 0 !important; max-width:100% !important; }
.block-container > [data-testid="stVerticalBlock"] { gap:0 !important; }
.st-key-contenu, .st-key-contenu_admin { margin:0 auto; padding:1.4rem 1.2rem; }
.st-key-contenu { max-width:900px; }
.st-key-contenu_admin { max-width:1200px; }
.st-key-contenu [data-testid="stVerticalBlock"], .st-key-contenu_admin [data-testid="stVerticalBlock"] { gap:1rem; }
h1, h2, h3, .serif { font-family:'Fraunces',Georgia,serif !important; color:#2b2f36; }

/* Barre du haut */
.st-key-topbar { position:relative; background:#2b2f36; border-bottom:5px solid #b4b9c2;
  padding:.9rem max(1.2rem, calc((100% - 900px)/2 + 1.2rem)); min-height:62px; }
.marque { display:flex; align-items:center; gap:.7rem; color:#fff; font-family:'Fraunces',Georgia,serif;
  font-weight:700; font-size:1.55rem; line-height:1.2; }
.marque-logo { height:34px; width:auto; background:#fff; border-radius:6px; padding:2px; }
.st-key-menu_box { position:absolute; top:.55rem; right:max(.8rem, calc((100% - 900px)/2 + .8rem)); width:auto !important; }
.st-key-menu_box button { background:transparent !important; border:0 !important; color:#fff !important;
  font-weight:600; box-shadow:none !important; }
[data-testid="stPopoverBody"] { background:#2b2f36 !important; min-width:270px; border:1px solid #3b4049; }
[data-testid="stPopoverBody"] button { background:transparent !important; color:#e5e7eb !important;
  border:0 !important; justify-content:flex-start !important; text-align:left; font-size:1rem; }
[data-testid="stPopoverBody"] button[kind="primary"], [data-testid="stPopoverBody"] [data-testid="stBaseButton-primary"] {
  background:#3b4049 !important; border:1px solid #b4b9c2 !important; color:#fff !important; }
/* Menu compact : toutes les pages (jusqu'à Délibération) et « Se déconnecter » tiennent à l'écran */
[data-testid="stPopoverBody"] { max-height:90vh !important; overflow-y:auto !important; padding:.6rem .8rem !important; }
[data-testid="stPopoverBody"] [data-testid="stVerticalBlock"] { gap:.15rem !important; }
[data-testid="stPopoverBody"] button { min-height:2rem !important; padding:.15rem .7rem !important; }
.nav-t { color:#b4b9c2; letter-spacing:.2em; font-size:.75rem; font-weight:700; margin-bottom:.4rem; }

/* En-tête de page */
.entete { background:#e5e7ea; border-bottom:3px solid #2b2f36;
  padding:1.4rem max(1.4rem, calc((100% - 900px)/2 + 1.4rem)); }
.entete .kicker { color:#b4b9c2; font-family:'Fraunces',Georgia,serif; font-style:italic; font-size:1rem; }
.entete h1 { font-size:2.1rem; margin:.1rem 0 .4rem 0; padding:0; line-height:1.15; }
.entete p { color:#6b7280; margin:0; font-size:1.02rem; }

/* Tableaux */
.tbl-box { overflow-x:auto; }
.tbl { width:100%; border-collapse:collapse; font-size:.98rem; }
.tbl th { font-size:.74rem; letter-spacing:.13em; text-transform:uppercase; color:#4b5563; text-align:center;
  border-top:3px solid #2b2f36; border-bottom:2px solid #2b2f36; padding:.9rem .6rem; font-weight:700; }
.tbl th:first-child, .tbl td:first-child { text-align:left; }
.tbl td { padding:.95rem .6rem; border-bottom:1px solid #c4c8ce; text-align:center; }
.tbl td:first-child { font-family:'Fraunces',Georgia,serif; font-weight:700; color:#2b2f36; }
.tbl tbody tr:nth-child(even) td { background:#eceef1; }
.tbl tr.pied td { border-top:3px solid #2b2f36; font-weight:700; background:#f1f2f4 !important; }

/* Tuiles et titres de section */
.tuiles { display:grid; grid-template-columns:1fr 1fr; gap:.8rem; }
.tuile { background:#fff; border:1px solid #d5d8dd; border-top:3px solid #b4b9c2; padding:.9rem 1rem; }
.tuile .tl { font-size:.72rem; letter-spacing:.13em; text-transform:uppercase; color:#6b7280; font-weight:700; }
.tuile .tv { font-family:'Fraunces',Georgia,serif; font-size:1.3rem; font-weight:700; color:#2b2f36; margin-top:.2rem; }
.sect { font-family:'Fraunces',Georgia,serif; font-size:1.5rem; color:#2b2f36; margin:.6rem 0 .2rem 0; font-weight:700; }
.vide { color:#8a8f99; font-style:italic; font-size:1.1rem; }
.legende { color:#4b5563; font-size:.92rem; }

/* Résultats de délibération */
[class*="st-key-delib_box"] { background:#2b2f36; padding:1.4rem 1.4rem 1.6rem 1.4rem; margin-top:1rem; }
.delib-t { color:#b4b9c2; letter-spacing:.14em; font-weight:700; font-size:.9rem; margin-bottom:1rem; }
.delib-g { display:grid; grid-template-columns:1fr 1fr; gap:1.1rem 1rem; }
.delib-g .l { color:#aab0ba; text-transform:uppercase; font-size:.8rem; letter-spacing:.06em; }
.delib-g .v { color:#fff; font-family:'Fraunces',Georgia,serif; font-size:1.7rem; font-weight:700; }
.delib-g .v.m { font-size:1.25rem; }
[class*="st-key-dl_bulletin"] button { background:#b4b9c2 !important; color:#2b2f36 !important; border:0 !important;
  border-radius:0 !important; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }

/* Boutons et champs */
[data-testid^="stBaseButton-primary"] { background:#2b2f36 !important; border-color:#2b2f36 !important; color:#fff !important; }
[data-testid^="stBaseButton-secondary"] { border-radius:4px; }
</style>
"""


def conteneur(cle):
    try:
        return st.container(key=cle)
    except TypeError:
        return st.container()


def popover_menu():
    try:
        return st.popover("Menu", icon=":material/menu:")
    except TypeError:
        return st.popover("Menu")
    except AttributeError:
        return st.expander("Menu")


def deconnecter():
    for cle in ("etu", "admin", "page"):
        st.session_state.pop(cle, None)
    try:
        st.query_params.clear()
    except Exception:
        pass


def barre_haut(pages=None, connecte=False, logo_chemin=None):
    """Barre marine en haut ; le menu (bouton à droite) apparaît seulement une fois connecté."""
    logo = data_uri(logo_chemin)
    img = f'<img src="{logo}" class="marque-logo">' if logo else ""
    with conteneur("topbar"):
        st.markdown(f'<div class="marque">{img}<span>{esc(NOM)}.</span></div>',
                    unsafe_allow_html=True)
        if connecte:
            with conteneur("menu_box"):
                with popover_menu():
                    st.markdown('<div class="nav-t">NAVIGATION</div>', unsafe_allow_html=True)
                    for cle, libelle in (pages or []):
                        actif = st.session_state.get("page", "tableau") == cle
                        if st.button(libelle, key=f"nav_{cle}", use_container_width=True,
                                     type="primary" if actif else "secondary"):
                            st.session_state["page"] = cle
                            st.rerun()
                    if st.button("Se déconnecter", key="nav_logout", use_container_width=True):
                        deconnecter()
                        st.rerun()


def entete_page(kicker, titre, sous=""):
    st.markdown(f'<div class="entete"><div class="kicker">{esc(kicker)}</div>'
                f'<h1>{esc(titre)}</h1><p>{esc(sous)}</p></div>', unsafe_allow_html=True)


def num(v):
    return "-" if pd.isna(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def cote_html(v):
    """< 10 : rouge ; = 10 : bleu ; > 10 : noir."""
    if pd.isna(v):
        return "-"
    couleur = "#c62828" if v < 10 else "#1d4ed8" if v == 10 else "#2b2f36"
    return f'<b style="color:{couleur}">{v:.2f}</b>'


def tableau_html(entetes, lignes, pied=None):
    th = "".join(f"<th>{esc(h)}</th>" for h in entetes)
    corps = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in l) + "</tr>" for l in lignes)
    bas = ('<tr class="pied">' + "".join(f"<td>{c}</td>" for c in pied) + "</tr>") if pied else ""
    return (f'<div class="tbl-box"><table class="tbl"><thead><tr>{th}</tr></thead>'
            f"<tbody>{corps}{bas}</tbody></table></div>")


def tuiles_html(items):
    return '<div class="tuiles">' + "".join(
        f'<div class="tuile"><div class="tl">{esc(l)}</div><div class="tv">{esc(v)}</div></div>'
        for l, v in items) + "</div>"


def date_fr(horodatage, avec_heure=True):
    """'2026-10-12 14:30' -> '12/10/2026 à 14:30'."""
    try:
        t = datetime.strptime(str(horodatage), "%Y-%m-%d %H:%M")
        return t.strftime("%d/%m/%Y à %H:%M" if avec_heure else "%d/%m/%Y")
    except ValueError:
        return str(horodatage)


def afficher_deliberation(e, profil, d, moyenne, acquis, total_cr, annee, numero, cle, session="", univ=None,
                          date_delib=""):
    """Tableau des cotes + encadré de résultats + bouton du bulletin PDF (sans photo)."""
    lignes = [[esc(str(r["intitule"])), f"{r['credits']:g}", num(r["moyenne"]),
               num(r["examen"]), cote_html(r["note"])] for _, r in d.iterrows()]
    st.markdown(tableau_html(["Cours", "Crédits", "Moyenne /10", "Examen /10", "Total /20"], lignes),
                unsafe_allow_html=True)
    st.markdown('<div class="legende"><b style="color:#c62828">Rouge</b> : moins de '
                '10/20, non validé. <b style="color:#1d4ed8">Bleu</b> : 10/20, validé. '
                "<b>Noir</b> : plus de 10/20, validé.</div>", unsafe_allow_html=True)
    dec = decision(acquis, d)
    couleur_dec = {"ADMIS": "#86efac", "DÉFAILLANT": "#fca5a5"}.get(dec, "#b4b9c2")
    with conteneur(f"delib_box_{cle}"):
        st.markdown(
            '<div class="delib-t">RÉSULTATS DE DÉLIBÉRATION'
            f'{" · " + esc(session.upper()) if session else ""}</div><div class="delib-g">'
            '<div><div class="l">Moyenne</div><div class="v">'
            f'{"-" if moyenne is None else f"{moyenne:.2f}"}/20</div></div>'
            f'<div><div class="l">Crédits</div><div class="v">{acquis:g}/{total_cr:g}</div></div>'
            f'<div><div class="l">Mention</div><div class="v m">{esc(mention(acquis, d))}</div></div>'
            f'<div><div class="l">Décision</div><div class="v m" style="color:{couleur_dec}">'
            f'{esc(dec.upper())}</div></div></div>', unsafe_allow_html=True)
        if not complet(d):
            st.caption("Certaines cotes ne sont pas encore disponibles : "
                       "la décision finale n'est pas encore calculée.")
        try:
            pdf = bulletin_pdf(e, profil, d, moyenne, acquis, total_cr, annee, numero, session, univ, date_delib)
            st.download_button("Télécharger mon bulletin", pdf,
                               f"bulletin_{_cle_photo(e['matricule'])}_{_cle_photo(annee)}_{_cle_photo(session or 'actuel')}.pdf",
                               "application/pdf", key=f"dl_bulletin_{cle}")
        except ImportError:
            st.info("Bulletin PDF indisponible : installez reportlab "
                    "(py -m pip install reportlab).")


# ---------- Espace étudiant ----------
PAGES_ETUDIANT = [("tableau", "Tableau de bord"), ("cours", "Mes cours"), ("notes", "Mes notes"), ("deliberations", "Mes délibérations"),
                  ("paiements", "Mes paiements"), ("profil", "Mon profil")]


def page_connexion():
    barre_haut()
    # L'accès propriétaire n'est visible que via l'adresse secrète (?admin=CLE) : jamais pour les étudiants.
    cle = param_url("admin")
    acces_proprietaire = bool(cle) and hmac.compare_digest(cle.encode(), lire_cle_admin().encode())
    if acces_proprietaire:
        entete_page("Propriétaire", "Accès administrateur",
                    "Cet espace est réservé au propriétaire de la plateforme.")
        with conteneur("contenu"):
            if lire_cle_admin() == CLE_ADMIN_PAR_DEFAUT:
                st.warning("La clé d'accès par défaut est encore utilisée. "
                           "Définissez le secret CLE_ADMIN.")
            with st.form("f_admin"):
                mdp = st.text_input("Mot de passe administrateur", type="password")
                ok_admin = st.form_submit_button("Accéder", type="primary")
            if ok_admin:
                if hmac.compare_digest(mdp.encode(), lire_mot_de_passe().encode()):
                    st.session_state["admin"] = True
                    st.session_state["page"] = "accueil"
                    st.rerun()
                else:
                    st.error("Mot de passe incorrect.")
        return

    entete_page("Accès étudiant", "Connexion",
                "Entrez votre matricule et votre code personnel pour consulter vos résultats.")
    with conteneur("contenu"):
        with st.form("f_login"):
            mat = st.text_input("Matricule").strip()
            code = st.text_input("Code personnel", type="password").strip()
            ok = st.form_submit_button("Se connecter", type="primary")
        if ok:
            et = charger("etudiants")
            ligne = et[(et["matricule"].str.lower() == mat.lower()) & (et["code"] == code)]
            if mat and code and not ligne.empty:
                st.session_state["etu"] = ligne.iloc[0]["matricule"]
                st.session_state["page"] = "tableau"
                st.rerun()
            else:
                st.error("Matricule ou code incorrect.")


def espace_etudiant(mat):
    et, co, no, p = charger("etudiants"), charger("cours"), charger("notes"), params()
    ligne = et[et["matricule"] == mat]
    if ligne.empty:
        deconnecter()
        st.rerun()
        return
    e = ligne.iloc[0]
    en_ordre = est_en_ordre(e)
    d, moyenne, acquis, total_cr = bilan(e["matricule"], e["promotion"], co, no, faculte_de(e))
    photo = chemin_photo(e["matricule"])
    profil = profil_de(e["matricule"])
    fac = faculte_de(e)
    # Examen et total /20 visibles seulement après délibération (et frais en ordre si blocage actif)
    visible = p["deliberation_publiee"] and (en_ordre or not p["bloquer_impaye"])

    univ = infos_universite(e)
    barre_haut(PAGES_ETUDIANT, connecte=True, logo_chemin=univ["logo"])
    page = st.session_state.get("page", "tableau")

    # ----- Tableau de bord
    if page == "tableau":
        entete_page("Accueil", f"Bonjour, {nom_complet(e)}",
                    f"Bienvenue sur la plateforme {NOM} · {e['promotion']} · {fac} · {univ['nom']}")
        with conteneur("contenu"):
            st.markdown(tuiles_html([
                ("Matricule", e["matricule"]), ("Université", univ["nom"]), ("Promotion", e["promotion"]),
                ("Année académique", p["annee"]), ("Nombre de cours", str(len(d))),
                ("Crédits du programme", f"{total_cr:g}"),
                ("Frais", "En ordre" if en_ordre else "Pas en ordre"),
                ("Délibération", "Publiée" if p["deliberation_publiee"] else "Pas encore publiée"),
                ("Résultats visibles", "Oui" if visible else "Pas encore"),
            ]), unsafe_allow_html=True)
            champs = ["sexe", "date_naissance", "lieu_naissance", "nationalite", "adresse"]
            if not all(profil[c] for c in champs) or not os.path.exists(photo):
                st.warning("Complétez votre profil et ajoutez votre photo (menu, Mon profil) "
                           "pour une carte d'étudiant complète.")

    # ----- Mes cours
    elif page == "cours":
        entete_page("Académique", f"Mes cours ({len(d)})",
                    f"{e['promotion']} · {total_cr:g} crédits · {d['heures'].sum():g} heures")
        with conteneur("contenu"):
            if d.empty:
                st.markdown('<div class="vide">Aucun cours enregistré pour votre promotion.</div>',
                            unsafe_allow_html=True)
            else:
                lignes = [[esc(r["intitule"]), f"{r['credits']:g}", f"{r['heures']:g}h"]
                          for _, r in d.iterrows()]
                st.markdown(tableau_html(["Cours", "Crédits", "Heures"], lignes,
                                         ["Total", f"{total_cr:g}", f"{d['heures'].sum():g}h"]),
                            unsafe_allow_html=True)

    # ----- Mes notes (avec résultats de délibération)
    elif page == "notes":
        entete_page("Académique", "Mes notes", f"{e['promotion']} · {fac}")
        with conteneur("contenu"):
            if d.empty:
                st.markdown('<div class="vide">Aucun cours enregistré.</div>', unsafe_allow_html=True)
            elif not visible:
                lignes = [[esc(r["intitule"]), f"{r['credits']:g}", num(r["moyenne"])]
                          for _, r in d.iterrows()]
                st.markdown(tableau_html(["Cours", "Crédits", "Moyenne /10"], lignes),
                            unsafe_allow_html=True)
                if not p["deliberation_publiee"]:
                    st.info("Délibération pas encore faite : seule votre moyenne /10 "
                            "(interrogations, TP, TD...) est visible. La cote d'examen et le "
                            "total /20 apparaîtront après la délibération.")
                else:
                    st.warning("Cote d'examen et total bloqués : vous n'êtes pas sur la liste des étudiants en ordre.")
            else:
                afficher_deliberation(e, profil, d, moyenne, acquis, total_cr, p["annee"],
                                      f"{ligne.index[0] + 1:03d}", "actuel", "", univ)

    # ----- Mes délibérations (historique enregistré)
    elif page == "deliberations":
        entete_page("Académique", "Mes délibérations",
                    "Chaque délibération reste enregistrée ici : première session, rattrapage, deuxième session, etc.")
        with conteneur("contenu"):
            arch = charger("deliberations")
            arch = arch[(arch["matricule"] == e["matricule"]) & (arch["publie"] == "1")]
            if arch.empty:
                st.markdown('<div class="vide">Aucune délibération enregistrée pour le moment.</div>',
                            unsafe_allow_html=True)
            else:
                arch = arch.sort_values("date", ascending=False)
                bloque = (not en_ordre) and p["bloquer_impaye"]
                for i, (_, r) in enumerate(arch.iterrows()):
                    titre = f"{r['annee']} · {r['session']} · {r['promotion']} · délibéré le {date_fr(r['date'], False)}"
                    with st.expander(titre, expanded=(i == 0)):
                        st.markdown(f"**Date de la délibération : {date_fr(r['date'])}**")
                        if bloque:
                            st.warning("Cette délibération est bien enregistrée, mais les cotes sont "
                                       "bloquées : régularisez d'abord vos frais.")
                            continue
                        da, moy_a, acq_a, tot_a = bilan_depuis_archive(r)
                        afficher_deliberation(e, profil, da, moy_a, acq_a, tot_a, r["annee"],
                                              r["numero"], f"arch{i}", r["session"], univ, r["date"])

    # ----- Mes paiements
    elif page == "paiements":
        entete_page("Finances", "Mes paiements", f"{e['promotion']}. La situation est mise à jour "
                    "par la comptabilité de l'université.")
        with conteneur("contenu"):
            st.markdown(tuiles_html([("Situation", "En ordre" if en_ordre else "Pas en ordre")]),
                        unsafe_allow_html=True)
            if en_ordre:
                st.success("Vous êtes sur la liste des étudiants en ordre : vous avez accès à la délibération.")
            else:
                st.warning("Vous n'êtes pas encore sur la liste des étudiants en ordre. "
                           "Passez régulariser à la comptabilité.")

    # ----- Mon profil
    else:
        entete_page("Compte", "Mon profil", f"{nom_complet(e)} · {e['matricule']}")
        with conteneur("contenu"):
            champs = ["sexe", "date_naissance", "lieu_naissance", "nationalite", "adresse"]
            profil_ok = all(profil[c] for c in champs) and os.path.exists(photo)
            with st.expander("Modifier mon profil et ma photo", expanded=not profil_ok):
                with st.form("f_profil"):
                    options = ["", "Masculin", "Féminin"]
                    sexe = st.selectbox("Sexe", options,
                                        index=options.index(profil["sexe"]) if profil["sexe"] in options else 0)
                    dn = st.text_input("Date de naissance (JJ/MM/AAAA)", profil["date_naissance"])
                    ln = st.text_input("Lieu de naissance", profil["lieu_naissance"])
                    nat = st.text_input("Nationalité", profil["nationalite"])
                    adr = st.text_area("Adresse (où j'habite)", profil["adresse"])
                    tel = st.text_input("Téléphone", profil["telephone"])
                    photo_up = st.file_uploader("Ma photo (JPG ou PNG)", type=["jpg", "jpeg", "png"])
                    envoye = st.form_submit_button("Enregistrer mon profil", type="primary")
                if envoye:
                    nouveau = pd.DataFrame([[e["matricule"], sexe, dn.strip(), ln.strip(),
                                             nat.strip(), adr.strip(), tel.strip()]],
                                           columns=FICHIERS["profils"])
                    sauver("profils", fusionner(charger("profils"), nouveau, CLES["profils"]))
                    if photo_up is not None:
                        try:
                            sauver_photo(e["matricule"], photo_up)
                        except Exception:
                            st.error("Photo illisible. Essayez une autre image (JPG ou PNG).")
                    st.success("Profil enregistré. Rechargez la page pour voir la carte à jour.")

            profil = profil_de(e["matricule"])
            st.markdown('<div class="sect">Carte d\'étudiant</div>', unsafe_allow_html=True)
            st.markdown(carte_html(e, profil, p["annee"], photo, univ), unsafe_allow_html=True)
            st.markdown('<div class="sect">Verso de la carte</div>', unsafe_allow_html=True)
            st.markdown(carte_verso_html(e, profil, p["annee"], univ), unsafe_allow_html=True)
            try:
                st.download_button("Télécharger ma carte recto-verso (PDF)",
                                   carte_pdf(e, profil, p["annee"], photo, univ),
                                   f"carte_{e['matricule']}.pdf", "application/pdf", key="dl_carte")
            except ImportError:
                st.info("PDF indisponible : installez reportlab (py -m pip install reportlab).")


def supprimer_photo(mat):
    chemin = os.path.join(DOSSIER_PHOTOS, _cle_photo(mat) + ".jpg")
    if os.path.exists(chemin):
        os.remove(chemin)
    if base_en_ligne():
        db_supprimer("photo_" + _cle_photo(mat))


def supprimer_etudiants(mats):
    """Supprime les étudiants et tout ce qui leur appartient : notes, profil, photo, délibérations."""
    cibles = {str(m).lower() for m in mats}
    for nom in ("etudiants", "notes", "profils", "deliberations"):
        df = charger(nom)
        sauver(nom, df[~df["matricule"].str.lower().isin(cibles)])
    for m in mats:
        supprimer_photo(m)


# ---------- Espace administrateur ----------
# L'accueil n'est plus dans le menu : on y revient avec le bouton « Accueil » affiché sur chaque page.
PAGES_ADMIN = [("parametres", "Sauvegarde"), ("universites", "Universités"), ("facultes", "Facultés"),
               ("etudiants", "Étudiants"), ("cours", "Cours"), ("notes", "Notes"),
               ("paiements", "Paiements"), ("deliberation", "Délibération")]
# ---------- Documents à remettre aux étudiants (Word et PDF) ----------
TYPE_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def tableau_docx(titre, sous_titre, entetes, lignes):
    """Document Word (.docx) avec un titre et un tableau, sans bibliothèque externe."""
    import zipfile
    from xml.sax.saxutils import escape as xe

    def cellule(texte, gras=False, fond=None):
        pr = f'<w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{fond}"/></w:tcPr>' if fond else ""
        rpr = "<w:rPr><w:b/>" + ('<w:color w:val="FFFFFF"/>' if fond else "") + "</w:rPr>" if gras else ""
        return f'<w:tc>{pr}<w:p><w:r>{rpr}<w:t xml:space="preserve">{xe(str(texte))}</w:t></w:r></w:p></w:tc>'

    def ligne(vals, entete=False):
        return "<w:tr>" + "".join(cellule(v, entete, "1F3A5F" if entete else None) for v in vals) + "</w:tr>"

    bord = "".join(f'<w:{c} w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
                   for c in ("top", "left", "bottom", "right", "insideH", "insideV"))
    corps = (f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr>'
             f'<w:t>{xe(titre)}</w:t></w:r></w:p>'
             f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>{xe(sous_titre)}</w:t></w:r></w:p>'
             f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{bord}</w:tblBorders></w:tblPr>'
             + ligne(entetes, True) + "".join(ligne(l) for l in lignes) + "</w:tbl>"
             f'<w:p><w:r><w:rPr><w:i/><w:sz w:val="16"/></w:rPr><w:t>Document généré par {xe(NOM)} '
             f'le {date.today().strftime("%d/%m/%Y")}</w:t></w:r></w:p>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{corps}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1000" w:right="900" w:bottom="1000" w:left="900" w:header="500" w:footer="500" w:gutter="0"/>'
                '</w:sectPr></w:body></w:document>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def tableau_pdf(titre, sous_titre, entetes, lignes):
    """Document PDF avec un titre et un tableau."""
    from xml.sax.saxutils import escape as xe
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st_ = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
                            title=titre)
    donnees = [list(entetes)] + [[Paragraph(xe(str(v)), st_["Normal"]) for v in l] for l in lignes]
    t = Table(donnees, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#e3f4f1")]),
                           ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9fb3c8")),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    titre_p = Paragraph(f"<para alignment='center'><b><font size=16>{xe(titre)}</font></b></para>", st_["Normal"])
    sous_p = Paragraph(f"<para alignment='center'>{xe(sous_titre)}</para>", st_["Normal"])
    doc.build([titre_p, sous_p, Spacer(1, 12), t, Spacer(1, 14),
               Paragraph(f"<i>Document généré par {xe(NOM)} le {date.today().strftime('%d/%m/%Y')}</i>",
                         st_["Normal"])])
    return buf.getvalue()


def boutons_documents(titre, sous_titre, tab, nom_fichier, cle):
    """Boutons de téléchargement Word et PDF pour un tableau (DataFrame)."""
    entetes, lignes = list(tab.columns), tab.astype(str).values.tolist()
    c1, c2 = st.columns(2)
    c1.download_button("Télécharger en Word", tableau_docx(titre, sous_titre, entetes, lignes),
                       f"{nom_fichier}.docx", TYPE_DOCX, key=f"{cle}_word")
    try:
        c2.download_button("Télécharger en PDF", tableau_pdf(titre, sous_titre, entetes, lignes),
                           f"{nom_fichier}.pdf", "application/pdf", key=f"{cle}_pdf")
    except ImportError:
        c2.info("PDF indisponible : installez reportlab.")


def afficher_nouveaux_ids():
    """Identifiants (matricule + mot de passe) des étudiants ajoutés pendant cette session."""
    nouveaux = st.session_state.get("nouveaux_ids", [])
    if not nouveaux:
        return
    st.success("Étudiants ajoutés. Remettez-leur leur matricule et leur mot de passe "
               "(ils ne seront plus affichés après la fermeture de cette page).")
    tab = pd.DataFrame(nouveaux)
    st.dataframe(tab, hide_index=True)
    boutons_documents("Identifiants des étudiants", "Matricule et mot de passe à remettre à chaque étudiant",
                      tab, "identifiants_etudiants", "dl_ids")
    c1, c2 = st.columns(2)
    c1.download_button("Télécharger les identifiants (CSV)", tab.to_csv(index=False),
                       "identifiants_nouveaux_etudiants.csv", "text/csv", key="dl_ids_csv")
    if c2.button("Vider cette liste", key="vider_ids"):
        st.session_state["nouveaux_ids"] = []
        st.rerun()


# ---------- Import Excel (étudiants, cours, notes des professeurs) ----------
SYNONYMES = {
    "matricule": ["matricule", "matr", "mat", "n_matricule", "numero_matricule"],
    "nom": ["nom", "noms", "nom_complet", "nom_et_postnom", "etudiant", "nom_etudiant",
            "nom_de_l_etudiant", "noms_de_l_etudiant"],
    "postnom": ["postnom", "post_nom", "post_nom_s", "postnoms"],
    "prenom": ["prenom", "prenoms", "pre_nom"],
    "sexe": ["sexe", "genre", "gender"],
    "promotion": ["promotion", "promos", "promo", "niveau"],
    "faculte": ["faculte", "faculty", "fac"],
    "frais_dus": ["frais_dus", "frais", "frais_academiques", "montant_du"],
    "intitule": ["intitule", "intitule_du_cours", "intitule_de_cours", "cours", "matiere", "matieres",
                 "nom_du_cours", "libelle", "designation", "unite_d_enseignement", "ue",
                 "element_constitutif", "ec", "cours_matieres", "intitule_ue", "intitules"],
    "heures": ["heures", "heure", "volume_horaire", "nombre_d_heures", "nbre_d_heures", "vh", "vol_horaire"],
    "credits": ["credits", "credit", "nombre_de_credits", "nbre_de_credits", "nombre_credits", "cr"],
    "cote": ["cote", "cotes", "note", "notes", "moyenne", "examen", "points", "resultat"],
}
for _k, _v in {
    "matricule": ["matricules", "no_matricule", "n_matr", "matr_"],
    "nom": ["nom_postnom_prenom", "nom_post_nom_prenom", "noms_complets", "nom_et_prenom", "noms_et_prenoms",
            "nom_prenom", "identite", "etudiants", "candidat", "candidats", "noms_des_etudiants"],
    "heures": ["h", "vol_h", "volume_h", "heures_totales", "total_heures"],
    "credits": ["ects", "nbre_credits", "credits_ects"],
}.items():
    SYNONYMES[_k] += _v
TYPE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _norm(txt):
    t = unicodedata.normalize("NFD", str(txt)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def lire_tableur(f):
    """Lit un fichier Excel (.xlsx) ou CSV envoyé : tout en texte, lignes vides retirées."""
    nom = str(getattr(f, "name", "")).lower()
    if nom.endswith(".csv"):
        try:
            df = pd.read_csv(f, dtype=str, sep=None, engine="python")
        except Exception:
            f.seek(0)
            df = pd.read_csv(f, dtype=str, sep=";")
    else:
        df = pd.read_excel(f, dtype=str)
    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    if df.empty:
        return df
    return df[df.apply(lambda r: "".join(r.values) != "", axis=1)].reset_index(drop=True)


def renommer_colonnes(df, cles):
    """Reconnaît les en-têtes même écrits autrement (« Genre » -> sexe, « Matière » -> intitule...)."""
    carte, pris = {}, set()
    for c in df.columns:
        n = _norm(c)
        for k in cles:
            if k not in pris and n in SYNONYMES[k]:
                carte[c] = k
                pris.add(k)
                break
    return df.rename(columns=carte)


def modele_excel(colonnes, exemples):
    """Modèle Excel à télécharger ; None si la bibliothèque openpyxl n'est pas installée."""
    try:
        tampon = io.BytesIO()
        pd.DataFrame(exemples, columns=colonnes).to_excel(tampon, index=False)
        return tampon.getvalue()
    except ImportError:
        return None


def bouton_modele(label, donnees, nom, mime, key):
    if donnees is None:
        st.warning("Les fichiers Excel demandent la bibliothèque openpyxl. Dans le terminal, tapez : "
                   "py -m pip install openpyxl, puis relancez l'application.")
    else:
        st.download_button(label, donnees, nom, mime, key=key)


def sexe_normalise(v):
    n = _norm(v)
    if n in ("m", "h", "masculin", "homme", "garcon", "male"):
        return "Masculin"
    if n in ("f", "feminin", "femme", "fille", "female"):
        return "Féminin"
    return ""


def nombre_texte(v):
    """'7,5' -> 7.5 ; None si illisible."""
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def resoudre_faculte(fac_df, valeur, defaut):
    """Nom officiel de la faculté (par son nom ou son code) ; None si inconnue ; défaut si vide."""
    v = str(valeur).strip()
    if not v:
        return defaut
    cible = faculte_de({"faculte": v}).lower()
    for _, r in fac_df.iterrows():
        if (faculte_de({"faculte": r["faculte"]}).lower() == cible
                or str(r["code"]).strip().lower() == v.lower()):
            return r["faculte"]
    return None


# ---------- Lecture libre des fichiers : Excel, CSV, Word, PDF, texte (aucun modèle imposé) ----------
TYPES_FICHIERS = ["xlsx", "xls", "csv", "docx", "pdf", "txt"]
RE_MATRICULE = re.compile(r"\b\d{1,4}\s*/\s*[A-Za-z]{1,6}\s*/\s*\d{2,4}\s*/\s*\d{1,5}\b")
RE_CODE = re.compile(r"^[A-Za-z0-9/._\-]{4,25}$")
RE_CODE_COURS = re.compile(r"^[A-Za-z]{2,6}\s?-?\d{2,4}[A-Za-z]?$")
# Mots qui trahissent un titre ou un en-tête (et non un étudiant) quand la ligne n'a pas de matricule
MOTS_ENTETE = {"liste", "etudiants", "etudiant", "promotion", "faculte", "universite", "annee", "academique",
               "matricule", "matricules", "postnom", "prenom", "prenoms", "genre", "sexe", "signature", "total",
               "session", "cours", "credits", "heures", "cote", "cotes", "moyenne", "examen", "paiement",
               "paiements", "frais", "ordre", "republique", "democratique", "ministere", "enseignement",
               "superieur", "nom", "noms", "numero", "observation", "observations", "deliberation",
               "resultats", "page", "telephone", "adresse", "date"}
MARQUES_ABSENT = {"ABS", "ABSENT", "NC", "ND", "-", "–", "X"}   # cote non disponible (absence...)
MOTS_ENTETE_COURS = {"credits", "credit", "heures", "intitule", "matiere", "matieres", "volume", "horaire",
                     "promotion", "faculte", "universite", "annee", "academique", "liste", "cours", "ue", "ec",
                     "code", "vh", "cr", "enseignant", "total"}


def _texte(c):
    return re.sub(r"\s+", " ", str(c if c is not None else "").replace("\xa0", " ")).strip()


def _est_nombre(v):
    return nombre_texte(v) is not None


def _detacher(p):
    """'1. MUTOMBO Jean M 7,5' -> ['1.', 'MUTOMBO Jean', 'M', '7,5'] (rang au début, sexe et nombres à la fin)."""
    toks, debut, fin = p.split(" "), [], []
    while len(toks) > 1 and re.fullmatch(r"\d{1,3}[.)\-]?", toks[0]):
        debut.append(toks.pop(0))
    while len(toks) > 1 and (_est_nombre(toks[-1]) or toks[-1] in ("M", "F")
                             or toks[-1].upper() in MARQUES_ABSENT):
        fin.insert(0, toks.pop())
    reste = " ".join(toks)
    return debut + ([reste] if reste else []) + fin


def decouper_ligne(ligne):
    """Ligne de texte (Word ou PDF sans tableau) -> cellules : rang, matricule, nom, nombres..."""
    parts = [x.strip() for x in re.split(r"\t|;|\||\s{2,}", str(ligne).replace("\xa0", " ")) if x.strip()]
    cellules = []
    for p in parts:
        m = RE_MATRICULE.search(p)
        if m:
            cellules += _detacher(p[:m.start()].strip()) if p[:m.start()].strip() else []
            cellules.append(re.sub(r"\s+", "", m.group(0)))
            apres = p[m.end():].strip()
            cellules += _detacher(apres) if apres else []
        else:
            cellules += _detacher(p)
    return cellules


def _lignes_docx(f):
    import zipfile
    from xml.etree import ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    f.seek(0)
    racine = ET.fromstring(zipfile.ZipFile(f).read("word/document.xml"))

    def texte_para(p):
        out = []
        for el in p.iter():
            if el.tag == ns + "t":
                out.append(el.text or "")
            elif el.tag == ns + "tab":
                out.append("\t")
            elif el.tag == ns + "br":
                out.append(" ")
        return "".join(out)

    tableaux, paragraphes = [], []
    for el in racine.find(ns + "body"):
        if el.tag == ns + "tbl":
            for tr in el.iter(ns + "tr"):
                tableaux.append([_texte(" ".join(texte_para(p) for p in tc.findall(ns + "p")))
                                 for tc in tr.findall(ns + "tc")])
        elif el.tag == ns + "p":
            paragraphes.append(texte_para(el))
    if tableaux:
        return tableaux
    return [c for c in (decouper_ligne(l) for l in paragraphes) if c]


def _lignes_pdf(f):
    f.seek(0)
    tableaux, textes = [], []
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber:
        with pdfplumber.open(f) as pdf:
            for page in pdf.pages:
                for t in page.extract_tables():
                    tableaux += [[_texte(c) for c in ligne] for ligne in t]
                textes.append(page.extract_text() or "")
    else:
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("Pour lire les PDF, installez pdfplumber : py -m pip install pdfplumber")
        textes = [page.extract_text() or "" for page in PdfReader(f).pages]
    tableaux = [l for l in tableaux if any(l)]
    if tableaux:
        return tableaux
    lignes = [c for c in (decouper_ligne(l) for t in textes for l in t.splitlines()) if c]
    if not lignes:
        raise ValueError("aucun texte lisible dans ce PDF (PDF scanné ou image ?). "
                         "Envoyez plutôt un PDF numérique, un Word ou un Excel.")
    return lignes


def _texte_brut(f):
    f.seek(0)
    brut = f.read()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return brut.decode(enc)
        except UnicodeDecodeError:
            continue
    return brut.decode("utf-8", errors="replace")


def lire_document(f, cle="doc", toutes_feuilles=False):
    """Lit un fichier Excel, CSV, Word, PDF ou texte, sans modèle. Retourne une liste de lignes (listes de cellules)."""
    import csv
    nom = str(getattr(f, "name", "")).lower()
    f.seek(0)
    if nom.endswith((".xlsx", ".xls")):
        feuilles = pd.read_excel(f, dtype=str, header=None, sheet_name=None)
        feuilles = {k: v for k, v in feuilles.items() if not v.dropna(how="all").empty}
        if not feuilles:
            return []
        if toutes_feuilles:   # le nom de chaque feuille sert de titre de section (ex. une feuille par promotion)
            lignes = []
            for nom_f, feuille in feuilles.items():
                lignes.append([str(nom_f)])
                lignes += feuille.fillna("").values.tolist()
        else:
            choix = list(feuilles)[0]
            if len(feuilles) > 1:
                choix = st.selectbox("Feuille du fichier Excel à lire", list(feuilles), key=f"feuille_{cle}")
            lignes = feuilles[choix].fillna("").values.tolist()
    elif nom.endswith(".docx"):
        lignes = _lignes_docx(f)
    elif nom.endswith(".pdf"):
        lignes = _lignes_pdf(f)
    elif nom.endswith(".csv"):
        texte = _texte_brut(f)
        try:
            delim = csv.Sniffer().sniff(texte[:4000], delimiters=";,\t|").delimiter
        except csv.Error:
            delim = ";" if texte.count(";") >= texte.count(",") else ","
        lignes = list(csv.reader(texte.splitlines(), delimiter=delim))
    else:
        lignes = [decouper_ligne(l) for l in _texte_brut(f).splitlines()]
    lignes = [[_texte(c) for c in l] for l in lignes]
    return [l for l in lignes if any(l)]


def preparer_grille(lignes, cles):
    """Lignes -> (tableau, carte, en-tête trouvé). Si une ligne d'en-tête reconnaissable existe (dans les 15
    premières lignes), les colonnes portent ses noms et la carte dit quelle colonne est quoi.
    Sinon les colonnes s'appellent « Colonne 1, 2... » et la carte est vide (lecture par le contenu)."""
    n = max(len(l) for l in lignes)
    lignes = [l + [""] * (n - len(l)) for l in lignes]
    for i in range(min(15, len(lignes))):
        carte_i = {}
        for j, c in enumerate(lignes[i]):
            k = next((k for k in cles if k not in carte_i.values() and _norm(c) in SYNONYMES[k]), None)
            if k:
                carte_i[j] = k
        if any(k in carte_i.values() for k in ("matricule", "nom", "intitule")):
            noms, vus = [], {}
            for j, c in enumerate(lignes[i]):
                base = c or f"Colonne {j + 1}"
                vus[base] = vus.get(base, 0) + 1
                noms.append(base if vus[base] == 1 else f"{base} ({vus[base]})")
            df = pd.DataFrame(lignes[i + 1:], columns=noms)
            return df, {k: noms[j] for j, k in carte_i.items()}, True
    return pd.DataFrame(lignes, columns=[f"Colonne {j + 1}" for j in range(n)]), {}, False


def charger_grille(f, cle, cles, toutes_feuilles=False):
    """Lit le fichier envoyé ; affiche l'erreur et retourne None si illisible."""
    try:
        lignes = lire_document(f, cle, toutes_feuilles)
    except ImportError as err:
        st.error(str(err))
        return None
    except Exception as err:
        st.error(f"Fichier illisible : {err}")
        return None
    if not lignes:
        st.error("Ce fichier est vide ou ne contient aucun texte lisible.")
        return None
    return preparer_grille(lignes, cles)[:2]


def colonne_rang(df):
    """Colonne de numérotation (1, 2, 3...) à ignorer."""
    for c in df.columns:
        v = [str(x).strip().rstrip(".)") for x in df[c] if str(x).strip()]
        if len(v) >= 3 and all(x.isdigit() for x in v):
            n = [int(x) for x in v]
            if n[0] in (0, 1) and sum(b == a + 1 for a, b in zip(n, n[1:])) >= 0.8 * (len(n) - 1):
                return c
    return None


def analyser_ligne(cells):
    """Une ligne sans en-tête -> (matricule, nom complet, sexe) ou None si ce n'est pas un étudiant."""
    texte_ligne = " ".join(cells)
    mat = ""
    for c in cells:
        m = RE_MATRICULE.search(c)
        if m:
            mat = re.sub(r"\s+", "", m.group(0))
            break
    if not mat and set(_norm(texte_ligne).split("_")) & MOTS_ENTETE:
        return None
    if not mat:   # code collé (lettres + chiffres, sans espace) : matricule d'un autre format
        for c in cells:
            if RE_CODE.match(c) and re.search(r"\d", c) and re.search(r"[A-Za-z]", c) and not _est_nombre(c):
                mat = c
                break
    sexe, noms = "", []
    for c in cells:
        if RE_MATRICULE.search(c) or c == mat:
            continue
        if c in ("M", "F", "m", "f") or (len(_norm(c).split("_")) == 1 and sexe_normalise(c)):
            sexe = sexe_normalise(c)
            continue
        if _est_nombre(c) or c.upper() in MARQUES_ABSENT or not re.search(r"[^\W\d_]{2,}", c):
            continue
        noms.append(c)
    nom = " ".join(noms)
    if not mat and (not 2 <= len(nom.split()) <= 6 or re.search(r"\d", nom)):
        return None
    if not (mat or nom):
        return None
    return mat, nom, sexe


def trouver_promotion(texte, promos):
    """Promotion connue contenue dans un texte (« PROMOTION : Bac 2 », « bac 2 »...), sinon None."""
    n = "_" + _norm(texte) + "_"
    for p in sorted(promos, key=lambda x: -len(str(x))):
        k = _norm(p)
        if k and f"_{k}_" in n:
            return p
    return None


def eclater_nom(nom, pn, pr):
    """Un seul nom écrit en entier : 1er mot = nom, 2e = post-nom, le reste (3e...) = prénom."""
    if nom and not pn and not pr:
        t = nom.split()
        if len(t) >= 3:
            return t[0], t[1], " ".join(t[2:])
        if len(t) == 2:
            return t[0], t[1], ""
    return nom, pn, pr


def personnes_depuis_grille(df, carte, promos=()):
    """Étudiants reconnus dans un tableau : colonnes matricule, nom, postnom, prenom, sexe, _ligne, promotion.
    Seuls le nom et le matricule comptent ; le reste du fichier est ignoré. Un nom écrit en entier est éclaté
    (nom, post-nom, prénom). La promotion vient de la colonne « promotion » ou du dernier titre de section."""
    sortie, courante = [], ""
    for i, r in df.iterrows():
        def val(k):
            return str(r[carte[k]]).strip() if k in carte else ""
        mat, nom, pn, pr, sx = val("matricule"), val("nom"), val("postnom"), val("prenom"), val("sexe")
        texte_ligne = " ".join(str(x).strip() for x in r.values if str(x).strip())
        if carte.get("nom") or carte.get("matricule"):
            if _norm(nom or mat).startswith("total"):
                continue
            if not nom and not pn and not pr:   # en-tête reconnu mais pas de colonne de noms : on lit le contenu
                trouve = analyser_ligne([str(x).strip() for x in r.values if str(x).strip()])
                if trouve:
                    mat, nom, sx = mat or trouve[0], trouve[1], sx or trouve[2]
        else:
            trouve = analyser_ligne([str(x).strip() for x in r.values if str(x).strip()])
            if not trouve:
                titre = trouver_promotion(texte_ligne, promos) if promos else None
                if titre:
                    courante = titre
                continue
            mat, nom, sx = trouve
        if not (nom or pn or pr):
            titre = trouver_promotion(texte_ligne, promos) if promos else None
            if titre:
                courante = titre
            continue
        promo_col = val("promotion")
        promo = (trouver_promotion(promo_col, promos) or promo_col) if promo_col else courante
        nom, pn, pr = eclater_nom(nom, pn, pr)
        sortie.append([mat, nom, pn, pr, sexe_normalise(sx), i, promo])
    return pd.DataFrame(sortie, columns=["matricule", "nom", "postnom", "prenom", "sexe", "_ligne", "promotion"])


def index_etudiants(grp):
    idx = {"mat": {}, "nom": {}, "liste": []}
    for i, g in zip(grp.index, [r for _, r in grp.iterrows()]):
        idx["mat"][str(g["matricule"]).lower()] = i
        k = _norm(nom_complet(g))
        idx["nom"].setdefault(k, []).append(i)
        idx["liste"].append((i, frozenset(k.split("_")) - {""}))
    return idx


def trouver_etudiant(idx, mat, nom):
    """Retrouve un étudiant par matricule, sinon par nom (même dans un autre ordre ou incomplet).
    Retourne (index ou None, statut, reconnu par)."""
    mat = str(mat).strip()
    if mat and mat.lower() in idx["mat"]:
        return idx["mat"][mat.lower()], "OK", "matricule"
    k = _norm(nom)
    if k:
        exact = idx["nom"].get(k, [])
        if len(exact) == 1:
            return exact[0], "OK", "nom"
        if len(exact) > 1:
            return None, "Homonymes (même nom complet) : le matricule les distingue", ""
        t = frozenset(k.split("_")) - {""}
        if len(t) >= 2:
            # nom incomplet ou dans un autre ordre : seulement si l'un des noms contient l'autre.
            # Deux noms qui ont chacun un mot en plus (ex. prénoms Jeremie / Papy) = deux personnes différentes.
            scores = [(len(t & ti) / max(len(t), len(ti)), i) for i, ti in idx["liste"]
                      if len(t & ti) >= 2 and (t <= ti or ti <= t)]
            if scores:
                meilleur = max(sc for sc, _ in scores)
                tops = [i for sc, i in scores if sc == meilleur]
                if len(tops) == 1:
                    return tops[0], "OK", "nom (ordre ou nom incomplet)"
                return None, "Nom incomplet, correspond à plusieurs étudiants : précisez le prénom ou le matricule", ""
    if mat and not k:
        return None, "Matricule absent de cette promotion", ""
    return None, "Nom absent de cette promotion", ""


def analyser_liste_ordre(pers, grp, par_promotion=False):
    """Étudiants en ordre : seuls les noms (ou matricules) du fichier comptent, peu importe sa forme.
    Avec par_promotion (liste de toute la faculté), chaque étudiant est cherché dans sa promotion,
    lue dans l'en-tête du fichier ; sans promotion indiquée, dans toute la faculté."""
    if pers.empty:
        return None, "Aucun nom d'étudiant reconnu dans ce fichier."
    idx_tous = index_etudiants(grp)
    idx_promo = {p: index_etudiants(grp[grp["promotion"] == p]) for p in grp["promotion"].unique()} \
        if par_promotion else {}
    sortie = []
    for _, r in pers.iterrows():
        nom_lu, promo_lue = nom_complet(r), str(r.get("promotion", "")).strip()
        if par_promotion and promo_lue and promo_lue not in idx_promo:
            i, statut, par = None, f"Promotion inconnue : {promo_lue}", ""
        else:
            idx = idx_promo.get(promo_lue, idx_tous) if par_promotion else idx_tous
            i, statut, par = trouver_etudiant(idx, r["matricule"], nom_lu)
            if par_promotion and not promo_lue and i is not None:
                par += " · promotion non précisée"
        ligne = {"Dans le fichier": nom_lu or r["matricule"],
                 "Matricule": grp.at[i, "matricule"] if i is not None else r["matricule"],
                 "Nom (plateforme)": nom_complet(grp.loc[i]) if i is not None else "",
                 "Promotion": (grp.at[i, "promotion"] if par_promotion else "") if i is not None else promo_lue,
                 "Reconnu par": par, "Statut": statut, "_i": i}
        sortie.append(ligne)
    res = pd.DataFrame(sortie, columns=["Dans le fichier", "Matricule", "Nom (plateforme)", "Promotion",
                                        "Reconnu par", "Statut", "_i"])
    return (res if par_promotion else res.drop(columns=["Promotion"])), ""


def colonnes_de_cotes(df, carte, pers):
    """Colonnes du fichier qui contiennent des cotes (nombres), sans la numérotation ni les identités."""
    exclues = {carte.get(k) for k in ("matricule", "nom", "postnom", "prenom", "sexe")}
    rang = colonne_rang(df.loc[pers["_ligne"]])   # numérotation : seulement sur les lignes d'étudiants
    candidates = []
    for c in df.columns:
        if c in exclues or c == rang:
            continue
        vals = [str(df.at[i, c]).strip() for i in pers["_ligne"]]
        vals = [v for v in vals if v]
        if vals and sum(_est_nombre(v) for v in vals) / len(vals) >= 0.6:
            candidates.append(c)
    return candidates


def analyser_notes(pers, df, colonne, et, cours_choisi):
    """Cotes d'UNE colonne du fichier pour UN cours : chaque étudiant est retrouvé par matricule ou par nom."""
    if pers.empty:
        return None, "Aucun étudiant reconnu dans ce fichier."
    idx = index_etudiants(et)
    sortie = []
    for _, r in pers.iterrows():
        valeur = str(df.at[r["_ligne"], colonne]).strip()
        if not valeur or valeur.upper() in MARQUES_ABSENT:
            continue   # cote vide ou absence : rien à enregistrer
        nom_lu = nom_complet(r)
        i, statut, par = trouver_etudiant(idx, r["matricule"], nom_lu)
        v = nombre_texte(valeur)
        mat, nom_pf = "", ""
        if i is not None:
            mat, nom_pf = et.at[i, "matricule"], nom_complet(et.loc[i])
            if v is None or v < 0 or v > 10:
                statut = "Cote invalide (0 à 10)"
        sortie.append({"Matricule": mat or r["matricule"], "Nom (plateforme)": nom_pf,
                       "Dans le fichier": nom_lu or r["matricule"], "Cours": cours_choisi, "Cote": valeur,
                       "Genre": r["sexe"], "Statut": statut, "_ok": statut == "OK", "_val": v})
    return pd.DataFrame(sortie, columns=["Matricule", "Nom (plateforme)", "Dans le fichier", "Cours", "Cote",
                                          "Genre", "Statut", "_ok", "_val"]), ""


def lire_cours_libres(df, carte, promo, fac, inverser=False):
    """Cours : seulement l'intitulé, les heures et les crédits. Avec en-têtes reconnus, on les suit ;
    sinon on lit le contenu (texte = intitulé, nombres = heures et crédits)."""
    if "intitule" in carte:
        df = df[~df[carte["intitule"]].map(lambda v: _norm(v).startswith("total"))]
        return preparer_cours(df.rename(columns={v: k for k, v in carte.items()}), promo, fac)
    rang = colonne_rang(df)
    lignes = []
    for _, r in df.iterrows():
        cells = [str(r[c]).strip() for c in df.columns if c != rang and str(r[c]).strip()]
        if not cells or _norm(cells[0]).startswith("total"):
            continue
        nombres = [nombre_texte(c) for c in cells if _est_nombre(c)]
        textes = [c for c in cells if not _est_nombre(c) and re.search(r"[^\W\d_]{3,}", c)
                  and not RE_CODE_COURS.match(c)]
        if not textes or not nombres:
            continue
        if set(_norm(textes[0]).split("_")) & MOTS_ENTETE_COURS and len(textes[0].split()) <= 2:
            continue
        if len(nombres) == 1:
            h, cr = (0, nombres[0]) if nombres[0] <= 15 else (nombres[0], 0)
        else:
            h = max(nombres)
            cr = nombres[-1] if nombres[-1] <= 20 and nombres[-1] != h else min(nombres)
        if inverser:
            h, cr = cr, h
        lignes.append([textes[0], promo, f"{h:g}", f"{cr:g}", fac])
    if not lignes:
        return None, ["Aucun cours reconnu : il faut, sur chaque ligne, l'intitulé du cours et au moins un "
                      "nombre (heures ou crédits)."]
    return pd.DataFrame(lignes, columns=FICHIERS["cours"]), []



def preparer_etudiants(brut, fac, promo, existants, annee):
    """Liste des étudiants d'une promotion (en-têtes : matricule (facultatif), nom, postnom, prenom, genre).
    Si la fiche a déjà un matricule, il est conservé tel quel : seul le mot de passe est généré.
    Sans matricule, la plateforme en génère un (code faculté + année + numéro)."""
    new = renommer_colonnes(brut, ["matricule", "nom", "postnom", "prenom", "sexe"])
    if "nom" not in new.columns:
        return pd.DataFrame(columns=FICHIERS["etudiants"]), {}, \
            ["Colonne manquante : nom (attendues : matricule (facultatif), nom, postnom, prénom, genre)."], []
    for c in ("matricule", "postnom", "prenom", "sexe"):
        if c not in new.columns:
            new[c] = ""
    cible = faculte_de({"faculte": fac}).lower()
    deja_mat = {str(m).lower() for m in existants["matricule"]}
    # Homonymes autorisés : deux étudiants peuvent avoir le même nom complet, ils se distinguent par le
    # matricule (et la promotion). Un nom n'est ignoré que s'il est déjà inscrit autant de fois
    # qu'il apparaît dans le fichier (cas d'un fichier envoyé deux fois).
    deja_nom = {}
    for _, r in existants.iterrows():
        if r["promotion"].lower() == promo.lower() and faculte_de(r).lower() == cible:
            k = _norm(nom_complet(r))
            deja_nom[k] = deja_nom.get(k, 0) + 1
    erreurs, ignores, lignes, sexes, rangs = [], [], [], {}, {}
    vus, occurrences, rang = set(), {}, 0
    for i, r in new.iterrows():
        if not r["nom"]:
            continue
        rang += 1   # numéro sur la liste d'inscription : 1er nom = 001, 2e = 002, ...
        complet_nom = nom_complet(r)
        mat = r["matricule"].strip()
        if mat:
            if mat.lower() in deja_mat or mat.lower() in vus:
                ignores.append(f"{complet_nom} : le matricule {mat} existe déjà : ignoré.")
                continue
            vus.add(mat.lower())
        else:
            k = _norm(complet_nom)
            occurrences[k] = occurrences.get(k, 0) + 1
            if occurrences[k] <= deja_nom.get(k, 0):
                ignores.append(f"{complet_nom} est déjà inscrit en {promo} : ignoré.")
                continue
        lignes.append([mat, r["nom"], promo, "", fac, "", r["postnom"], r["prenom"]])
        sexes[len(lignes) - 1] = sexe_normalise(r["sexe"])
        rangs[len(lignes) - 1] = rang
    df = pd.DataFrame(lignes, columns=FICHIERS["etudiants"])
    if df.empty:
        return df, {}, erreurs, ignores
    df, err2 = completer_identifiants(df, existants, annee, rangs, ignores)
    erreurs += err2
    profils = {df.at[i, "matricule"]: sx for i, sx in sexes.items() if sx}
    return df, profils, erreurs, ignores


def _chercher_entete(brut, cles):
    """Si l'en-tête n'est pas sur la 1re ligne (titre au-dessus du tableau), le retrouve dans les 10 premières lignes."""
    for i in range(min(10, len(brut))):
        noms = brut.iloc[i].astype(str).str.strip().tolist()
        t = pd.DataFrame(brut.iloc[i + 1:].values, columns=noms)
        t = renommer_colonnes(t, cles)
        if "intitule" in t.columns:
            t = t[t.apply(lambda r: "".join(r.astype(str)) != "", axis=1)]
            return t.reset_index(drop=True)
    return None


def preparer_cours(brut, promo_defaut="", fac=""):
    """Liste de cours envoyée par la faculté. Seul l'en-tête compte : intitulé, promotion, heures, crédits."""
    cles = ["intitule", "promotion", "heures", "credits"]
    new = renommer_colonnes(brut, cles)
    if "intitule" not in new.columns:
        autre = _chercher_entete(brut, cles)
        if autre is None:
            lues = ", ".join(f"« {c} »" for c in brut.columns)
            return None, ["Colonne manquante : intitulé du cours (attendues : intitulé, promotion, heures, "
                          f"crédits). Colonnes lues dans votre fichier : {lues}. Renommez la colonne des "
                          "cours en « intitulé » et mettez les en-têtes sur la première ligne."]
        new = autre
    if "promotion" not in new.columns:
        if not promo_defaut:
            return None, ["Colonne « promotion » absente du fichier : ajoutez-la ou choisissez "
                          "la promotion ci-dessus."]
        new["promotion"] = promo_defaut
    elif promo_defaut:
        new["promotion"] = promo_defaut   # la promotion choisie fait foi
    for c in ("heures", "credits"):
        if c not in new.columns:
            new[c] = "0"
    erreurs, lignes = [], []
    for i, r in new.iterrows():
        if not (r["intitule"] and r["promotion"]):
            erreurs.append(f"Ligne {i + 2} : intitulé et promotion sont obligatoires.")
            continue
        h = nombre_texte(r["heures"] or "0")
        cr = nombre_texte(r["credits"] or "0")
        if h is None or cr is None:
            erreurs.append(f"Ligne {i + 2} ({r['intitule']}) : heures ou crédits illisibles.")
            continue
        lignes.append([r["intitule"], r["promotion"], f"{h:g}", f"{cr:g}", fac])
    return pd.DataFrame(lignes, columns=FICHIERS["cours"]), erreurs


def noms_proches(a, b):
    ta, tb = set(_norm(a).split("_")) - {""}, set(_norm(b).split("_")) - {""}
    return bool(ta and tb) and len(ta & tb) >= min(2, len(ta), len(tb))


def appliquer_notes(n, lignes, champ):
    """Met à jour seulement la colonne moyenne OU examen ; l'autre cote est conservée."""
    base = {(r["matricule"], r["cours"]): {"moyenne": r["moyenne"], "examen": r["examen"]}
            for _, r in n.iterrows()}
    for _, r in lignes.iterrows():
        cle = (r["Matricule"], r["Cours"])
        val = base.pop(cle, {"moyenne": "", "examen": ""})   # remis en fin de liste = le plus récent
        val[champ] = f"{r['_val']:g}"
        base[cle] = val
    return pd.DataFrame([[k[0], k[1], v["moyenne"], v["examen"]] for k, v in base.items()],
                        columns=FICHIERS["notes"])


def enregistrer_sexes(profils):
    """profils : {matricule: sexe}. Ne remplace jamais un sexe déjà renseigné."""
    lignes_p = []
    for mat, sx in profils.items():
        pf = profil_de(mat)
        if not pf["sexe"]:
            pf["sexe"] = sx
            lignes_p.append([mat] + [pf[c] for c in FICHIERS["profils"][1:]])
    if lignes_p:
        sauver("profils", fusionner(charger("profils"),
                                    pd.DataFrame(lignes_p, columns=FICHIERS["profils"]), CLES["profils"]))


def import_etudiants_excel(fac, promo):
    st.markdown(f"**Liste des étudiants de {promo} : envoyez votre fichier (Excel, CSV, Word ou PDF).**")
    st.caption("Aucun modèle à respecter, peu importe la forme du fichier : la plateforme prend seulement le nom "
               "complet de chaque étudiant (et son matricule s'il existe). Quand le nom est écrit en entier, le 1er mot "
               "est le nom, le 2e le post-nom et le 3e (ou le reste) le prénom. Si la fiche a déjà un matricule, "
               "il est conservé et la plateforme donne seulement le mot de passe. Sans matricule, elle génère "
               "le matricule et le mot de passe. La faculté et la promotion sont celles choisies en haut de la page.")
    f = st.file_uploader("Fichier des étudiants", type=TYPES_FICHIERS, key="up_etudiants")
    if f is None:
        return
    g = charger_grille(f, "etudiants", ["matricule", "nom", "postnom", "prenom", "sexe"])
    if g is None:
        return
    pers = personnes_depuis_grille(*g)
    if pers.empty:
        st.error("Aucun nom d'étudiant reconnu dans ce fichier.")
        return
    st.markdown(f"**{len(pers)} étudiant(s) reconnu(s) : vérifiez la liste avant d'enregistrer.**")
    st.dataframe(pers.drop(columns=["_ligne", "promotion"]).rename(columns={
        "matricule": "Matricule", "nom": "Nom", "postnom": "Post-nom", "prenom": "Prénom", "sexe": "Genre"}),
        hide_index=True)
    if st.button("Enregistrer et donner les mots de passe", type="primary", key="btn_imp_et"):
        existants = charger("etudiants")
        df, profils, erreurs, ignores = preparer_etudiants(pers.drop(columns=["_ligne", "promotion"]), fac, promo,
                                                           existants, params()["annee"])
        for msg in ignores:
            st.warning(msg)
        if erreurs:
            st.error("Import annulé :\n\n" + "\n\n".join(erreurs))
            return
        if df.empty:
            st.info("Aucun nouvel étudiant à enregistrer.")
            return
        sauver("etudiants", fusionner(existants, df, CLES["etudiants"]))
        enregistrer_sexes(profils)
        ajouter_promotion(fac, promo)
        for _, r in df.iterrows():
            st.session_state.setdefault("nouveaux_ids", []).append(
                {"Matricule": r["matricule"], "Nom": r["nom"], "Post-nom": r["postnom"], "Prénom": r["prenom"],
                 "Faculté": faculte_de(r), "Promotion": r["promotion"], "Mot de passe": r["code"]})
        st.success(f"{len(df)} étudiants enregistrés avec leur matricule et leur mot de passe.")


def import_cours_excel(fac, promo):
    st.markdown(f"**Liste des cours de {promo} : envoyez votre fichier (Excel, CSV, Word ou PDF).**")
    st.caption("Aucun modèle à respecter : la plateforme prend seulement l'intitulé du cours, le nombre d'heures "
               f"et le nombre de crédits. Les cours sont rattachés à {promo} ({fac}) : seuls les étudiants de "
               "cette promotion les verront.")
    f = st.file_uploader("Fichier des cours", type=TYPES_FICHIERS, key="up_cours")
    if f is None:
        return
    inverser = st.checkbox("Inverser heures et crédits (si la plateforme les a mélangés)", key="co_inverser")
    g = charger_grille(f, "cours", ["intitule", "heures", "credits", "promotion"])
    if g is None:
        return
    df, erreurs = lire_cours_libres(g[0], g[1], promo, fac, inverser)
    if erreurs:
        st.error("\n\n".join(erreurs))
    if df is None or df.empty:
        return
    st.markdown(f"**{len(df)} cours reconnu(s) : vérifiez les heures et les crédits avant d'enregistrer.**")
    st.dataframe(df[["intitule", "heures", "credits"]].rename(columns={
        "intitule": "Cours", "heures": "Heures", "credits": "Crédits"}), hide_index=True)
    if erreurs:
        st.caption("Les lignes en erreur ne seront pas enregistrées.")
    if st.button("Enregistrer les cours", type="primary", key="btn_imp_co"):
        sauver("cours", fusionner(charger("cours"), df, CLES["cours"]))
        ajouter_promotion(fac, promo)
        st.success(f"{len(df)} cours enregistrés pour {promo} ({fac}).")


def import_notes_excel(co, et):
    """co et et : cours et étudiants de la faculté et de la promotion choisies."""
    cours = st.selectbox("Cours", cours_recents_d_abord(co, et), key="imp_no_cours")
    st.markdown(f"**Notes de « {cours} » : envoyez le fichier du professeur (Excel, CSV, Word ou PDF).**")
    st.caption("Aucun modèle à respecter : chaque étudiant est retrouvé par son nom ou son matricule, "
               "et vous choisissez la colonne de cotes à prendre. Le reste du fichier est ignoré.")
    type_cote = st.radio("Ce fichier contient", ["Les moyennes /10 (interrogations, TP, TD...)",
                                                  "Les cotes d'examen /10"], key="imp_no_type")
    champ = "moyenne" if type_cote.startswith("Les moyennes") else "examen"
    f = st.file_uploader("Fichier des cotes", type=TYPES_FICHIERS, key="up_notes_xl")
    if f is None:
        return
    g = charger_grille(f, "notes", ["matricule", "nom", "postnom", "prenom", "sexe", "cote"])
    if g is None:
        return
    df, carte = g
    pers = personnes_depuis_grille(df, carte)
    if pers.empty:
        st.error("Aucun étudiant reconnu dans ce fichier.")
        return
    options = colonnes_de_cotes(df, carte, pers)
    if not options:
        st.error("Aucune colonne de cotes (nombres) trouvée dans ce fichier.")
        return
    cle_c, cle_cote = _norm(cours), "cote"
    defaut = next((i for i, c in enumerate(options) if _norm(c) == cle_c), None)
    if defaut is None:
        defaut = next((i for i, c in enumerate(options) if _norm(c) in SYNONYMES[cle_cote]), 0)

    def etiquette(c):
        ex = next((str(df.at[i, c]).strip() for i in pers["_ligne"] if str(df.at[i, c]).strip()), "")
        return f"{c} (ex. : {ex})"

    colonne = st.selectbox("Colonne de cotes à prendre", options, index=defaut, format_func=etiquette,
                           key=f"col_cotes_{cours}")
    res, msg = analyser_notes(pers, df, colonne, et, cours)
    if res is None:
        st.error(msg)
        return
    st.dataframe(res.drop(columns=["_ok", "_val"]), hide_index=True)
    ok = res[res["_ok"]]
    st.caption(f"{len(ok)} ligne(s) valide(s) sur {len(res)}. Les lignes en erreur ne seront pas importées.")
    if not ok.empty and st.button(f"Enregistrer ({'moyennes' if champ == 'moyenne' else 'examens'})",
                                  type="primary", key="btn_imp_no"):
        sauver("notes", appliquer_notes(charger("notes"), ok, champ))
        enregistrer_sexes({r["Matricule"]: r["Genre"] for _, r in ok[ok["Genre"] != ""].iterrows()})
        st.success(f"{len(ok)} cotes enregistrées pour « {cours} » uniquement.")


def modele_notes_cours(grp, cours):
    """Fichier à remettre au professeur : matricule, nom, postnom, prénom + colonne du cours (vide)."""
    lignes = [[r["matricule"], r["nom"], r["postnom"], r["prenom"], ""] for _, r in grp.iterrows()]
    return modele_excel(["matricule", "nom", "postnom", "prenom", cours], lignes)


def cours_recents_d_abord(co, et):
    """Cours de la promotion : ceux dont on vient d'ajouter des cotes d'abord, puis les autres (ordre alphabétique)."""
    intitules = list(co["intitule"].unique())
    n = charger("notes")
    n = n[n["matricule"].isin(et["matricule"])]
    recents = [c for c in dict.fromkeys(n["cours"].tolist()[::-1]) if c in intitules]
    return recents + sorted(c for c in intitules if c not in recents)


# ---------- Choix faculté + promotion (commun à toutes les pages) ----------
def choisir_groupe(cle, promo=True):
    """Université (si plusieurs), faculté, puis promotion de cette faculté. Retourne (faculté, promotion)."""
    fac_df, u_df = facultes_df(), universites_df()
    if len(u_df) > 1:
        univ = st.selectbox("Université", u_df["universite"].tolist(), key=f"{cle}_univ")
        fac_df = fac_df[fac_df["universite"] == univ]
    if fac_df.empty:
        st.info("Cette université n'a pas encore de faculté. Ajoutez-en dans le menu Facultés.")
        st.stop()
    fac = st.selectbox("Faculté", fac_df["faculte"].tolist(), key=f"{cle}_fac")
    if not promo:
        return fac, ""
    promos = liste_promos(fac_df[fac_df["faculte"] == fac].iloc[0]["promotions"])
    if not promos:
        st.info("Cette faculté n'a pas encore de promotion. Ajoutez-en dans le menu Facultés.")
        return fac, ""
    return fac, st.selectbox("Promotion", promos, key=f"{cle}_promo_{fac}")


def meme_faculte(serie, fac):
    cible = faculte_de({"faculte": fac}).lower()
    return serie.map(lambda f: faculte_de({"faculte": f}).lower() == cible).astype(bool)


def etudiants_du_groupe(et, fac, promo):
    return et[(et["promotion"] == promo) & meme_faculte(et["faculte"], fac)]


def migrer_cours_sans_code():
    """Une seule fois : supprime les anciens cours (à code). Les cotes déjà saisies sont rattachées
    à l'intitulé du cours pour réapparaître quand la faculté renverra sa liste."""
    co = charger("cours")
    if "code_cours" not in co.columns:
        return
    no, et = charger("notes"), charger("etudiants")
    if "code_cours" in no.columns and not no.empty:
        promo = dict(zip(et["matricule"], et["promotion"]))
        cle = {(r["code_cours"], r["promotion"]): r["intitule"] for _, r in co.iterrows()}
        no = no.copy()
        no["cours"] = [cle.get((r["code_cours"], promo.get(r["matricule"], "")), "")
                       for _, r in no.iterrows()]
        sauver("notes", no[no["cours"] != ""][FICHIERS["notes"]])
    elif "code_cours" in no.columns:
        sauver("notes", no[FICHIERS["notes"]])
    sauver("cours", pd.DataFrame(columns=FICHIERS["cours"]))
    st.info("Les anciens cours (avec code) ont été supprimés. Importez la liste de cours envoyée par "
            "la faculté pour chaque promotion.")


def page_universites():
    u_df = universites_df()
    fac_df = facultes_df()
    st.markdown("**Universités enregistrées**")
    st.dataframe(pd.DataFrame({
        "Université": u_df["universite"], "Sigle": u_df["sigle"], "Pays": u_df["pays"],
        "Adresse": u_df["adresse"], "Email": u_df["email"], "Téléphone": u_df["telephone"],
        "Facultés": [int((fac_df["universite"] == n).sum()) for n in u_df["universite"]],
        "Logo": ["Oui" if logo_universite(n) else "Non" for n in u_df["universite"]]}), hide_index=True)

    st.markdown("**Ajouter ou modifier une université**")
    st.caption("Pour modifier une université (nom, sigle, pays, adresse, email, logo), saisissez le même nom. "
               "Son nom, son logo, son adresse et son email apparaissent sur les cartes de ses étudiants "
               "(verso) ; le nom et le logo aussi sur les bulletins.")
    with st.form("f_univ", clear_on_submit=True):
        nom = st.text_input("Nom de l'université (ex. UNIVERSITÉ DE KOLWEZI)")
        sigle = st.text_input("Sigle (ex. UNIKOL)")
        pays = st.text_input("Pays", PAYS)
        adresse = st.text_input("Adresse de l'université (ex. Av. de l'Université, Kolwezi)")
        email = st.text_input("Email de l'université (ex. contact@unikol.ac.cd)")
        telephone = st.text_input("Téléphone (facultatif)")
        logo_up = st.file_uploader("Logo (PNG ou JPG)", type=["png", "jpg", "jpeg"])
        ok = st.form_submit_button("Enregistrer l'université", type="primary")
    if ok:
        if not nom.strip():
            st.error("Indiquez le nom de l'université.")
        else:
            if email.strip() and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
                st.error("L'adresse email n'est pas valide (exemple : contact@unikol.ac.cd).")
                st.stop()
            ligne = pd.DataFrame([[nom.strip(), sigle.strip(), pays.strip() or PAYS, adresse.strip(),
                                   email.strip(), telephone.strip()]],
                                 columns=FICHIERS["universites"])
            sauver_universites(fusionner(u_df, ligne, CLES["universites"]))
            if logo_up is not None:
                try:
                    sauver_logo_universite(nom.strip(), logo_up)
                except Exception:
                    st.error("Logo illisible. Essayez un autre fichier (PNG ou JPG).")
            st.success("Université enregistrée.")
            st.rerun()

    st.markdown("---")
    st.markdown("**Logo d'une université**")
    choix = st.selectbox("Université", u_df["universite"].tolist(), key="un_logo_choix")
    logo = logo_universite(choix)
    if logo:
        st.image(logo, width=110)
    else:
        st.info("Aucun logo pour cette université.")
    nouveau = st.file_uploader("Nouveau logo (PNG ou JPG)", type=["png", "jpg", "jpeg"], key="un_logo_up")
    c1, c2 = st.columns(2)
    if c1.button("Enregistrer le logo", key="un_logo_ok") and nouveau is not None:
        try:
            sauver_universites(u_df)      # fige la liste (université par défaut incluse)
            sauver_logo_universite(choix, nouveau)
            st.success("Logo enregistré.")
            st.rerun()
        except Exception:
            st.error("Image illisible.")
    if logo and c2.button("Supprimer le logo", key="un_logo_rm"):
        supprimer_logo_universite(choix)
        st.rerun()

    with st.expander("Supprimer une université"):
        st.caption("Supprime aussi toutes ses facultés, leurs promotions, étudiants, cours, notes et "
                   "délibérations. Action définitive : téléchargez d'abord une sauvegarde.")
        a_sup = st.selectbox("Université à supprimer", u_df["universite"].tolist(), key="un_sup")
        sur = st.checkbox("Je confirme la suppression définitive", key="un_sup_ok")
        if st.button("Supprimer l'université", key="un_sup_btn"):
            if not sur:
                st.error("Cochez la confirmation.")
            else:
                for f in fac_df.loc[fac_df["universite"] == a_sup, "faculte"].tolist():
                    supprimer_faculte(f)
                supprimer_logo_universite(a_sup)
                reste = u_df[u_df["universite"] != a_sup]
                sauver_universites(reste)
                st.success("Université supprimée.")
                st.rerun()


def supprimer_promotions(fac, promos):
    """Retire des promotions d'une faculté, avec leurs étudiants, cours, notes et délibérations."""
    fac_df = facultes_df()
    cible = faculte_de({"faculte": fac}).lower()
    et = charger("etudiants")
    mats = et[meme_faculte(et["faculte"], fac) & et["promotion"].isin(promos)]["matricule"].tolist()
    if mats:
        supprimer_etudiants(mats)
    co = charger("cours")
    garder = ~(co["promotion"].isin(promos) & meme_faculte(co["faculte"], fac) & (co["faculte"] != ""))
    sauver("cours", co[garder])
    for i, r in fac_df.iterrows():
        if faculte_de({"faculte": r["faculte"]}).lower() == cible:
            fac_df.at[i, "promotions"] = ", ".join(x for x in liste_promos(r["promotions"]) if x not in promos)
    sauver("facultes", fac_df)


def supprimer_faculte(fac):
    """Supprime la faculté avec toutes ses promotions et leurs données."""
    sauver_universites(universites_df())      # fige la liste pour ne pas recréer la faculté par défaut
    fac_df = facultes_df()
    ligne = fac_df[fac_df["faculte"] == fac]
    if not ligne.empty:
        supprimer_promotions(fac, liste_promos(ligne.iloc[0]["promotions"]))
    et = charger("etudiants")
    reste = et[meme_faculte(et["faculte"], fac)]["matricule"].tolist()
    if reste:
        supprimer_etudiants(reste)
    co = charger("cours")
    sauver("cours", co[~((co["faculte"] != "") & meme_faculte(co["faculte"], fac))])
    sauver("facultes", facultes_df().query("faculte != @fac"))


def page_facultes():
    fac_df, u_df, p_an = facultes_df(), universites_df(), params()["annee"]

    st.markdown("**Ajouter ou modifier une faculté**")
    st.caption("Pour modifier une faculté, saisissez le même nom : le code et les promotions sont remplacés.")
    with st.form("f_fac", clear_on_submit=True):
        univ = st.selectbox("Université", u_df["universite"].tolist())
        nf = st.text_input("Nom de la faculté (ex. Faculté des Sciences : GEOLOGIE)")
        cf = st.text_input("Code de la faculté (ex. 51/kz)")
        pf = st.text_input("Promotions, séparées par des virgules (ex. L1 Géologie, L2 Géologie)")
        envoye = st.form_submit_button("Enregistrer la faculté", type="primary")
    if envoye:
        meme_nom = fac_df[fac_df["faculte"].map(lambda x: faculte_de({"faculte": x}).lower())
                          == faculte_de({"faculte": nf}).lower()]
        code_pris = fac_df[(fac_df["code"].str.strip().str.lower() == cf.strip().lower())
                           & (fac_df["universite"] == univ)
                           & (fac_df["faculte"].map(lambda x: faculte_de({"faculte": x}).lower())
                              != faculte_de({"faculte": nf}).lower())]
        if not (nf.strip() and cf.strip()):
            st.error("Indiquez le nom et le code de la faculté.")
        elif not meme_nom.empty and (meme_nom["universite"] != univ).any():
            st.error("Ce nom de faculté existe déjà dans une autre université. Ajoutez le sigle de "
                     "l'université dans le nom (ex. « Faculté de Droit - UNILU »).")
        elif not code_pris.empty:
            st.error(f"Ce code est déjà utilisé par : {code_pris.iloc[0]['faculte']}.")
        else:
            sauver_universites(u_df)
            ligne_f = pd.DataFrame([[nf.strip(), cf.strip(), ", ".join(liste_promos(pf)), univ]],
                                   columns=FICHIERS["facultes"])
            sauver("facultes", fusionner(fac_df, ligne_f, CLES["facultes"]))
            st.success("Faculté enregistrée.")
            st.rerun()

    st.markdown("---")
    fac, _ = choisir_groupe("fa", promo=False)
    fac_df = facultes_df()
    i = fac_df.index[fac_df["faculte"] == fac][0]
    code, promos = fac_df.at[i, "code"], liste_promos(fac_df.at[i, "promotions"])
    st.markdown(f"**Université :** {fac_df.at[i, 'universite']} · **Code de la faculté :** {code} · "
                f"premier matricule de l'année {p_an} : {prefixe_matricule(code, p_an)}001")
    et_tous = charger("etudiants")
    if promos:
        st.dataframe(pd.DataFrame({"Promotion": promos,
                                   "Étudiants": [len(etudiants_du_groupe(et_tous, fac, pr)) for pr in promos]}),
                     hide_index=True)
    else:
        st.info("Cette faculté n'a pas encore de promotion.")

    k = st.session_state.get("fa_k", 0)
    nouvelle = st.text_input("Ajouter une promotion à cette faculté (ex. Bac 1 Géologie)", key=f"fa_np_{k}")
    if st.button("Ajouter la promotion", key="fa_add"):
        if not nouvelle.strip():
            st.error("Écrivez le nom de la promotion.")
        elif nouvelle.strip().lower() in [x.lower() for x in promos]:
            st.warning("Cette promotion existe déjà.")
        else:
            sauver_universites(u_df)
            fac_df.at[i, "promotions"] = ", ".join(promos + [nouvelle.strip()])
            sauver("facultes", fac_df)
            st.session_state["fa_k"] = k + 1
            st.rerun()

    if promos:
        with st.expander("Supprimer des promotions de cette faculté"):
            st.caption("Supprime aussi les étudiants, cours, notes et délibérations de ces promotions. "
                       "Action définitive.")
            sortir = st.multiselect("Promotions à supprimer", promos, key=f"fa_rm_{fac}")
            sur_p = st.checkbox("Je confirme la suppression définitive", key="fa_rm_conf")
            if st.button("Supprimer les promotions", key="fa_rm_ok"):
                if not sortir or not sur_p:
                    st.error("Choisissez des promotions et cochez la confirmation.")
                else:
                    sauver_universites(u_df)
                    supprimer_promotions(fac, sortir)
                    st.rerun()

    with st.expander("Supprimer cette faculté"):
        st.caption(f"Supprime « {fac} » avec toutes ses promotions, ses étudiants, ses cours, ses notes et "
                   "ses délibérations. Action définitive : téléchargez d'abord une sauvegarde "
                   "(menu « Sauvegarde »).")
        sur_f = st.checkbox("Je confirme la suppression définitive de cette faculté", key="fac_sup_conf")
        if st.button("Supprimer la faculté", key="fac_sup_ok"):
            if not sur_f:
                st.error("Cochez la confirmation.")
            else:
                supprimer_faculte(fac)
                st.success("Faculté supprimée.")
                st.rerun()


def page_etudiants():
    fac, promo = choisir_groupe("et")
    if not promo:
        return
    fac_df, p_an = facultes_df(), params()["annee"]
    st.markdown("---")
    import_etudiants_excel(fac, promo)
    afficher_nouveaux_ids()

    k = st.session_state.get("n_et", 0)
    with st.expander("Ajouter un étudiant à la main"):
        st.markdown(f"**Nouvel étudiant en {promo}** : le matricule et le mot de passe sont générés.")
        n = st.text_input("Nom", key=f"et_nom_{k}")
        pn = st.text_input("Post-nom", key=f"et_pn_{k}")
        pr = st.text_input("Prénom", key=f"et_pr_{k}")
        sx = st.selectbox("Genre", ["", "Masculin", "Féminin"], key=f"et_sx_{k}")
        no_liste = st.number_input("Numéro sur la liste d'inscription (0 = automatique)", 0, step=1,
                                   key=f"et_no_{k}")
        tous_et = charger("etudiants")
        apercu, _ = nouveau_matricule(tous_et, code_faculte(fac_df, fac), p_an, int(no_liste) or None)
        if apercu:
            st.caption(f"Matricule qui sera attribué : **{apercu}** (année académique {p_an})")
        else:
            st.caption("Ce numéro de la liste est déjà attribué.")
        if st.button("Ajouter l'étudiant", type="primary", key=f"et_ok_{k}"):
            if not n.strip():
                st.error("Remplissez le nom.")
            elif not apercu:
                st.error("Ce numéro de la liste d'inscription est déjà pris. Changez-le ou mettez 0.")
            else:
                code_perso = generer_code()
                ligne = pd.DataFrame([[apercu, n.strip(), promo, code_perso, fac, "", pn.strip(), pr.strip()]],
                                     columns=FICHIERS["etudiants"])
                sauver("etudiants", fusionner(tous_et, ligne, CLES["etudiants"]))
                if sx:
                    enregistrer_sexes({apercu: sx})
                ajouter_promotion(fac, promo)
                st.session_state.setdefault("nouveaux_ids", []).append(
                    {"Matricule": apercu, "Nom": n.strip(), "Post-nom": pn.strip(), "Prénom": pr.strip(),
                     "Faculté": fac, "Promotion": promo, "Mot de passe": code_perso})
                st.session_state["n_et"] = k + 1
                st.rerun()

    grp = etudiants_du_groupe(charger("etudiants"), fac, promo)
    if not grp.empty:
        with st.expander("Donner un nouveau mot de passe"):
            st.caption("Génère un nouveau mot de passe (le matricule ne change pas).")
            opts = [f"{m} — {nom_complet(r)}" for m, (_, r) in zip(grp["matricule"], grp.iterrows())]
            qui = st.multiselect("Étudiants", opts, key=f"mdp_{fac}_{promo}")
            if st.button("Générer de nouveaux mots de passe", key="mdp_ok") and qui:
                tous = charger("etudiants")
                for c in qui:
                    m = c.split(" — ")[0]
                    nv = generer_code()
                    tous.loc[tous["matricule"] == m, "code"] = nv
                    r = tous[tous["matricule"] == m].iloc[0]
                    st.session_state.setdefault("nouveaux_ids", []).append(
                        {"Matricule": m, "Nom": r["nom"], "Post-nom": r["postnom"], "Prénom": r["prenom"],
                         "Faculté": fac, "Promotion": promo, "Mot de passe": nv})
                sauver("etudiants", tous)
                st.rerun()

    if not grp.empty:
        with st.expander("Télécharger la liste des étudiants (Word / PDF)"):
            liste = grp[["matricule", "nom", "postnom", "prenom", "code"]].rename(columns={
                "matricule": "Matricule", "nom": "Nom", "postnom": "Post-nom", "prenom": "Prénom",
                "code": "Mot de passe"})
            st.caption("La liste contient le matricule et le mot de passe de chaque étudiant.")
            boutons_documents(f"Liste des étudiants · {promo}", f"{fac} · année académique {p_an}",
                              liste, f"liste_etudiants_{_cle_photo(promo)}", "dl_liste")

    with st.expander("Supprimer des étudiants"):
        st.caption("Supprime l'étudiant, ses notes, son profil, sa photo et ses délibérations "
                   "enregistrées. Cette action est définitive : téléchargez d'abord une sauvegarde "
                   "(menu « Sauvegarde »).")
        if grp.empty:
            st.info("Aucun étudiant dans cette promotion.")
        else:
            options = [f"{m} — {nom_complet(r)}" for m, (_, r) in zip(grp["matricule"], grp.iterrows())]
            tout = st.checkbox(f"Sélectionner tous les étudiants affichés ({len(options)})", key="sup_tout")
            choisis = st.multiselect("Étudiants à supprimer", options,
                                     default=options if tout else [], key=f"sup_choix_{fac}_{promo}_{tout}")
            sur = st.checkbox("Je confirme la suppression définitive", key="sup_confirme")
            if st.button("Supprimer la sélection", type="primary", key="sup_ok"):
                if not choisis:
                    st.error("Choisissez au moins un étudiant.")
                elif not sur:
                    st.error("Cochez la confirmation pour supprimer.")
                else:
                    supprimer_etudiants([c.split(" — ")[0] for c in choisis])
                    st.session_state["sup_message"] = f"{len(choisis)} étudiant(s) supprimé(s)."
                    st.rerun()
    if st.session_state.get("sup_message"):
        st.success(st.session_state.pop("sup_message"))
    st.markdown(f"**Étudiants de {promo} ({fac}) : {len(grp)}**")
    st.dataframe(grp[["matricule", "nom", "postnom", "prenom", "code", "en_ordre"]].rename(columns={
        "matricule": "Matricule", "nom": "Nom", "postnom": "Post-nom", "prenom": "Prénom",
        "code": "Mot de passe", "en_ordre": "En ordre"}), hide_index=True)
    with st.expander("Profils remplis par les étudiants"):
        pr = charger("profils")
        st.dataframe(pr[pr["matricule"].isin(grp["matricule"])], hide_index=True)


def page_cours():
    fac, promo = choisir_groupe("co")
    if not promo:
        return
    import_cours_excel(fac, promo)
    with st.expander("Ajouter un cours à la main (cas exceptionnel)"):
        with st.form("f_co", clear_on_submit=True):
            it = st.text_input("Intitulé du cours")
            h = st.number_input("Heures", 0, step=5)
            cr = st.number_input("Crédits (ex. 3)", 0, step=1, value=3)
            envoye = st.form_submit_button("Ajouter")
            if envoye and not it.strip():
                st.error("Remplissez l'intitulé.")
            if envoye and it.strip():
                ligne = pd.DataFrame([[it.strip(), promo, str(h), str(cr), fac]], columns=FICHIERS["cours"])
                sauver("cours", fusionner(charger("cours"), ligne, CLES["cours"]))
                st.success(f"Cours ajouté à {promo}.")
    tous = charger("cours")
    grp = cours_de(tous, promo, fac)
    if not grp.empty:
        with st.expander("Supprimer des cours"):
            tout_c = st.checkbox(f"Sélectionner tous les cours affichés ({len(grp)})", key="sup_co_tout")
            choisis_c = st.multiselect("Cours à supprimer", grp["intitule"].tolist(),
                                       default=grp["intitule"].tolist() if tout_c else [],
                                       key=f"sup_co_choix_{fac}_{promo}_{tout_c}")
            sur_c = st.checkbox("Je confirme la suppression", key="sup_co_ok")
            if st.button("Supprimer les cours choisis", key="sup_co_btn"):
                if not choisis_c or not sur_c:
                    st.error("Choisissez des cours et cochez la confirmation.")
                else:
                    sauver("cours", tous.drop(index=grp.index[grp["intitule"].isin(choisis_c)]))
                    st.success(f"{len(choisis_c)} cours supprimé(s).")
                    st.rerun()
        total = pd.to_numeric(grp["credits"], errors="coerce").fillna(0).sum()
        st.info(f"{promo} : {total:g} crédits au total (somme des crédits de tous les cours).")
    st.dataframe(grp, hide_index=True)


def page_notes():
    fac, promo = choisir_groupe("no")
    if not promo:
        return
    co = cours_de(charger("cours"), promo, fac)
    grp = etudiants_du_groupe(charger("etudiants"), fac, promo)
    if co.empty or grp.empty:
        st.info(f"Enregistrez d'abord la liste des cours et les étudiants de {promo}.")
        return
    st.markdown("---")
    import_notes_excel(co, grp)
    with st.expander("Saisir une note à la main (cas exceptionnel)"):
        with st.form("f_no", clear_on_submit=True):
            qui = st.selectbox("Étudiant", [f"{m} — {nom_complet(r)}" for m, (_, r) in
                                            zip(grp["matricule"], grp.iterrows())])
            cc = st.selectbox("Cours", sorted(co["intitule"].unique()))
            mo = st.number_input("Moyenne /10 (interrogations, TP, TD...)", 0.0, 10.0, 0.0, 0.25)
            ex = st.number_input("Examen /10", 0.0, 10.0, 0.0, 0.25)
            avec_ex = st.checkbox("Enregistrer aussi la cote d'examen "
                                  "(décocher si l'examen n'est pas encore passé)", value=True)
            if st.form_submit_button("Enregistrer"):
                m = qui.split(" — ")[0]
                n = charger("notes")
                ancien = n[(n["matricule"] == m) & (n["cours"] == cc)]
                ex_val = str(ex) if avec_ex else (ancien.iloc[0]["examen"] if not ancien.empty else "")
                ligne = pd.DataFrame([[m, cc, str(mo), ex_val]], columns=FICHIERS["notes"])
                sauver("notes", fusionner(n, ligne, CLES["notes"]))
                st.success("Note enregistrée (total /20 = moyenne + examen).")
    cours_vu = st.session_state.get("imp_no_cours")
    no = charger("notes")
    no = no[no["matricule"].isin(grp["matricule"]) & ((no["cours"] == cours_vu) if cours_vu else True)]
    nom_de = {r["matricule"]: nom_complet(r) for _, r in grp.iterrows()}
    st.markdown(f"**Notes enregistrées : {cours_vu or 'tous les cours'}**")
    no = no.iloc[::-1]   # dernières cotes ajoutées en premier
    st.dataframe(no.assign(nom=no["matricule"].map(nom_de))[["matricule", "nom", "cours", "moyenne", "examen"]],
                 hide_index=True)


def page_paiements():
    toute = st.checkbox("Liste de toute la faculté (toutes les promotions dans le même fichier)", key="pa_toute")
    fac, promo = choisir_groupe("pa", promo=not toute)
    if not toute and not promo:
        return
    et = charger("etudiants")
    if toute:
        grp = et[meme_faculte(et["faculte"], fac)]
        portee = f"la faculté ({fac})"
    else:
        grp = etudiants_du_groupe(et, fac, promo)
        portee = promo
    if grp.empty:
        st.info(f"Aucun étudiant inscrit en {portee}.")
        return
    st.markdown(f"**Étudiants de {portee} en ordre avec les frais.** Seuls les étudiants de cette liste ont "
                "accès à la délibération.")
    if toute:
        st.caption("Envoyez la liste de toute la faculté (Excel, CSV, Word ou PDF). Aucun modèle à respecter : "
                   "la plateforme lit la promotion de chaque étudiant dans l'en-tête du fichier (colonne "
                   "« Promotion », titre de chaque section ou nom de chaque feuille Excel), puis prend "
                   "seulement les noms.")
    else:
        st.caption("Envoyez la liste des étudiants en ordre (Excel, CSV, Word ou PDF). Aucun modèle à respecter : "
                   "la plateforme prend seulement les noms (ou matricules), peu importe la forme du fichier.")
    f = st.file_uploader("Fichier des étudiants en ordre", type=TYPES_FICHIERS, key="up_ordre")
    seul = st.checkbox("Remplacer la liste précédente (les étudiants absents du fichier ne sont plus en ordre"
                       + (", pour les promotions présentes dans le fichier)" if toute else ")"),
                       True, key="ordre_seul")
    n_ed = st.session_state.get("ed_n", 0)
    if f is not None:
        g = charger_grille(f, "ordre", ["matricule", "nom", "postnom", "prenom", "promotion"], toute)
        promos = sorted(grp["promotion"].unique()) if toute else ()
        res, msg = (None, "") if g is None else analyser_liste_ordre(
            personnes_depuis_grille(g[0], g[1], promos), grp, toute)
        if g is None:
            pass   # message d'erreur déjà affiché
        elif res is None:
            st.error(msg)
        else:
            st.dataframe(res.drop(columns=["_i"]), hide_index=True)
            ok = res[res["Statut"] == "OK"]
            st.caption(f"{len(ok)} étudiant(s) reconnu(s) sur {len(res)}.")
            if not ok.empty and st.button("Enregistrer cette liste", type="primary", key="ordre_ok"):
                reconnus = [int(x) for x in ok["_i"]]
                if seul:
                    concernees = set(et.loc[reconnus, "promotion"])
                    cible = grp[grp["promotion"].isin(concernees)] if toute else grp
                    et.loc[cible.index, "en_ordre"] = "0"
                et.loc[reconnus, "en_ordre"] = "1"
                sauver("etudiants", et)
                st.session_state["ed_n"] = n_ed + 1
                st.success("Liste des étudiants en ordre enregistrée.")
                st.rerun()

    ordre = grp[grp["en_ordre"] == "1"]
    st.markdown(f"**Étudiants en ordre : {len(ordre)} sur {len(grp)}**")
    if ordre.empty:
        st.info("Aucun étudiant en ordre pour le moment.")
        return
    colonnes = ["matricule", "nom", "postnom", "prenom"] + (["promotion"] if toute else [])
    liste = ordre[colonnes].rename(columns={
        "matricule": "Matricule", "nom": "Nom", "postnom": "Post-nom", "prenom": "Prénom",
        "promotion": "Promotion"})
    st.dataframe(liste, hide_index=True)
    with st.expander("Retirer des étudiants de la liste"):
        opts = [f"{m} — {nom_complet(r)}" for m, (_, r) in zip(ordre["matricule"], ordre.iterrows())]
        sortir = st.multiselect("Étudiants à retirer", opts, key=f"ord_rm_{fac}_{promo}_{n_ed}")
        if st.button("Retirer de la liste", key="ord_rm_ok") and sortir:
            mats = {c.split(" — ")[0] for c in sortir}
            et.loc[et["matricule"].isin(mats), "en_ordre"] = "0"
            sauver("etudiants", et)
            st.session_state["ed_n"] = n_ed + 1
            st.rerun()


def page_deliberation():
    p = params()
    fac, promo = choisir_groupe("de")
    session = st.selectbox("Session", SESSIONS, key="delib_session")
    with st.expander("Paramètres généraux (année, publication, blocage)"):
        p["annee"] = st.text_input("Année académique", p["annee"])
        p["bloquer_impaye"] = st.checkbox("Bloquer les résultats des étudiants qui ne sont pas en ordre",
                                          p["bloquer_impaye"])
        p["deliberation_publiee"] = st.toggle("Publier les résultats de la délibération",
                                              p["deliberation_publiee"])
        if st.button("Appliquer"):
            sauver_params(p)
            st.success("Paramètres enregistrés.")
    if not promo:
        return
    co, no = charger("cours"), charger("notes")
    grp = etudiants_du_groupe(charger("etudiants"), fac, promo)
    ordre = grp[grp["en_ordre"] == "1"]
    st.markdown("---")
    st.markdown(f"**Délibération · {session} · {promo} ({fac})**")
    st.caption(f"{len(ordre)} étudiant(s) en ordre sur {len(grp)} : seuls les étudiants enregistrés dans "
               "« Paiements » sont délibérés et pourront voir leurs résultats. "
               "1) Calculez les résultats. 2) Vérifiez-les. 3) Publiez.")
    hors = grp[grp["en_ordre"] != "1"]
    if not hors.empty:
        with st.expander(f"{len(hors)} étudiant(s) non délibéré(s) (pas en ordre dans « Paiements »)"):
            st.dataframe(hors[["matricule", "nom", "postnom", "prenom"]].rename(columns={
                "matricule": "Matricule", "nom": "Nom", "postnom": "Post-nom", "prenom": "Prénom"}),
                hide_index=True)
    cle_calc = f"calc_{fac}_{promo}_{session}"
    if st.button("Calculer les résultats", type="primary", key="delib_calc"):
        if ordre.empty:
            st.error("Aucun étudiant en ordre dans cette promotion (menu Paiements).")
        else:
            st.session_state[cle_calc] = True
    if st.session_state.get(cle_calc) and not ordre.empty:
        lignes = []
        for _, e in ordre.iterrows():
            dd, moy, acq, tot = bilan(e["matricule"], e["promotion"], co, no, faculte_de(e))
            lignes.append({"Matricule": e["matricule"], "Nom": e["nom"], "Post-nom": e["postnom"],
                           "Prénom": e["prenom"],
                           "Moyenne": None if moy is None else round(moy, 2),
                           "Crédits validés": f"{acq:g}/{tot:g}",
                           "Mention": mention(acq, dd), "Décision": decision(acq, dd)})
        rapport = pd.DataFrame(lignes)
        st.markdown(f"**Résultats calculés · {session} (étudiants en ordre)**")
        st.dataframe(rapport, hide_index=True)
        st.download_button("Télécharger le PV (CSV)", rapport.to_csv(index=False),
                           f"deliberation_{_cle_photo(promo)}_{_cle_photo(session)}.csv", "text/csv")
        c1, c2 = st.columns(2)
        publier = c1.button("Publier les résultats", type="primary", key="delib_publier")
        garder = c2.button("Enregistrer sans publier", key="delib_enreg")
        if publier or garder:
            snap = archiver_deliberation(ordre, co, no, p["annee"], session, publier)
            sauver("deliberations", fusionner(charger("deliberations"), snap, CLES["deliberations"]))
            if publier and not p["deliberation_publiee"]:
                p["deliberation_publiee"] = True
                sauver_params(p)
            st.success(f"Délibération « {session} » {p['annee']} "
                       f"{'publiée' if publier else 'enregistrée (non visible)'} pour {len(snap)} étudiants en ordre.")
    arch = charger("deliberations")
    dans = (arch["promotion"] == promo) & meme_faculte(arch["faculte"], fac)
    if dans.any():
        st.markdown("---")
        st.markdown("**Délibérations déjà enregistrées pour cette promotion**")
        groupes = (arch[dans].groupby(["annee", "session"])
                   .agg(Date=("date", "max"), Etudiants=("matricule", "count"),
                        Visible=("publie", lambda v: "Oui" if (v == "1").all() else
                                 "Non" if (v == "0").all() else "En partie"))
                   .reset_index().rename(columns={"annee": "Année", "session": "Session"}))
        st.dataframe(groupes, hide_index=True)
        choix_g = st.selectbox("Gérer la visibilité de",
                               [f"{r['Année']} · {r['Session']}" for _, r in groupes.iterrows()],
                               key="delib_gerer")
        ga, gs = choix_g.split(" · ", 1)
        c1, c2 = st.columns(2)
        for colonne, libelle, valeur in ((c1, "Rendre visible", "1"), (c2, "Masquer", "0")):
            if colonne.button(libelle, key=f"vis_{valeur}"):
                arch.loc[dans & (arch["annee"] == ga) & (arch["session"] == gs), "publie"] = valeur
                sauver("deliberations", arch)
                st.success("Visibilité mise à jour.")
                st.rerun()


def page_parametres():
    st.markdown("**Sauvegarde des données (à télécharger régulièrement)**")
    for nom_table in FICHIERS:
        st.download_button(f"Télécharger {nom_table}.csv", charger(nom_table).to_csv(index=False),
                           f"{nom_table}.csv", "text/csv", key=f"sauv_{nom_table}")


def espace_admin():
    migrer_cours_sans_code()
    page = st.session_state.get("page", "accueil")
    if page not in [c for c, _ in PAGES_ADMIN]:
        page = "accueil"
    if page == "accueil":
        entete_page("Bienvenue", f"Bienvenue sur la plateforme {NOM}",
                    "Utilisez le menu en haut à droite pour gérer les universités, les facultés, les étudiants, "
                    "les cours, les notes, les paiements et les délibérations.")
        return
    if st.button("← Accueil", key="retour_accueil"):
        st.session_state["page"] = "accueil"
        st.rerun()
    st.subheader("Espace administrateur")
    if base_en_ligne():
        st.caption("Stockage : base de données en ligne (données permanentes).")
    else:
        st.warning("Stockage local : sur Streamlit Cloud, ces données sont effacées à chaque "
                   "redémarrage. Branchez la base Supabase (secrets SUPABASE_URL et SUPABASE_KEY).")
    st.markdown(f'<div class="sect">{esc(dict(PAGES_ADMIN)[page])}</div>', unsafe_allow_html=True)
    {"parametres": page_parametres, "universites": page_universites, "facultes": page_facultes, "etudiants": page_etudiants,
     "cours": page_cours, "notes": page_notes, "paiements": page_paiements,
     "deliberation": page_deliberation}[page]()


# ---------- Application ----------
try:
    from PIL import Image as _Img
    _icone = _Img.open(trouver_logo()) if trouver_logo() else None
except Exception:
    _icone = None
st.set_page_config(page_title=NOM, page_icon=_icone, initial_sidebar_state="collapsed")
st.markdown(STYLE, unsafe_allow_html=True)

if st.session_state.get("admin"):
    barre_haut(PAGES_ADMIN, connecte=True)
    with conteneur("contenu_admin"):
        espace_admin()
elif st.session_state.get("etu"):
    espace_etudiant(st.session_state["etu"])
else:
    page_connexion()
