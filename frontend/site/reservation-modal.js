// Modale de confirmation de réservation (chat web). Partagée entre chat.html
// et salon-public.html : appelée quand le serveur envoie
// {"type": "reservation_confirmee", ...}, jamais construite à partir du texte
// du bot ni de ce que le client a saisi.

function afficherConfirmationReservation(info) {
  const elementActifAvant = document.activeElement;

  const fond = document.createElement("div");
  fond.className = "fond-modale-reservation";

  const carte = document.createElement("div");
  carte.className = "modale-reservation";
  carte.setAttribute("role", "dialog");
  carte.setAttribute("aria-modal", "true");
  carte.setAttribute("aria-labelledby", "titre-modale-reservation");

  const icone = document.createElement("div");
  icone.className = "icone-validation";
  // SVG fixe, sans aucune valeur injectée : pas de risque XSS ici.
  icone.innerHTML =
    '<svg viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="23" fill="none"/><path fill="none" d="M15 27l7 7 16-17"/></svg>';
  carte.appendChild(icone);

  const titre = document.createElement("h2");
  titre.id = "titre-modale-reservation";
  titre.textContent = "Réservation confirmée";
  carte.appendChild(titre);

  const recap = document.createElement("dl");
  recap.className = "recap-reservation";
  // Chaque valeur vient du serveur (données réelles de l'événement créé) et est
  // insérée via textContent, jamais innerHTML.
  [
    ["Salon", info.salon],
    ["Prestation", info.prestation],
    ["Date", info.date_affichage],
    ["Heure", info.heure],
    ["Nom", info.prenom],
  ].forEach(([libelle, valeur]) => {
    const dt = document.createElement("dt");
    dt.textContent = libelle;
    const dd = document.createElement("dd");
    dd.textContent = valeur;
    recap.appendChild(dt);
    recap.appendChild(dd);
  });
  carte.appendChild(recap);

  const actions = document.createElement("div");
  actions.className = "actions-modale-reservation";

  const boutonIcs = document.createElement("button");
  boutonIcs.type = "button";
  boutonIcs.textContent = "Ajouter à mon calendrier";
  boutonIcs.addEventListener("click", () => telechargerIcsReservation(info));
  actions.appendChild(boutonIcs);

  const boutonFermer = document.createElement("button");
  boutonFermer.type = "button";
  boutonFermer.className = "primary";
  boutonFermer.textContent = "Fermer";
  actions.appendChild(boutonFermer);

  carte.appendChild(actions);
  fond.appendChild(carte);
  document.body.appendChild(fond);

  const focusables = [boutonIcs, boutonFermer];
  boutonFermer.focus();

  function fermer() {
    document.removeEventListener("keydown", surClavier);
    fond.remove();
    if (elementActifAvant && typeof elementActifAvant.focus === "function") {
      elementActifAvant.focus();
    }
  }

  function surClavier(event) {
    if (event.key === "Escape") {
      fermer();
      return;
    }
    if (event.key !== "Tab") return;
    event.preventDefault();
    const i = focusables.indexOf(document.activeElement);
    const suivant = event.shiftKey
      ? focusables[(i - 1 + focusables.length) % focusables.length]
      : focusables[(i + 1) % focusables.length];
    suivant.focus();
  }

  document.addEventListener("keydown", surClavier);
  boutonFermer.addEventListener("click", fermer);
  fond.addEventListener("click", (event) => {
    if (event.target === fond) fermer();
  });
}

// Décalage (en minutes) entre UTC et l'heure de Paris à l'instant donné,
// calculé via Intl plutôt qu'une table fixe pour rester correct autour du
// changement d'heure été/hiver.
function decalageParisMinutes(date) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Europe/Paris",
    hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(date).reduce((acc, p) => ((acc[p.type] = p.value), acc), {});
  const commeUtc = Date.UTC(
    parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second
  );
  return Math.round((commeUtc - date.getTime()) / 60000);
}

function parisVersUtc(dateIso, heure) {
  const [an, mois, jour] = dateIso.split("-").map(Number);
  const [h, m] = heure.split(":").map(Number);
  const naif = new Date(Date.UTC(an, mois - 1, jour, h, m));
  return new Date(naif.getTime() - decalageParisMinutes(naif) * 60000);
}

function formaterIcsUtc(date) {
  return date.toISOString().replace(/[-:]/g, "").split(".")[0] + "Z";
}

function echapperIcs(texte) {
  return String(texte).replace(/([,;])/g, "\\$1");
}

function telechargerIcsReservation(info) {
  const debut = parisVersUtc(info.date, info.heure);
  // Durée fixée à 1h : aucune durée par prestation en base pour l'instant.
  const fin = new Date(debut.getTime() + 60 * 60000);
  const lignes = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Rachel//Reservation//FR",
    "BEGIN:VEVENT",
    `UID:${Date.now()}-reservation@rachel`,
    `DTSTAMP:${formaterIcsUtc(new Date())}`,
    `DTSTART:${formaterIcsUtc(debut)}`,
    `DTEND:${formaterIcsUtc(fin)}`,
    `SUMMARY:${echapperIcs(info.prestation)} — ${echapperIcs(info.salon)}`,
    `DESCRIPTION:${echapperIcs("Rendez-vous au nom de " + info.prenom)}`,
    "END:VEVENT",
    "END:VCALENDAR",
  ].join("\r\n");

  const blob = new Blob([lignes], { type: "text/calendar;charset=utf-8" });
  const lien = document.createElement("a");
  lien.href = URL.createObjectURL(blob);
  lien.download = "reservation.ics";
  document.body.appendChild(lien);
  lien.click();
  lien.remove();
  URL.revokeObjectURL(lien.href);
}
