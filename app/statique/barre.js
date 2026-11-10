// Commun aux menus (Services, Appareils, Cross-seed, Métriques) : barre du haut, choix du fond, effets à la souris,
// apparition des panneaux, icônes des applis et mise en forme des nombres.
// Usage : <div id="barre" data-menu="services"></div> puis <script src="/s/barre.js"></script>.
// Le fond choisi est retenu dans le navigateur (localStorage), propre à chaque appareil.
(function () {
  const FONDS = [
    ["Dégradés", [["minuit", "Minuit"], ["aurore", "Aurore"], ["maillage", "Maillage"], ["prisme", "Prisme"], ["ocean", "Océan"],
                  ["lagon", "Lagon"], ["emeraude", "Émeraude"], ["rose", "Rose"], ["ambre", "Ambre"], ["crepuscule", "Crépuscule"], ["brume", "Brume"]]],
    ["Sobres", [["degrade", "Gris bleuté"], ["graphite", "Graphite"], ["noir", "Noir"]]],
    ["Formes", [["voiles", "Voiles"], ["voiles-nuit", "Nuit"], ["voiles-graphite", "Ardoise"], ["neon", "Néon"], ["neon-bleu", "Néon bleu"]]],
  ];
  const FORMES = new Set(FONDS[2][1].map((f) => f[0]));
  const TOUS = FONDS.flatMap((g) => g[1].map((f) => f[0]));
  const MENUS = [
    ["services", "/", "Services", '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>'],
    ["appareils", "/appareils", "Appareils", '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>'],
    ["crossseed", "/crossseed", "Cross-seed", '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>'],
    ["metriques", "/metriques", "Métriques", '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>'],
  ];
  const A0 = { echapper: (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])) };
  const CONF = window.VIGIE || { titre: "Vigie" };
  if (!CONF.qbittorrent) MENUS.splice(MENUS.findIndex((m) => m[0] === "crossseed"), 1);
  const memo = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
  let fond = memo("vigie-fond") || "minuit";
  if (!TOUS.includes(fond)) fond = "minuit";
  let anime = memo("vigie-fond-anime") !== "0";
  let calque = document.getElementById("fond");
  if (!calque) { calque = document.createElement("div"); calque.id = "fond"; document.body.prepend(calque); }
  const appliquer = () => { calque.className = `f-${fond}` + (anime && !FORMES.has(fond) ? " anime" : ""); };
  appliquer();

  const svg = (p, t = 15) => `<svg width="${t}" height="${t}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
  const hote = document.getElementById("barre");
  if (hote) {
    const actif = hote.dataset.menu;
    hote.className = "barre-haut";
    hote.innerHTML = `
      <a class="marque" href="/"><i>${A0.echapper(CONF.titre.slice(0, 1).toUpperCase())}</i>${A0.echapper(CONF.titre)}</a>
      <nav class="menus" aria-label="Menus">${MENUS.map(([id, href, nom, ic]) =>
        `<a href="${href}" ${id === actif ? `aria-current="page"` : ""} data-menu="${id}">${svg(ic)}${nom}<b hidden></b></a>`).join("")}</nav>
      <div class="droite">
        <span class="heure" id="barre-heure"></span>
        <button class="bouton-fond" id="barre-fond" aria-label="Changer le fond d'écran" aria-expanded="false">${svg('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>')}</button>
      </div>
      <div class="choix-fond" id="choix-fond">${FONDS.map(([titre, l]) => `<h4>${titre}</h4><div class="grille-f">${l.map(([id, nom]) =>
        `<button data-fond="${id}" class="f-${id}${id === fond ? " actif" : ""}" aria-label="Fond ${nom}"><span>${nom}</span></button>`).join("")}</div>`).join("")}
        <label><input type="checkbox" id="fond-anime" ${anime ? "checked" : ""}> Dégradé en mouvement lent</label></div>`;
    const heure = () => { document.getElementById("barre-heure").textContent = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); };
    heure(); setInterval(heure, 15000);
    const bouton = document.getElementById("barre-fond"), choix = document.getElementById("choix-fond");
    bouton.onclick = (e) => { e.stopPropagation(); const o = choix.classList.toggle("ouvert"); bouton.setAttribute("aria-expanded", o); };
    document.addEventListener("click", (e) => { if (!choix.contains(e.target)) { choix.classList.remove("ouvert"); bouton.setAttribute("aria-expanded", "false"); } });
    choix.onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      fond = b.dataset.fond; appliquer(); memo("vigie-fond", fond);
      choix.querySelectorAll("button").forEach((x) => x.classList.toggle("actif", x === b));
    };
    document.getElementById("fond-anime").onchange = (e) => { anime = e.target.checked; appliquer(); memo("vigie-fond-anime", anime ? "1" : "0"); };
  }

  // ------------------------------------------------------------ effets à la souris (pas sur écran tactile)
  const souris = matchMedia("(hover: hover) and (pointer: fine)").matches;
  const calme = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (souris) {
    document.addEventListener("pointermove", (e) => {
      const el = e.target.closest && e.target.closest(".lueur, .carte, .chiffre");
      if (el) { const r = el.getBoundingClientRect(); el.style.setProperty("--mx", `${e.clientX - r.left}px`); el.style.setProperty("--my", `${e.clientY - r.top}px`); }
      const t = !calme && e.target.closest && e.target.closest(".incline");
      if (t) {
        const r = t.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
        t.style.transform = `perspective(700px) rotateX(${(-y * 7).toFixed(2)}deg) rotateY(${(x * 7).toFixed(2)}deg) translateY(-3px)`;
      }
    }, { passive: true });
    document.addEventListener("pointerout", (e) => {
      const t = e.target.closest && e.target.closest(".incline");
      if (t && !t.contains(e.relatedTarget)) t.style.transform = "";
    }, { passive: true });
  }

  // ------------------------------------------------------------ outils partagés
  const nb = (x, d = 0) => x == null || isNaN(x) ? "—" : Number(x).toLocaleString("fr-FR", { maximumFractionDigits: d, minimumFractionDigits: d });
  const unite = (x, base, us) => { if (x == null) return "—"; let i = 0; while (Math.abs(x) >= base && i < us.length - 1) { x /= base; i++; } return `${nb(x, x < 10 && i ? 1 : 0)} ${us[i]}`; };
  const A = window.vigie = {
    nb, svg,
    octets: (x) => unite(x, 1024, ["o", "Ko", "Mo", "Go", "To"]),
    bits: (x) => unite(x, 1000, ["b/s", "kb/s", "Mb/s", "Gb/s"]),
    date: (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—",
    duree: (s) => { if (s == null) return "—"; const j = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return j ? `${j} j ${h} h` : h ? `${h} h ${m} min` : `${m} min`; },
    il_y_a: (ts) => { const s = Date.now() / 1000 - ts; return s < 90 ? "à l'instant" : s < 3600 ? `il y a ${Math.round(s / 60)} min` : s < 86400 ? `il y a ${Math.round(s / 3600)} h` : `il y a ${Math.round(s / 86400)} j`; },
    echapper: (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    // première fois : les panneaux apparaissent en cascade ; ensuite les mises à jour ne rejouent pas l'animation
    apparition: (racine) => {
      if (calme || document.body.dataset.vu) return;
      document.body.dataset.vu = "1"; document.body.classList.add("anim");
      racine.querySelectorAll(".apparait, .carte, .chiffre").forEach((el, i) => el.style.setProperty("--i", Math.min(i, 24)));
      setTimeout(() => document.body.classList.remove("anim"), 2200);
    },
    // Pastille du menu Métriques : nombre d'alertes ouvertes
    alertes: (n) => { const b = hote && hote.querySelector('[data-menu="metriques"] b'); if (b) { b.hidden = !n; b.textContent = n || ""; } },
    ICONES: {
      plex: ["M8 4l8 8-8 8"], jellyfin: ["M12 3 3 19h18L12 3z", "M12 10l-3.5 6h7L12 10z"], seerr: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
      tautulli: ["M5 20V11", "M12 20V4", "M19 20v-7"], qbit: ["M12 3v12", "m7 10 5 5 5-5", "M4 21h16"], seed: ["M12 21V9", "m7 14 5-5 5 5", "M4 3h16"],
      sonarr: ["M3 7h18v12H3z", "m8 3 4 4 4-4"], radarr: ["M4 4h16v16H4z", "M8 4v16", "M16 4v16", "M4 12h4", "M16 12h4"],
      lidarr: ["M9 18V5l12-2v13", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M18 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"], prowlarr: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m21 21-4.3-4.3"],
      bazarr: ["M3 5h18v14H3z", "M7 15h4", "M13 15h4", "M7 11h10"], cleanuparr: ["M20 4l-8 8", "M10 10l4 4", "M10 10c-3 2-5 5-6 10 5-1 8-3 10-6", "M7 17l2-2"], opnsense: ["M3 5h18v14H3z", "M3 10h18", "M3 15h18", "M9 5v5", "M15 10v5", "M9 15v4"],
      adguard: ["M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z", "m9 12 2 2 4-4"], zoraxy: ["M3 12h7", "M10 12l4-6h6", "M10 12l4 6h6", "m18 3 3 3-3 3", "m18 15 3 3-3 3"],
      cloudflare: ["M7 18a4 4 0 0 1-.6-8 6 6 0 0 1 11.6-1.5A4.5 4.5 0 0 1 17.5 18H7z"], vpn: ["M5 11h14v10H5z", "M8 11V7a4 4 0 0 1 8 0v4"],
      auth: ["M7.5 10.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9z", "M10.7 12 20 2.7", "m16 7 3 3", "m14 9 2 2"], vault: ["M3 4h18v16H3z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z", "M7 20v2", "M17 20v2"],
      proxmox: ["M4 4h16v6H4z", "M4 14h16v6H4z", "M8 7h.01", "M8 17h.01"], pbs: ["M3 4h18v4H3z", "M5 8v12h14V8", "M10 12h4"], beszel: ["M3 12h4l3-8 4 16 3-8h4"],
      portainer: ["M12 2 3 7v10l9 5 9-5V7l-9-5z", "M3 7l9 5 9-5", "M12 12v10"], kopia: ["M9 9h12v12H9z", "M5 15H3V3h12v2"],
      netbox: ["M9 3h6v6H9z", "M12 9v3", "M5 15v-3h14v3", "M3 15h4v6H3z", "M10 15h4v6h-4z", "M17 15h4v6h-4z"],
      kuma: ["M20.8 5.6a5.5 5.5 0 0 0-7.8 0L12 6.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 22l8.8-8.6a5.5 5.5 0 0 0 0-7.8z", "M6 12h3l2-3 2 5 1.5-2H18"],
      ha: ["M3 11 12 3l9 8", "M5 10v11h14V10", "M10 21v-6h4v6"], rustdesk: ["M2 4h20v13H2z", "M8 21h8", "M12 17v4", "m10 8 3 2.5-3 2.5"],
      mail: ["M3 5h18v14H3z", "m3 6 9 7 9-7"], apps: ["M4 4h6v6H4z", "M14 4h6v6h-6z", "M4 14h6v6H4z", "M14 14h6v6h-6z"],
      web: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M3 12h18", "M12 3c3 3.5 3 14.5 0 18", "M12 3c-3 3.5-3 14.5 0 18"],
      nas: ["M6 3h12v18H6z", "M10 7h4", "M10 11h4", "M12 17h.01"],
      musique: ["M9 18V5l12-2v13", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M18 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
      ia: ["M12 3v3", "M5 8h14v11H5z", "M9 13h.01", "M15 13h.01", "M10 16h4", "M2 12h3", "M19 12h3"],
      photos: ["M3 5h18v14H3z", "M9 11a2 2 0 1 0 0-.01", "m21 16-5-5L6 19"],
      cloche: ["M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9", "M10.3 21a1.94 1.94 0 0 0 3.4 0"],
      bouclier: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],
      reveil: ["M12 2v10", "M18.4 6.6a9 9 0 1 1-12.77.04"],
      videos: ["M22 8.6a2.5 2.5 0 0 0-1.8-1.8C18.6 6.4 12 6.4 12 6.4s-6.6 0-8.2.4A2.5 2.5 0 0 0 2 8.6 26 26 0 0 0 1.6 12a26 26 0 0 0 .4 3.4 2.5 2.5 0 0 0 1.8 1.8c1.6.4 8.2.4 8.2.4s6.6 0 8.2-.4a2.5 2.5 0 0 0 1.8-1.8 26 26 0 0 0 .4-3.4 26 26 0 0 0-.4-3.4z", "m10 15 5-3-5-3z"],
      documents: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "M16 13H8", "M16 17H8", "M10 9H8"],
      dessin: ["M12 19l7-7 3 3-7 7-3-3z", "M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z", "M2 2l7.586 7.586", "M11 13a2 2 0 1 0 0-.01"],
      partage: ["M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8", "M16 6l-4-4-4 4", "M12 2v13"],
      telechargement: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "M7 10l5 5 5-5", "M12 15V3"],
      casque: ["M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"],
      livre: ["M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z", "M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"],
      manette: ["M6 11h4", "M8 9v4", "M15 12h.01", "M18 10h.01", "M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z"],
      ecran: ["M2 3h20v14H2z", "M8 21h8", "M12 17v4", "m10 8 4 2-4 2z"],
      synchro: ["M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", "M3 3v5h5", "M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16", "M16 16h5v5"],
      anime: ["M7 3h10l-2 4H9z", "M4 7h16v13H4z", "M9 13h.01", "M15 13h.01"],
      wiki: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"],
      git: ["M6 3v12", "M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M18 9a9 9 0 0 1-9 9"],
      enceinte: ["M6 2h12v20H6z", "M12 18a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 6h.01"],
    },
    icone: (cle, t = 20) => `<svg width="${t}" height="${t}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${(A.ICONES[cle] || A.ICONES.web).map((d) => `<path d="${d}"/>`).join("")}</svg>`,
  };
  // page déjà écrite (Appareils) : apparition dès le chargement ; les autres l'appellent après leur premier affichage
  document.addEventListener("DOMContentLoaded", () => { if (document.querySelector(".carte")) A.apparition(document); });
  // le nombre d'alertes s'affiche sur les 3 menus
  fetch("/api/services", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((d) => d && A.alertes(d.n_alertes)).catch(() => {});
})();
