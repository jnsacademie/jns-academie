import base64
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



# ---------- Données ----------
def charger(nom):
    f = f"{nom}.csv"
    if os.path.exists(f):
        df = pd.read_csv(f, dtype=str).fillna("")
        for c in FICHIERS[nom]:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=FICHIERS[nom])


def sauver(nom, df):
    df.to_csv(f"{nom}.csv", index=False)


def fusionner(df, nouveau, cles):
    return pd.concat([df, nouveau], ignore_index=True).drop_duplicates(subset=cles, keep="last")


def params():
    base = {"deliberation_publiee": False, "bloquer_impaye": True, "annee": "2025-2026"}
    if os.path.exists(PARAMS):
        with open(PARAMS, encoding="utf-8") as f:
            base.update(json.load(f))
    return base


def sauver_params(p):
    with open(PARAMS, "w", encoding="utf-8") as f:
        json.dump(p, f)


def trouver_logo():
    """Logo de l'université : fichier logo.png / logo.jpg / logo.jpeg à côté de ce programme."""
    for nom in ("logo.png", "logo.jpg", "logo.jpeg"):
        chemin = os.path.join(DOSSIER_APP, nom)
        if os.path.exists(chemin):
            return chemin
    return None


def sauver_logo(fichier):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(fichier)).convert("RGBA")
    img.thumbnail((800, 800))
    supprimer_logo()
    img.save(os.path.join(DOSSIER_APP, "logo.png"), "PNG")


def supprimer_logo():
    for nom in ("logo.png", "logo.jpg", "logo.jpeg"):
        chemin = os.path.join(DOSSIER_APP, nom)
        if os.path.exists(chemin):
            os.remove(chemin)


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


def chemin_photo(mat):
    return os.path.join(DOSSIER_PHOTOS, re.sub(r"[^\w\-]", "_", str(mat)) + ".jpg")


def sauver_photo(mat, fichier):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(fichier)).convert("RGB")
    img.thumbnail((480, 600))
    img.save(chemin_photo(mat), "JPEG", quality=88)


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


