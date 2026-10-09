import base64
import hmac
from html import escape as esc
import io
import json
import os
import re
from datetime import date

import pandas as pd
import streamlit as st

NOM = "Jns Académie"
PAYS = "RÉPUBLIQUE DÉMOCRATIQUE DU CONGO"
UNIVERSITE = "UNIVERSITÉ DE KOLWEZI"
FACULTE = "Faculté des Sciences : GEOLOGIE"   # faculté par défaut (si non précisée pour l'étudiant)
MOT_DE_PASSE_PAR_DEFAUT = "admin123"   # utilisé seulement si aucun secret n'est défini


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
    "etudiants": ["matricule", "nom", "promotion", "code", "frais_dus", "frais_payes", "faculte"],
    "cours": ["code_cours", "intitule", "promotion", "heures", "credits"],
    "notes": ["matricule", "code_cours", "moyenne", "examen"],  # moyenne /10 + examen /10
    "profils": ["matricule", "sexe", "date_naissance", "lieu_naissance",
                "nationalite", "adresse", "telephone"],
}
CLES = {"etudiants": ["matricule"], "cours": ["code_cours", "promotion"],
        "notes": ["matricule", "code_cours"], "profils": ["matricule"]}
DOSSIER_APP = os.path.dirname(os.path.abspath(__file__))
PARAMS = "parametres.json"
DOSSIER_PHOTOS = "photos"
os.makedirs(DOSSIER_PHOTOS, exist_ok=True)

TOTAL_CREDITS = 60   # crédits du programme d'une promotion
SEUIL_CREDITS = 45   # minimum de crédits validés pour passer



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
    return df


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


