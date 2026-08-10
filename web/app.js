/* =========================================================================
   PronoStat — interface web
   =========================================================================
   Servie par l'API elle-même : même origine, donc aucun CORS à configurer
   et un seul hébergement. Volontairement sans framework ni compilation —
   modifier un fichier et recharger suffit.

   Le jeton d'accès est saisi une fois puis conservé dans le navigateur. Il
   n'est jamais écrit dans le dépôt : cette API dépense un quota payant, et
   un secret livré dans du code public serait exploitable par n'importe qui.
   ====================================================================== */

const CLE_JETON = "pronostat.token";
const vue = document.getElementById("vue");

const etat = {
  page: "accueil",
  jeton: localStorage.getItem(CLE_JETON) || "",
  competitions: null,   // mis en cache : la liste ne change pas en session
};

/* ----------------------------------------------------------------- API --- */

async function api(chemin, options = {}) {
  const reponse = await fetch(chemin, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${etat.jeton}`,
      ...(options.headers || {}),
    },
  });
  if (reponse.status === 401 || reponse.status === 503) {
    // Jeton absent ou refusé : inutile d'afficher une erreur technique,
    // on renvoie l'utilisateur là où il peut agir.
    etat.jeton = "";
    localStorage.removeItem(CLE_JETON);
    aller("profil");
    throw new Error("authentification");
  }
  if (!reponse.ok) {
    const corps = await reponse.json().catch(() => ({}));
    throw new Error(corps.detail || `Erreur ${reponse.status}`);
  }
  return reponse.json();
}

/* ------------------------------------------------------------- outils --- */

const echapper = (t) =>
  String(t ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const pourcent = (p) => (p == null ? "—" : `${Math.round(p * 100)} %`);

/** Initiales d'une équipe, faute d'écussons : « Crystal Palace » → « CP ». */
function initiales(nom) {
  return String(nom || "?")
    .split(/\s+/).filter((m) => m.length > 2).slice(0, 2)
    .map((m) => m[0].toUpperCase()).join("") || nom.slice(0, 2).toUpperCase();
}

function quand(iso) {
  if (!iso) return "à venir";
  const d = new Date(iso);
  const jours = Math.round((d - new Date()) / 86400000);
  const heure = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  if (jours === 0) return `Aujourd'hui · ${heure}`;
  if (jours === 1) return `Demain · ${heure}`;
  return `${d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" })} · ${heure}`;
}

/** « il y a 4 min » plutôt qu'un horodatage : c'est la fraîcheur qui
    intéresse l'utilisateur, pas l'heure exacte de collecte. */
function fraicheur(iso) {
  if (!iso) return "—";
  const minutes = Math.round((new Date() - new Date(iso)) / 60000);
  if (minutes < 1) return "à l'instant";
  if (minutes < 60) return `il y a ${minutes} min`;
  const heures = Math.round(minutes / 60);
  if (heures < 24) return `il y a ${heures} h`;
  return `il y a ${Math.round(heures / 24)} j`;
}

const couleurConfiance = (c) => (c >= 7 ? "vert" : c >= 4.5 ? "ambre" : "rouge");

/* ------------------------------------------------------------ rendu ----- */

function afficher(html) {
  vue.innerHTML = `<div class="vue">${html}</div>`;
  vue.scrollTop = 0;
  window.scrollTo(0, 0);
}

function chargement(message = "Chargement…") {
  afficher(`
    <div class="squelette" style="margin-bottom:12px"></div>
    <div class="squelette" style="height:110px"></div>
    <p class="surtitre" style="text-align:center;margin-top:24px">${echapper(message)}</p>
  `);
}

function erreur(message) {
  afficher(`
    <div class="vide">
      <span class="emoji">⚠️</span>
      <p>${echapper(message)}</p>
      <button class="bouton secondaire" style="margin-top:24px;max-width:220px"
              onclick="location.reload()">Réessayer</button>
    </div>
  `);
}

const bandeau = `
  <p class="bandeau-responsable">
    Aucun résultat garanti — jouez responsable. 18+<br>
    Les probabilités sont des estimations, pas des certitudes.
  </p>`;

/* --------------------------------------------------------- carte match --- */

function carteMatch(m) {
  const pronostic = m.recommendation
    ? `<div class="match-pied">
         <span class="pronostic-libelle">${echapper(m.recommendation)}</span>
         <span class="pronostic-proba">${pourcent(m.probability)}</span>
       </div>`
    : `<div class="match-pied">
         <span class="puce">Analyse non lancée</span>
         <span class="puce or">Analyser →</span>
       </div>`;

  return `
    <article class="carte carte-cliquable" data-match='${echapper(JSON.stringify(m))}'>
      <div class="match-entete">
        <span class="match-competition">${echapper(m.competition)}</span>
        <span class="match-heure">${echapper(quand(m.starts_at))}</span>
      </div>
      <div class="match-equipes">
        <div class="equipe">
          <div class="equipe-ecusson">${echapper(initiales(m.home))}</div>
          <div class="equipe-nom">${echapper(m.home)}</div>
        </div>
        <span class="match-vs">VS</span>
        <div class="equipe">
          <div class="equipe-ecusson">${echapper(initiales(m.away))}</div>
          <div class="equipe-nom">${echapper(m.away)}</div>
        </div>
      </div>
      ${pronostic}
    </article>`;
}

/* ------------------------------------------------------------ accueil --- */

async function pageAccueil() {
  chargement("Recherche des matchs du jour…");
  const aujourdhui = new Date().toLocaleDateString("fr-FR", {
    weekday: "long", day: "numeric", month: "long",
  });

  let comps;
  try {
    comps = await competitions();
  } catch (e) {
    if (e.message !== "authentification") erreur(e.message);
    return;
  }

  // On ne balaie que les compétitions majeures : afficher le monde entier
  // noierait l'information, et le cahier des charges plafonne à dix.
  const majeures = comps.filter((c) => c.tier === 1).slice(0, 8);
  const lots = await Promise.all(
    majeures.map(async (c) => {
      try {
        const f = await api(
          `/fixtures?sport=${encodeURIComponent(c.sport)}` +
          `&competition_key=${encodeURIComponent(c.key)}`);
        return f.slice(0, 3).map((x) => ({ ...x, competition: c.label,
                                           sport: c.sport, competition_key: c.key }));
      } catch { return []; }
    })
  );

  const matchs = lots.flat()
    .sort((a, b) => new Date(a.starts_at || 0) - new Date(b.starts_at || 0))
    .slice(0, 10);

  const corps = matchs.length
    ? `<div class="liste-matchs">${matchs.map(carteMatch).join("")}</div>`
    : `<div class="vide"><span class="emoji">📭</span>
         <p>Aucune rencontre programmée pour l'instant.</p>
         <p style="font-size:.85rem;margin-top:8px">
           Les calendriers se remplissent à l'approche des journées.</p>
       </div>`;

  afficher(`
    <header style="margin-bottom:28px">
      <p class="surtitre">Aujourd'hui</p>
      <h1 style="text-transform:capitalize">${echapper(aujourdhui)}</h1>
    </header>
    <div class="section-titre">
      <h2>À l'affiche</h2>
      <span class="puce">${matchs.length} rencontre${matchs.length > 1 ? "s" : ""}</span>
    </div>
    ${corps}
    ${bandeau}
  `);
  brancherCartes();
}

/* ----------------------------------------------------------- analyses --- */

async function pageAnalyses() {
  chargement("Ouverture de vos analyses…");
  let historique;
  try {
    historique = await api("/history");
  } catch (e) {
    if (e.message !== "authentification") erreur(e.message);
    return;
  }

  if (!historique.length) {
    afficher(`
      <h1 style="margin-bottom:24px">Mes analyses</h1>
      <div class="vide">
        <span class="emoji">📊</span>
        <p>Aucune analyse pour le moment.</p>
        <p style="font-size:.85rem;margin-top:8px">
          Lancez-en une avec le bouton <strong>+</strong>.</p>
      </div>`);
    return;
  }

  const resolus = historique.filter((e) => e.resolved);
  const reussis = resolus.filter((e) => e.hit).length;
  const confiance = historique.reduce((s, e) => s + (e.confidence || 0), 0) / historique.length;

  // Le taux n'est affiché qu'au-delà de vingt résultats connus : en deçà, un
  // pourcentage donnerait une précision que l'échantillon ne porte pas.
  const assez = resolus.length >= 20;
  const resume = `
    <div class="grille-3" style="margin-bottom:24px">
      <div class="mini-carte">
        <div class="cle">Analyses</div>
        <div class="valeur">${historique.length}</div>
      </div>
      <div class="mini-carte">
        <div class="cle">Réussite</div>
        <div class="valeur">${assez ? Math.round((reussis / resolus.length) * 100) + " %" : "—"}</div>
      </div>
      <div class="mini-carte">
        <div class="cle">Confiance</div>
        <div class="valeur">${confiance.toFixed(1)}</div>
      </div>
    </div>
    ${assez ? "" : `<p class="surtitre" style="text-align:center;margin-bottom:24px">
        ${resolus.length} résultat${resolus.length > 1 ? "s" : ""} connu${resolus.length > 1 ? "s" : ""}
        · il en faut 20 pour un taux honnête</p>`}`;

  const cartes = historique.map((e) => {
    const statut = !e.resolved
      ? '<span class="puce">En attente</span>'
      : e.hit ? '<span class="puce vert">Réussi</span>'
              : '<span class="puce rouge">Manqué</span>';
    const score = e.resolved && e.actual_home != null
      ? `<span class="puce">${e.actual_home} – ${e.actual_away}</span>` : "";
    return `
      <article class="carte" style="margin-bottom:12px">
        <div class="match-entete">
          <span class="match-competition">${echapper(e.competition)}</span>
          <span class="match-heure">${echapper(fraicheur(e.created_at))}</span>
        </div>
        <div style="font-weight:650;margin-bottom:10px">
          ${echapper(e.home)} <span style="color:var(--texte-faible)">vs</span> ${echapper(e.away)}
        </div>
        <div class="match-pied" style="margin-top:0;border-top:none;padding-top:0">
          <span class="pronostic-libelle">${echapper(e.recommendation)}</span>
          <span style="display:flex;gap:6px;align-items:center">
            ${score}
            <span class="puce ${couleurConfiance(e.confidence)}">${(e.confidence || 0).toFixed(1)}/10</span>
            ${statut}
          </span>
        </div>
      </article>`;
  }).join("");

  afficher(`<h1 style="margin-bottom:24px">Mes analyses</h1>${resume}${cartes}${bandeau}`);
}

/* ---------------------------------------------------- nouvelle analyse --- */

async function competitions() {
  if (!etat.competitions) etat.competitions = await api("/competitions");
  return etat.competitions;
}

async function pageNouvelle(prerempli = null) {
  chargement("Préparation…");
  let comps;
  try {
    comps = await competitions();
  } catch (e) {
    if (e.message !== "authentification") erreur(e.message);
    return;
  }

  const sports = [...new Set(comps.map((c) => c.sport))];
  const nomSport = { football: "Football", basket: "Basketball",
                     tennis: "Tennis", hockey: "Hockey sur glace" };

  afficher(`
    <h1 style="margin-bottom:24px">Nouvelle analyse</h1>
    <label class="champ"><span>Sport</span>
      <select id="sel-sport">
        ${sports.map((s) => `<option value="${s}">${nomSport[s] || s}</option>`).join("")}
      </select>
    </label>
    <label class="champ"><span>Compétition</span>
      <select id="sel-comp"><option>Chargement…</option></select>
    </label>
    <label class="champ"><span>Équipe / joueur 1</span>
      <select id="sel-dom" disabled><option>—</option></select>
    </label>
    <label class="champ"><span>Équipe / joueur 2</span>
      <select id="sel-ext" disabled><option>—</option></select>
    </label>
    <div id="zone-rencontre" style="margin-bottom:16px"></div>
    <button class="bouton" id="btn-lancer" disabled>Lancer l'analyse</button>
    ${bandeau}
  `);

  const selSport = document.getElementById("sel-sport");
  const selComp = document.getElementById("sel-comp");
  const selDom = document.getElementById("sel-dom");
  const selExt = document.getElementById("sel-ext");
  const zone = document.getElementById("zone-rencontre");
  const bouton = document.getElementById("btn-lancer");
  let calendrier = [];

  function remplirCompetitions() {
    const liste = comps.filter((c) => c.sport === selSport.value);
    selComp.innerHTML = liste
      .map((c) => `<option value="${c.key}">${echapper(c.label)}</option>`).join("");
    chargerEquipes();
  }

  async function chargerEquipes() {
    selDom.disabled = selExt.disabled = true;
    selDom.innerHTML = selExt.innerHTML = "<option>Chargement…</option>";
    zone.innerHTML = "";
    bouton.disabled = true;
    const sport = selSport.value, cle = selComp.value;
    if (!cle) return;
    try {
      const [{ teams }, fixtures] = await Promise.all([
        api(`/teams?sport=${sport}&competition_key=${cle}`),
        api(`/fixtures?sport=${sport}&competition_key=${cle}`).catch(() => []),
      ]);
      calendrier = fixtures;
      if (!teams.length) {
        selDom.innerHTML = selExt.innerHTML = "<option>Aucun participant</option>";
        zone.innerHTML = `<div class="carte"><p style="font-size:.88rem;color:var(--texte-doux)">
          Aucun participant disponible pour cette compétition.</p></div>`;
        return;
      }
      selDom.innerHTML = teams.map((t) => `<option>${echapper(t)}</option>`).join("");
      selDom.disabled = false;
      majAdversaires();
    } catch (e) {
      if (e.message !== "authentification") zone.innerHTML =
        `<div class="carte"><p style="color:var(--rouge);font-size:.88rem">${echapper(e.message)}</p></div>`;
    }
  }

  /* La deuxième liste exclut l'équipe déjà choisie : une rencontre d'une
     équipe contre elle-même n'a aucun sens et l'API la refuserait. */
  function majAdversaires() {
    const choisi = selDom.value;
    const restants = [...selDom.options].map((o) => o.value).filter((t) => t !== choisi);
    const precedent = selExt.value;
    selExt.innerHTML = restants.map((t) => `<option>${echapper(t)}</option>`).join("");
    if (restants.includes(precedent)) selExt.value = precedent;
    selExt.disabled = false;
    verifierRencontre();
  }

  function verifierRencontre() {
    const dom = selDom.value, ext = selExt.value;
    bouton.disabled = !(dom && ext);
    if (!dom || !ext) { zone.innerHTML = ""; return; }
    const trouve = calendrier.find(
      (f) => (f.home === dom && f.away === ext) || (f.home === ext && f.away === dom));
    zone.innerHTML = trouve
      ? `<div class="carte" style="border-color:rgb(52 211 153 / .35)">
           <span class="puce vert">Rencontre trouvée</span>
           <p style="margin-top:10px;font-size:.9rem">
             ${echapper(trouve.home)} — ${echapper(trouve.away)}<br>
             <span style="color:var(--texte-doux)">${echapper(quand(trouve.starts_at))}</span></p>
         </div>`
      : `<div class="carte">
           <span class="puce ambre">Rencontre non programmée</span>
           <p style="margin-top:10px;font-size:.88rem;color:var(--texte-doux)">
             L'analyse fonctionnera, mais sans cotes réelles : seules les
             rencontres au calendrier en possèdent.</p>
         </div>`;
  }

  selSport.onchange = remplirCompetitions;
  selComp.onchange = chargerEquipes;
  selDom.onchange = majAdversaires;
  selExt.onchange = verifierRencontre;
  bouton.onclick = () => lancerAnalyse(selSport.value, selComp.value,
                                       selDom.value, selExt.value);

  remplirCompetitions();
  if (prerempli) {
    selSport.value = prerempli.sport;
    remplirCompetitions();
    selComp.value = prerempli.competition_key;
    await chargerEquipes();
    if ([...selDom.options].some((o) => o.value === prerempli.home)) {
      selDom.value = prerempli.home;
      majAdversaires();
      if ([...selExt.options].some((o) => o.value === prerempli.away)) {
        selExt.value = prerempli.away;
        verifierRencontre();
      }
    }
  }
}

/* --------------------------------------------------------- simulation --- */

const ETAPES = [
  "Recherche des informations",
  "Analyse des statistiques",
  "Lecture du marché",
  "Simulation statistique",
];

async function lancerAnalyse(sport, competition_key, home, away) {
  afficher(`
    <h1 style="margin-bottom:8px">Analyse en cours</h1>
    <p style="color:var(--texte-doux);margin-bottom:28px">
      ${echapper(home)} — ${echapper(away)}</p>
    <div class="carte" id="etapes">
      ${ETAPES.map((t, i) => `
        <div class="etape ${i === 0 ? "active" : ""}" data-i="${i}">
          <span class="etape-puce"></span>${echapper(t)}
        </div>`).join("")}
    </div>`);

  // L'avancement est indicatif : le serveur ne rend pas la main avant la fin.
  // Il informe sur ce qui se passe, sans prétendre mesurer le progrès réel.
  let i = 0;
  const minuteur = setInterval(() => {
    const etapes = document.querySelectorAll(".etape");
    if (i < etapes.length - 1 && etapes[i]) {
      etapes[i].classList.replace("active", "faite");
      etapes[i].querySelector(".etape-puce").textContent = "✓";
      etapes[++i]?.classList.add("active");
    }
  }, 3200);

  try {
    const resultat = await api("/analysis", {
      method: "POST",
      body: JSON.stringify({ sport, competition_key, home, away }),
    });
    clearInterval(minuteur);
    pageResultat(resultat);
  } catch (e) {
    clearInterval(minuteur);
    if (e.message !== "authentification") erreur(e.message);
  }
}

/* ----------------------------------------------------------- résultat --- */

function pageResultat(r) {
  const marches = r.markets || [];
  const trouver = (prefixe) => marches.filter((m) => m.key.startsWith(prefixe));
  const meilleur = (lignes) =>
    lignes.length ? lignes.reduce((a, b) => (b.probability > a.probability ? b : a)) : null;

  const score = (r.pick_scores || r.top_scores || [])[0];
  const total = meilleur(trouver("total_over_"));
  const btts = marches.find((m) => m.key === "btts_yes");
  const hcp = meilleur(trouver("hcp_"));

  const mini = (cle, valeur, sous) => `
    <div class="mini-carte">
      <div class="cle">${echapper(cle)}</div>
      <div class="valeur">${echapper(valeur)}</div>
      ${sous ? `<div class="cle" style="margin-top:2px">${echapper(sous)}</div>` : ""}
    </div>`;

  const cartesMarches = [
    score ? mini("Score probable", score.score, pourcent(score.probability)) : "",
    total ? mini("Buts", total.label.replace(/^Plus de /, "+"), pourcent(total.probability)) : "",
    btts ? mini("Les 2 marquent", btts.probability >= 0.5 ? "Oui" : "Non",
                pourcent(btts.probability >= 0.5 ? btts.probability : 1 - btts.probability)) : "",
    hcp ? mini("Handicap", hcp.label, pourcent(hcp.probability)) : "",
  ].filter(Boolean).join("");

  const sources = (r.sources || []).slice(0, 6).map((s) => `
    <div class="ligne-stat">
      <span style="color:var(--texte-doux)">${echapper(s.detail || s.source)}</span>
      <span class="val" style="font-weight:500;font-size:.82rem;color:var(--texte-faible)">
        ${echapper(fraicheur(s.fetched_at))}</span>
    </div>`).join("");

  const confiance = r.confidence ?? 0;
  const incoherences = (r.consistency || []).length
    ? `<div class="carte" style="border-color:rgb(248 113 113 / .4);margin-bottom:16px">
         <span class="puce rouge">Résultat incohérent</span>
         <p style="margin-top:10px;font-size:.88rem">${
           (r.consistency).map(echapper).join("<br>")}</p></div>`
    : "";

  afficher(`
    <button class="bouton secondaire" style="width:auto;padding:8px 16px;margin-bottom:20px"
            onclick="window.__retour()">← Retour</button>

    <div class="match-entete">
      <span class="match-competition">${echapper(r.match?.competition || "")}</span>
    </div>
    <div class="match-equipes" style="margin-bottom:24px">
      <div class="equipe">
        <div class="equipe-ecusson">${echapper(initiales(r.match?.home))}</div>
        <div class="equipe-nom">${echapper(r.match?.home)}</div>
      </div>
      <span class="match-vs">VS</span>
      <div class="equipe">
        <div class="equipe-ecusson">${echapper(initiales(r.match?.away))}</div>
        <div class="equipe-nom">${echapper(r.match?.away)}</div>
      </div>
    </div>

    ${incoherences}

    <div class="pronostic-vedette" style="margin-bottom:20px">
      <p class="surtitre">Pronostic principal</p>
      <p class="choix">${echapper(r.decision?.recommendation || "—")}</p>
      <p class="grande-proba">${pourcent(r.decision?.probability)}</p>
      <div style="margin-top:18px">
        <span class="puce ${couleurConfiance(confiance)}">
          Niveau de confiance ${confiance.toFixed(1)} / 10</span>
      </div>
    </div>

    ${cartesMarches ? `<div class="grille" style="margin-bottom:20px">${cartesMarches}</div>` : ""}

    <div class="section-titre"><h2>Issue du match</h2></div>
    <div class="carte">
      ${["home", "draw", "away"].filter((k) => r.probabilities?.[k] != null).map((k) => {
        const nom = k === "home" ? r.match.home : k === "away" ? r.match.away : "Match nul";
        const p = r.probabilities[k];
        return `<div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:.9rem">
            <span>${echapper(nom)}</span><span class="val">${pourcent(p)}</span></div>
          <div class="jauge"><div class="jauge-piste">
            <div class="jauge-valeur" style="width:${Math.round(p * 100)}%"></div>
          </div></div></div>`;
      }).join("")}
    </div>

    ${sources ? `<div class="section-titre"><h2>Sources des données</h2></div>
      <div class="carte">${sources}</div>` : ""}

    ${bandeau}
  `);
}

/* -------------------------------------------------------------- profil --- */

async function pageProfil() {
  if (!etat.jeton) {
    afficher(`
      <h1 style="margin-bottom:8px">Connexion</h1>
      <p style="color:var(--texte-doux);margin-bottom:28px;font-size:.9rem">
        Saisissez la clé d'accès de votre service. Elle reste sur cet appareil.</p>
      <label class="champ"><span>Clé d'accès</span>
        <input type="password" id="saisie-jeton" autocomplete="current-password"></label>
      <button class="bouton" id="btn-connexion">Se connecter</button>
      ${bandeau}`);
    document.getElementById("btn-connexion").onclick = () => {
      const valeur = document.getElementById("saisie-jeton").value.trim();
      if (!valeur) return;
      etat.jeton = valeur;
      localStorage.setItem(CLE_JETON, valeur);
      aller("accueil");
    };
    return;
  }

  chargement("Lecture des informations…");
  let quota, sante;
  try {
    [quota, sante] = await Promise.all([api("/quota"), fetch("/health").then((r) => r.json())]);
  } catch (e) {
    if (e.message !== "authentification") erreur(e.message);
    return;
  }

  const restant = quota.odds_credits_remaining;
  const limite = quota.odds_credits_limit || 500;
  const part = limite ? Math.max(0, Math.min(1, restant / limite)) : 0;

  afficher(`
    <h1 style="margin-bottom:24px">Profil</h1>

    <div class="section-titre"><h2>Consultations de cotes restantes</h2></div>
    <div class="carte" style="margin-bottom:20px">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span style="font-size:2rem;font-weight:700">${restant ?? "—"}</span>
        <span style="color:var(--texte-doux)">sur ${limite}</span>
      </div>
      <div class="jauge"><div class="jauge-piste">
        <div class="jauge-valeur ${part > 0.3 ? "vert" : ""}"
             style="width:${Math.round(part * 100)}%"></div></div></div>
      <p style="font-size:.8rem;color:var(--texte-faible);margin-top:10px">
        Une analyse en consomme ${quota.cost_per_analysis}.
        ${quota.period_resets_at
          ? "Renouvellement le " + new Date(quota.period_resets_at).toLocaleDateString("fr-FR")
          : "Date de renouvellement non communiquée."}</p>
    </div>

    <div class="section-titre"><h2>Application</h2></div>
    <div class="carte" style="margin-bottom:20px">
      <div class="ligne-stat"><span>Version du modèle</span>
        <span class="val">${echapper(sante.model_version)}</span></div>
      <div class="ligne-stat"><span>Version de la recherche</span>
        <span class="val">${echapper(sante.research_version)}</span></div>
    </div>

    <button class="bouton secondaire" id="btn-deconnexion">Se déconnecter</button>
    ${bandeau}`);

  document.getElementById("btn-deconnexion").onclick = () => {
    localStorage.removeItem(CLE_JETON);
    etat.jeton = "";
    aller("profil");
  };
}

/* ---------------------------------------------------------- navigation --- */

const PAGES = { accueil: pageAccueil, analyses: pageAnalyses,
                nouvelle: pageNouvelle, profil: pageProfil };

function aller(page, argument) {
  etat.page = page;
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("actif", b.dataset.page === page));
  (PAGES[page] || pageAccueil)(argument);
}

window.__retour = () => aller(etat.page === "nouvelle" ? "accueil" : etat.page);

function brancherCartes() {
  document.querySelectorAll("[data-match]").forEach((el) => {
    el.onclick = () => {
      const m = JSON.parse(el.dataset.match);
      lancerAnalyse(m.sport, m.competition_key, m.home, m.away);
    };
  });
}

document.querySelectorAll(".nav-item").forEach((b) => {
  b.onclick = () => aller(b.dataset.page);
});
document.getElementById("btn-nouvelle").onclick = () => aller("nouvelle");

aller(etat.jeton ? "accueil" : "profil");