# ---------- Espace étudiant ----------
def espace_etudiant():
    et, co, no, p = charger("etudiants"), charger("cours"), charger("notes"), params()
    st.subheader("Connexion étudiant")
    mat = st.text_input("Matricule").strip()
    code = st.text_input("Code personnel", type="password").strip()
    if not (mat and code):
        st.info("Entrez votre matricule et votre code personnel.")
        return
    ligne = et[(et["matricule"].str.lower() == mat.lower()) & (et["code"] == code)]
    if ligne.empty:
        st.error("Matricule ou code incorrect.")
        return

    e = ligne.iloc[0]
    st.success(f"Bienvenue {e['nom']} — {e['promotion']}")
    dus, payes = nombre(e["frais_dus"]), nombre(e["frais_payes"])
    en_ordre = payes >= dus
    d, moyenne, acquis, total_cr = bilan(e["matricule"], e["promotion"], co, no)
    photo = chemin_photo(e["matricule"])
    # Examen et total /20 visibles seulement après délibération (et si frais en ordre quand bloqué)
    visible = p["deliberation_publiee"] and (en_ordre or not p["bloquer_impaye"])

    t0, t1, t2, t3, t4 = st.tabs(["Profil et carte", "Frais", "Mes cours",
                                  "Notes", "Délibération"])

    with t0:
        profil = profil_de(e["matricule"])
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
                envoye = st.form_submit_button("Enregistrer mon profil")
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
                st.success("Profil enregistré.")

        profil = profil_de(e["matricule"])
        if not all(profil[c] for c in champs) or not os.path.exists(photo):
            st.warning("Complétez votre profil et ajoutez votre photo pour une carte "
                       "et un bulletin complets.")

        st.markdown("#### Carte d'étudiant")
        st.markdown(carte_html(e, profil, p["annee"], photo), unsafe_allow_html=True)
        try:
            st.download_button("Télécharger ma carte (PDF)",
                               carte_pdf(e, profil, p["annee"], photo),
                               f"carte_{e['matricule']}.pdf", "application/pdf")
        except ImportError:
            st.info("PDF indisponible : installez reportlab (py -m pip install reportlab).")

    with t1:
        c1, c2, c3 = st.columns(3)
        c1.metric("Frais dus", f"{dus:g}")
        c2.metric("Payé", f"{payes:g}")
        c3.metric("Reste", f"{max(dus - payes, 0):g}")
        if en_ordre:
            st.success("Vous êtes EN ORDRE avec les frais")
        else:
            st.error("Vous n'êtes PAS en ordre. Passez régulariser à la comptabilité.")

    with t2:
        if d.empty:
            st.info("Aucun cours enregistré pour votre promotion.")
        else:
            st.dataframe(d[["code_cours", "intitule", "heures", "credits"]].rename(columns={
                "code_cours": "Code", "intitule": "Cours", "heures": "Heures",
                "credits": "Crédits"}), hide_index=True)
            st.write(f"**Total : {d['heures'].sum():g} heures — {total_cr:g} crédits**")

    with t3:
        if d.empty:
            st.info("Aucun cours.")
        elif not visible:
            st.dataframe(d[["intitule", "credits", "moyenne"]].rename(columns={
                "intitule": "Cours", "credits": "Crédits", "moyenne": "Moyenne /10"}),
                hide_index=True)
            if not p["deliberation_publiee"]:
                st.info("Délibération pas encore faite : seule votre moyenne /10 "
                        "(interrogations, TP, TD...) est visible. La cote d'examen et le "
                        "total /20 apparaîtront après la délibération.")
            else:
                st.warning("Cote d'examen et total bloqués : régularisez d'abord vos frais.")
        else:
            aff = d[["intitule", "credits", "moyenne", "examen", "note", "statut"]].rename(
                columns={"intitule": "Cours", "credits": "Crédits", "moyenne": "Moyenne /10",
                         "examen": "Examen /10", "note": "Total /20", "statut": "Statut"})
            st.dataframe(colorer(aff, "Total /20"), hide_index=True)
            st.markdown(":red[**Rouge**] : moins de 10/20, non validé. "
                        ":blue[**Bleu**] : 10/20, validé. "
                        "**Noir** : plus de 10/20, validé.")
            st.metric("Crédits validés", f"{acquis:g} / {TOTAL_CREDITS}")
            if moyenne is not None:
                st.metric("Moyenne pondérée (par crédits)", f"{moyenne:.2f} / 20")

    with t4:
        if not p["deliberation_publiee"]:
            st.info("Les résultats de la délibération ne sont pas encore publiés.")
        elif p["bloquer_impaye"] and not en_ordre:
            st.warning("Résultats bloqués : régularisez d'abord vos frais.")
        else:
            st.write(f"**Année académique : {p['annee']}**")
            c1, c2, c3 = st.columns(3)
            c1.metric("Crédits validés", f"{acquis:g} / {TOTAL_CREDITS}")
            c2.metric("Minimum pour passer", f"{SEUIL_CREDITS} crédits")
            c3.metric("Moyenne annuelle", "—" if moyenne is None else f"{moyenne:.2f} / 20")
            if not complet(d):
                st.info("Certaines cotes ne sont pas encore disponibles : "
                        "la décision finale n'est pas encore calculée.")
            st.write(f"**Mention :** {mention(acquis, d)}")
            dec = decision(acquis, d)
            coul = "green" if dec == "ADMIS" else "red" if dec == "DÉFAILLANT" else "gray"
            st.subheader(f"Décision : :{coul}[{dec}]")
            try:
                pdf = bulletin_pdf(e, profil_de(e["matricule"]), d, moyenne, acquis,
                                   total_cr, p["annee"], photo,
                                   f"{ligne.index[0] + 1:03d}")
                st.download_button("Télécharger mon bulletin (PDF)", pdf,
                                   f"bulletin_{e['matricule']}.pdf", "application/pdf")
            except ImportError:
                st.info("Bulletin PDF indisponible : installez reportlab "
                        "(py -m pip install reportlab).")


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
    if lire_mot_de_passe() == MOT_DE_PASSE_PAR_DEFAUT:
        st.warning("Le mot de passe par défaut est encore utilisé. Définissez le secret MOT_DE_PASSE.")
    if st.text_input("Mot de passe", type="password") != lire_mot_de_passe():
        st.info("Entrez le mot de passe pour continuer.")
        return

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
st.set_page_config(page_title=NOM, page_icon=_icone)
if trouver_logo():
    st.image(trouver_logo(), width=100)
st.title(NOM)
espace = st.sidebar.radio("Espace", ["Étudiant", "Administrateur"])
if espace == "Étudiant":
    espace_etudiant()
else:
    espace_admin()