def data_uri(chemin):
    if not chemin or not os.path.exists(chemin):
        return ""
    mime = "image/png" if chemin.lower().endswith(".png") else "image/jpeg"
    with open(chemin, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def carte_html(e, profil, annee, photo):
    """Carte d'étudiant colorée affichée dans l'application."""
    from html import escape as esc
    logo, ph = data_uri(trouver_logo()), data_uri(photo)
    img_logo = (f'<img src="{logo}" style="height:46px;width:auto;">' if logo else "")
    img_photo = (f'<img src="{ph}" style="width:100%;height:100%;object-fit:cover;">' if ph
                 else '<div style="color:#64748b;font-size:12px;text-align:center;'
                      'padding-top:55px;">PHOTO</div>')

    def ligne(libelle, valeur):
        return (f'<div><span style="color:#f2b705;font-weight:700;">{libelle} :</span> '
                f'{esc(str(valeur) if str(valeur).strip() else "-")}</div>')

    naissance = f"{profil['date_naissance'] or '-'} à {profil['lieu_naissance'] or '-'}"
    morceaux = [
        '<div style="max-width:440px;border-radius:16px;overflow:hidden;'
        'box-shadow:0 4px 16px rgba(0,0,0,.3);font-family:Arial,sans-serif;color:#fff;'
        'background:linear-gradient(160deg,#1f3a5f 0%,#0f766e 100%);">',
        '<div style="background:#fff;padding:10px 14px;display:flex;align-items:center;'
        'gap:12px;border-bottom:4px solid #f2b705;">', img_logo,
        '<div><div style="color:#1f3a5f;font-weight:700;font-size:15px;">'
        f'{esc(UNIVERSITE)}</div>'
        '<div style="color:#b45309;font-weight:700;font-size:11px;letter-spacing:2px;">'
        "CARTE D'ÉTUDIANT</div></div></div>",
        '<div style="display:flex;gap:16px;padding:16px;">',
        '<div style="width:100px;height:128px;flex-shrink:0;border:3px solid #f2b705;'
        f'border-radius:8px;background:#f1f5f9;overflow:hidden;">{img_photo}</div>',
        '<div style="flex:1;font-size:13px;line-height:1.8;">',
        f'<div style="font-size:17px;font-weight:700;margin-bottom:6px;">{esc(str(e["nom"]).upper())}</div>',
        ligne("Matricule", e["matricule"]), ligne("Promotion", e["promotion"]),
        ligne("Né(e) le", naissance), ligne("Nationalité", profil["nationalite"]),
        "</div></div>",
        '<div style="background:#f2b705;color:#1f3a5f;padding:7px 14px;font-size:12px;'
        'font-weight:700;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;">'
        f'<span>{esc(faculte_de(e))}</span><span>Année académique {esc(str(annee))}</span></div>',
        "</div>",
    ]
    return "".join(morceaux)


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


def bilan(mat, promo, co, no):
    """Cours de la promotion + note de l'étudiant, moyenne pondérée, crédits."""
    d = co[co["promotion"] == promo].merge(no[no["matricule"] == mat], on="code_cours", how="left")
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
    return "Excellent" if acquis >= SEUIL_CREDITS else "Insuffisant"


def decision(acquis, d):
    if not complet(d):
        return "Non disponible"
    return "ADMIS" if acquis >= SEUIL_CREDITS else "DÉFAILLANT"


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


# ---------- PDF : bulletin et carte ----------
def bulletin_pdf(e, profil, d, moyenne, acquis, total_cr, annee, photo=None, numero=""):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def x(v):
        return escape(str(v)) if str(v).strip() else "-"

    s = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=40, rightMargin=40,
                            title=f"Bulletin {e['matricule']}")
    centre = ParagraphStyle("centre", parent=s["Normal"], alignment=TA_CENTER,
                            fontName="Helvetica-Bold", fontSize=11, leading=15)
    titre = ParagraphStyle("titre", parent=centre, fontSize=14, leading=20)
    texte = ParagraphStyle("texte", parent=s["Normal"], alignment=TA_JUSTIFY,
                           fontSize=10.5, leading=15)

    fac = faculte_de(e)
    civilite = {"Masculin": "Monsieur", "Féminin": "Madame"}.get(profil["sexe"], "Monsieur/Madame")
    el = [Paragraph(escape(PAYS), centre),
          Paragraph(escape(UNIVERSITE), centre),
          Paragraph(escape(faculte_entete(fac)), centre),
          Spacer(1, 10),
          Paragraph(f"BULLETIN DE COTES N° {x(numero) if numero else ''}", titre),
          Spacer(1, 10),
          Paragraph(
              f"{civilite} <b>{x(str(e['nom']).upper())}</b>, matricule <b>{x(e['matricule'])}</b>, "
              f"a obtenu à l'issue de l'année académique <b>{x(annee)}</b> les cotes "
              f"ci-dessous reprises aux examens portant sur les matières prévues au programme "
              f"de <b>{x(e['promotion'])}</b> {escape(a_la_faculte(fac))}.", texte),
          Spacer(1, 12)]

    logo = trouver_logo()
    if logo:
        from reportlab.lib.utils import ImageReader
        larg, haut = ImageReader(logo).getSize()
        img = Image(logo, width=75 * larg / haut, height=75)
        img.hAlign = "CENTER"
        el[0:0] = [img, Spacer(1, 6)]

    ident = Paragraph(
        f"<b>Sexe :</b> {x(profil['sexe'])}<br/>"
        f"<b>Né(e) le :</b> {x(profil['date_naissance'])} à {x(profil['lieu_naissance'])}<br/>"
        f"<b>Nationalité :</b> {x(profil['nationalite'])}<br/>"
        f"<b>Adresse :</b> {x(profil['adresse'])}<br/>"
        f"<b>Téléphone :</b> {x(profil['telephone'])}", s["Normal"])
    if photo and os.path.exists(photo):
        bloc = Table([[ident, Image(photo, width=80, height=100)]], colWidths=[400, 95])
        bloc.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        el.append(bloc)
    else:
        el.append(ident)
    el.append(Spacer(1, 14))

    def fmt(v):
        return "-" if pd.isna(v) else f"{v:.2f}"

    couleurs = []
    lignes = [["Code", "Matière suivie", "Crédits", "Moy. /10", "Exam. /10", "Cote /20", "Total"]]
    for i, (_, r) in enumerate(d.iterrows(), start=1):
        if not pd.isna(r["note"]) and r["note"] < 10:
            couleurs.append(("TEXTCOLOR", (5, i), (5, i), colors.red))
        elif not pd.isna(r["note"]) and r["note"] == 10:
            couleurs.append(("TEXTCOLOR", (5, i), (5, i), colors.blue))
        lignes.append([escape(r["code_cours"]), Paragraph(escape(r["intitule"]), s["Normal"]),
                       f"{r['credits']:g}", fmt(r["moyenne"]), fmt(r["examen"]),
                       fmt(r["note"]), fmt(r["total"])])
    lignes.append(["", "TOTAL", f"{total_cr:g}", "", "", "", f"{d['total'].sum():.2f}"])
    tableau = Table(lignes, colWidths=[50, 150, 45, 52, 52, 52, 60], repeatRows=1)
    tableau.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("BACKGROUND", (0, -1), (-1, -1), colors.whitesmoke),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
    ]))
    if couleurs:
        tableau.setStyle(TableStyle(couleurs))
    el += [tableau, Spacer(1, 16)]

    dec = "Non disponible"
    if complet(d):
        dec = "ADMIS" if acquis >= SEUIL_CREDITS else "DÉFAILLANT"
    resume = [["Moyenne annuelle", "-" if moyenne is None else f"{moyenne:.2f} / 20"],
              ["Mention", mention(acquis, d) if complet(d) else "Non disponible"],
              ["Décision", dec],
              ["Pourcentage", "-" if moyenne is None else f"{moyenne / 20 * 100:.1f} %"],
              ["Crédits validés sur 60", f"{acquis:g} / {TOTAL_CREDITS}"]]
    res = Table(resume, colWidths=[200, 200])
    res.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    el += [res, Spacer(1, 30),
           Paragraph(f"Document généré par {escape(NOM)} le {date.today().strftime('%d/%m/%Y')}",
                     s["Italic"])]
    doc.build(el)
    return buf.getvalue()


def carte_pdf(e, profil, annee, photo=None):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    larg, haut = 85.6 * mm, 54 * mm
    marine = colors.HexColor("#1f3a5f")
    sarcelle = colors.HexColor("#0f766e")
    dore = colors.HexColor("#f2b705")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(larg, haut))

    # Fond dégradé marine -> sarcelle
    n = 60
    for i in range(n):
        t = i / (n - 1)
        c.setFillColorRGB(marine.red * (1 - t) + sarcelle.red * t,
                          marine.green * (1 - t) + sarcelle.green * t,
                          marine.blue * (1 - t) + sarcelle.blue * t)
        c.rect(0, haut * (1 - (i + 1) / n), larg, haut / n + 0.3, fill=1, stroke=0)

    # Bandeau blanc d'en-tête avec logo, filet doré
    c.setFillColor(colors.white)
    c.rect(0, haut - 12 * mm, larg, 12 * mm, fill=1, stroke=0)
    c.setFillColor(dore)
    c.rect(0, haut - 13.2 * mm, larg, 1.2 * mm, fill=1, stroke=0)
    x_texte = 4 * mm
    logo = trouver_logo()
    if logo:
        c.drawImage(logo, 3 * mm, haut - 11 * mm, width=10 * mm, height=10 * mm,
                    preserveAspectRatio=True, anchor="c", mask="auto")
        x_texte = 15 * mm
    c.setFillColor(marine)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(x_texte, haut - 6 * mm, UNIVERSITE)
    c.setFillColor(colors.HexColor("#b45309"))
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(x_texte, haut - 10 * mm, "CARTE D'ÉTUDIANT")

    # Photo avec cadre doré
    c.setFillColor(dore)
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

    # Identité
    nom = str(e["nom"]).upper()
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9 if len(nom) <= 24 else 7.5)
    c.drawString(30 * mm, 35.5 * mm, nom[:34])
    naissance = f"{profil['date_naissance'] or '-'}"
    infos = [("Matricule : ", e["matricule"]), ("Promotion : ", e["promotion"]),
             ("Né(e) le : ", naissance), ("Nationalité : ", profil["nationalite"] or "-")]
    y = 30.5 * mm
    for libelle, valeur in infos:
        c.setFillColor(dore)
        c.setFont("Helvetica-Bold", 6.8)
        c.drawString(30 * mm, y, libelle)
        decal = c.stringWidth(libelle, "Helvetica-Bold", 6.8)
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 7.2)
        c.drawString(30 * mm + decal, y, str(valeur)[:28])
        y -= 4.6 * mm

    # Pied de carte doré
    c.setFillColor(dore)
    c.rect(0, 0, larg, 6 * mm, fill=1, stroke=0)
    c.setFillColor(marine)
    c.setFont("Helvetica-Bold", 6.3)
    c.drawString(4 * mm, 2.2 * mm, f"Année {annee}")
    c.drawRightString(larg - 4 * mm, 2.2 * mm, faculte_de(e)[:42])
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
.st-key-delib_box { background:#2b2f36; padding:1.4rem 1.4rem 1.6rem 1.4rem; margin-top:1rem; }
.delib-t { color:#b4b9c2; letter-spacing:.14em; font-weight:700; font-size:.9rem; margin-bottom:1rem; }
.delib-g { display:grid; grid-template-columns:1fr 1fr; gap:1.1rem 1rem; }
.delib-g .l { color:#aab0ba; text-transform:uppercase; font-size:.8rem; letter-spacing:.06em; }
.delib-g .v { color:#fff; font-family:'Fraunces',Georgia,serif; font-size:1.7rem; font-weight:700; }
.delib-g .v.m { font-size:1.25rem; }
.st-key-dl_bulletin button { background:#b4b9c2 !important; color:#2b2f36 !important; border:0 !important;
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


def barre_haut(pages=None, connecte=False):
    """Barre marine en haut ; le menu (bouton à droite) apparaît seulement une fois connecté."""
    logo = data_uri(trouver_logo())
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


# ---------- Espace étudiant ----------
PAGES_ETUDIANT = [("tableau", "Tableau de bord"), ("cours", "Mes cours"), ("notes", "Mes notes"),
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
            if lire_mot_de_passe() == MOT_DE_PASSE_PAR_DEFAUT:
                st.warning("Le mot de passe par défaut est encore utilisé. "
                           "Définissez le secret MOT_DE_PASSE.")
            if lire_cle_admin() == CLE_ADMIN_PAR_DEFAUT:
                st.warning("La clé d'accès par défaut est encore utilisée. "
                           "Définissez le secret CLE_ADMIN.")
            with st.form("f_admin"):
                mdp = st.text_input("Mot de passe administrateur", type="password")
                ok_admin = st.form_submit_button("Accéder", type="primary")
            if ok_admin:
                if hmac.compare_digest(mdp.encode(), lire_mot_de_passe().encode()):
                    st.session_state["admin"] = True
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
    dus, payes = nombre(e["frais_dus"]), nombre(e["frais_payes"])
    en_ordre = payes >= dus
    d, moyenne, acquis, total_cr = bilan(e["matricule"], e["promotion"], co, no)
    photo = chemin_photo(e["matricule"])
    profil = profil_de(e["matricule"])
    fac = faculte_de(e)
    # Examen et total /20 visibles seulement après délibération (et frais en ordre si blocage actif)
    visible = p["deliberation_publiee"] and (en_ordre or not p["bloquer_impaye"])

    barre_haut(PAGES_ETUDIANT, connecte=True)
    page = st.session_state.get("page", "tableau")

    # ----- Tableau de bord
    if page == "tableau":
        entete_page("Accueil", f"Bonjour, {e['nom']}", f"{e['promotion']} · {fac}")
        with conteneur("contenu"):
            reste = max(dus - payes, 0)
            st.markdown(tuiles_html([
                ("Matricule", e["matricule"]), ("Promotion", e["promotion"]),
                ("Année académique", p["annee"]), ("Nombre de cours", str(len(d))),
                ("Crédits du programme", f"{total_cr:g}"),
                ("Frais", "En ordre" if en_ordre else f"Reste {reste:g}"),
                ("Délibération", "Publiée" if p["deliberation_publiee"] else "Pas encore publiée"),
                ("Résultats visibles", "Oui" if visible else "Pas encore"),
            ]), unsafe_allow_html=True)
            champs = ["sexe", "date_naissance", "lieu_naissance", "nationalite", "adresse"]
            if not all(profil[c] for c in champs) or not os.path.exists(photo):
                st.warning("Complétez votre profil et ajoutez votre photo (menu, Mon profil) "
                           "pour une carte et un bulletin complets.")

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
                    st.warning("Cote d'examen et total bloqués : régularisez d'abord vos frais.")
            else:
                lignes = [[esc(r["intitule"]), f"{r['credits']:g}", num(r["moyenne"]),
                           num(r["examen"]), cote_html(r["note"])] for _, r in d.iterrows()]
                st.markdown(tableau_html(["Cours", "Crédits", "Moyenne /10", "Examen /10",
                                          "Total /20"], lignes), unsafe_allow_html=True)
                st.markdown('<div class="legende"><b style="color:#c62828">Rouge</b> : moins de '
                            '10/20, non validé. <b style="color:#1d4ed8">Bleu</b> : 10/20, validé. '
                            "<b>Noir</b> : plus de 10/20, validé.</div>", unsafe_allow_html=True)
                dec = decision(acquis, d)
                couleur_dec = {"ADMIS": "#86efac", "DÉFAILLANT": "#fca5a5"}.get(dec, "#b4b9c2")
                with conteneur("delib_box"):
                    st.markdown(
                        '<div class="delib-t">RÉSULTATS DE DÉLIBÉRATION</div><div class="delib-g">'
                        '<div><div class="l">Moyenne</div><div class="v">'
                        f'{"-" if moyenne is None else f"{moyenne:.2f}"}/20</div></div>'
                        f'<div><div class="l">Crédits</div><div class="v">{acquis:g}/{TOTAL_CREDITS}</div></div>'
                        f'<div><div class="l">Mention</div><div class="v m">{esc(mention(acquis, d))}</div></div>'
                        f'<div><div class="l">Décision</div><div class="v m" style="color:{couleur_dec}">'
                        f'{esc(dec.upper())}</div></div></div>', unsafe_allow_html=True)
                    if not complet(d):
                        st.caption("Certaines cotes ne sont pas encore disponibles : "
                                   "la décision finale n'est pas encore calculée.")
                    try:
                        pdf = bulletin_pdf(e, profil, d, moyenne, acquis, total_cr, p["annee"],
                                           photo, f"{ligne.index[0] + 1:03d}")
                        st.download_button("Télécharger mon bulletin", pdf,
                                           f"bulletin_{e['matricule']}.pdf", "application/pdf",
                                           key="dl_bulletin")
                    except ImportError:
                        st.info("Bulletin PDF indisponible : installez reportlab "
                                "(py -m pip install reportlab).")

    # ----- Mes paiements
    elif page == "paiements":
        entete_page("Finances", "Mes paiements", f"{e['promotion']}. Les frais sont enregistrés "
                    "par la comptabilité de l'université.")
        with conteneur("contenu"):
            reste = max(dus - payes, 0)
            st.markdown('<div class="sect">Frais à payer</div>', unsafe_allow_html=True)
            if dus <= 0:
                st.markdown('<div class="vide">Aucun frais applicable pour le moment.</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(tuiles_html([("Frais dus", f"{dus:g}"), ("Déjà payé", f"{payes:g}"),
                                         ("Reste à payer", f"{reste:g}"),
                                         ("Situation", "En ordre" if en_ordre else "Pas en ordre")]),
                            unsafe_allow_html=True)
                if not en_ordre:
                    st.warning("Vous n'êtes pas en ordre. Passez régulariser à la comptabilité.")
            st.markdown('<div class="sect">Historique</div>', unsafe_allow_html=True)
            if payes > 0:
                st.markdown(tableau_html(["Paiement", "Montant"], [["Total payé", f"{payes:g}"]]),
                            unsafe_allow_html=True)
            else:
                st.markdown('<div class="vide">Aucun paiement effectué.</div>', unsafe_allow_html=True)

    # ----- Mon profil
    else:
        entete_page("Compte", "Mon profil", f"{e['nom']} · {e['matricule']}")
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
            st.markdown(carte_html(e, profil, p["annee"], photo), unsafe_allow_html=True)
            try:
                st.download_button("Télécharger ma carte (PDF)",
                                   carte_pdf(e, profil, p["annee"], photo),
                                   f"carte_{e['matricule']}.pdf", "application/pdf", key="dl_carte")
            except ImportError:
                st.info("PDF indisponible : installez reportlab (py -m pip install reportlab).")


# ---------- Espace administrateur ----------
def import_csv(nom, defauts=None):
    f = st.file_uploader(f"Importer un CSV ({', '.join(FICHIERS[nom])})",
                         type="csv", key=f"up_{nom}")
    if f is None:
        return
    new = pd.read_csv(f, dtype=str).fillna("")
    for c, v in (defauts or {}).items():
        if c not in new.columns:
            new[c] = v
    manque = set(FICHIERS[nom]) - set(new.columns)
    if manque:
        st.error("Colonnes manquantes : " + ", ".join(manque))
        return
    new = new[FICHIERS[nom]]
    if nom == "notes":
        for c in ("moyenne", "examen"):
            v = pd.to_numeric(new[c], errors="coerce")
            if ((v > 10) | (v < 0)).any():
                st.error(f"La colonne « {c} » doit contenir des valeurs entre 0 et 10.")
                return
    st.dataframe(new)
    if st.button("Enregistrer l'import", key=f"btn_{nom}"):
        sauver(nom, fusionner(charger(nom), new, CLES[nom]))
        st.success(f"{len(new)} lignes enregistrées.")


def espace_admin():
    st.subheader("Espace administrateur")
    if base_en_ligne():
        st.caption("Stockage : base de données en ligne (données permanentes).")
    else:
        st.warning("Stockage local : sur Streamlit Cloud, ces données sont effacées à chaque "
                   "redémarrage. Branchez la base Supabase (secrets SUPABASE_URL et SUPABASE_KEY).")
    with st.expander("Sauvegarde des données (à télécharger régulièrement)"):
        for nom_table in FICHIERS:
            st.download_button(f"Télécharger {nom_table}.csv", charger(nom_table).to_csv(index=False),
                               f"{nom_table}.csv", "text/csv", key=f"sauv_{nom_table}")

    with st.expander("Logo de l'université", expanded=not trouver_logo()):
        if trouver_logo():
            st.image(trouver_logo(), width=120)
        else:
            st.info("Aucun logo pour le moment.")
        logo_up = st.file_uploader("Choisir le logo (PNG ou JPG)", type=["png", "jpg", "jpeg"],
                                   key="up_logo")
        if logo_up is not None and st.button("Enregistrer le logo"):
            try:
                sauver_logo(logo_up)
                st.success("Logo enregistré. Il apparaît sur les bulletins, les cartes et l'appli.")
                st.image(trouver_logo(), width=120)
            except Exception:
                st.error("Image illisible. Essayez un autre fichier (PNG ou JPG).")
        if trouver_logo() and st.button("Supprimer le logo"):
            supprimer_logo()
            st.success("Logo supprimé.")

    tabs = st.tabs(["Étudiants", "Cours", "Notes", "Paiements", "Délibération"])

    with tabs[0]:
        with st.form("f_et", clear_on_submit=True):
            m = st.text_input("Matricule")
            n = st.text_input("Nom complet")
            pr = st.text_input("Promotion (ex. L1 Géologie)")
            cd = st.text_input("Code personnel (donné à l'étudiant)")
            fd = st.number_input("Frais dus", 0.0, step=10.0)
            fa = st.text_input("Faculté (ex. Faculté d'Économie). Vide = faculté par défaut")
            envoye = st.form_submit_button("Ajouter")
            if envoye and not (m and n and pr and cd):
                st.error("Remplissez tous les champs (matricule, nom, promotion, code).")
            if envoye and m and n and pr and cd:
                ligne = pd.DataFrame([[m.strip(), n.strip(), pr.strip(), cd.strip(), str(fd), "0", fa.strip()]],
                                     columns=FICHIERS["etudiants"])
                sauver("etudiants", fusionner(charger("etudiants"), ligne, CLES["etudiants"]))
                st.success("Étudiant ajouté.")
        import_csv("etudiants", {"frais_payes": "0", "faculte": ""})
        with st.expander("Définir la faculté d'une promotion"):
            tous_et = charger("etudiants")
            if tous_et.empty:
                st.info("Ajoutez d'abord des étudiants.")
            else:
                promo_choisie = st.selectbox("Promotion", sorted(tous_et["promotion"].unique()),
                                             key="fac_promo")
                nouvelle = st.text_input("Faculté de cette promotion (ex. Faculté d'Économie)",
                                         key="fac_txt")
                if st.button("Appliquer la faculté") and nouvelle.strip():
                    tous_et.loc[tous_et["promotion"] == promo_choisie, "faculte"] = nouvelle.strip()
                    sauver("etudiants", tous_et)
                    st.success(f"Faculté enregistrée pour {promo_choisie}.")
        st.dataframe(charger("etudiants"), hide_index=True)
        with st.expander("Profils remplis par les étudiants"):
            st.dataframe(charger("profils"), hide_index=True)

    with tabs[1]:
        with st.form("f_co", clear_on_submit=True):
            cc = st.text_input("Code du cours")
            it = st.text_input("Intitulé")
            pr = st.text_input("Promotion")
            h = st.number_input("Heures", 0, step=5)
            cr = st.number_input("Crédits (ex. 3)", 0, step=1, value=3)
            envoye = st.form_submit_button("Ajouter")
            if envoye and not (cc and it and pr):
                st.error("Remplissez tous les champs (code, intitulé, promotion).")
            if envoye and cc and it and pr:
                ligne = pd.DataFrame([[cc.strip(), it.strip(), pr.strip(), str(h), str(cr)]],
                                     columns=FICHIERS["cours"])
                sauver("cours", fusionner(charger("cours"), ligne, CLES["cours"]))
                st.success("Cours ajouté.")
        import_csv("cours")
        tous = charger("cours")
        if not tous.empty:
            tous["_cr"] = pd.to_numeric(tous["credits"], errors="coerce").fillna(0)
            for promo, t in tous.groupby("promotion")["_cr"].sum().items():
                if t != TOTAL_CREDITS:
                    st.warning(f"{promo} : {t:g} crédits au total. "
                               f"Le programme doit totaliser {TOTAL_CREDITS} crédits.")
        st.dataframe(charger("cours"), hide_index=True)

    with tabs[2]:
        with st.form("f_no", clear_on_submit=True):
            m = st.text_input("Matricule de l'étudiant")
            cc = st.text_input("Code du cours")
            mo = st.number_input("Moyenne /10 (interrogations, TP, TD...)", 0.0, 10.0, 0.0, 0.25)
            ex = st.number_input("Examen /10", 0.0, 10.0, 0.0, 0.25)
            avec_ex = st.checkbox("Enregistrer aussi la cote d'examen "
                                  "(décocher si l'examen n'est pas encore passé)", value=True)
            envoye = st.form_submit_button("Enregistrer")
            if envoye and not (m and cc):
                st.error("Remplissez le matricule et le code du cours.")
            if envoye and m and cc:
                n = charger("notes")
                ancien = n[(n["matricule"] == m.strip()) & (n["code_cours"] == cc.strip())]
                if avec_ex:
                    ex_val = str(ex)
                else:
                    ex_val = ancien.iloc[0]["examen"] if not ancien.empty else ""
                ligne = pd.DataFrame([[m.strip(), cc.strip(), str(mo), ex_val]],
                                     columns=FICHIERS["notes"])
                sauver("notes", fusionner(n, ligne, CLES["notes"]))
                st.success("Note enregistrée (total /20 = moyenne + examen).")
        import_csv("notes")
        st.dataframe(charger("notes"), hide_index=True)

    with tabs[3]:
        et = charger("etudiants")
        if et.empty:
            st.info("Ajoutez d'abord des étudiants.")
        else:
            choix = st.selectbox("Étudiant", et["matricule"] + " — " + et["nom"])
            montant = st.number_input("Montant payé", 0.0, step=10.0)
            if st.button("Enregistrer le paiement") and montant > 0:
                i = et.index[et["matricule"] == choix.split(" — ")[0]][0]
                et.loc[i, "frais_payes"] = str(nombre(et.loc[i, "frais_payes"]) + montant)
                sauver("etudiants", et)
                st.success("Paiement enregistré.")
            et = charger("etudiants")
            et["en_ordre"] = et.apply(
                lambda r: "Oui" if nombre(r["frais_payes"]) >= nombre(r["frais_dus"]) else "Non",
                axis=1)
            st.dataframe(et.drop(columns=["code"]), hide_index=True)

    with tabs[4]:
        p = params()
        p["annee"] = st.text_input("Année académique", p["annee"])
        p["bloquer_impaye"] = st.checkbox("Bloquer les résultats si frais impayés",
                                          p["bloquer_impaye"])
        p["deliberation_publiee"] = st.toggle("Publier les résultats de la délibération",
                                              p["deliberation_publiee"])
        if st.button("Appliquer"):
            sauver_params(p)
            st.success("Paramètres enregistrés.")

        et, co, no = charger("etudiants"), charger("cours"), charger("notes")
        lignes = []
        for _, e in et.iterrows():
            dd, moy, acq, tot = bilan(e["matricule"], e["promotion"], co, no)
            lignes.append({"Matricule": e["matricule"], "Nom": e["nom"],
                           "Promotion": e["promotion"],
                           "Moyenne": None if moy is None else round(moy, 2),
                           "Crédits validés": f"{acq:g}/{TOTAL_CREDITS}",
                           "Mention": mention(acq, dd), "Décision": decision(acq, dd)})
        if lignes:
            rapport = pd.DataFrame(lignes)
            st.dataframe(rapport, hide_index=True)
            st.download_button("Télécharger le PV (CSV)", rapport.to_csv(index=False),
                               "deliberation.csv", "text/csv")


# ---------- Application ----------
try:
    from PIL import Image as _Img
    _icone = _Img.open(trouver_logo()) if trouver_logo() else None
except Exception:
    _icone = None
st.set_page_config(page_title=NOM, page_icon=_icone, initial_sidebar_state="collapsed")
st.markdown(STYLE, unsafe_allow_html=True)

if st.session_state.get("admin"):
    barre_haut([], connecte=True)
    with conteneur("contenu_admin"):
        espace_admin()
elif st.session_state.get("etu"):
    espace_etudiant(st.session_state["etu"])
else:
    page_connexion()
