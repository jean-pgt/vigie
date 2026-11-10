"""Tableau de bord Vigie : données des pages Services et Métriques.

S'appuie sur les mesures de app.py (OPNsense, switch, AdGuard, Uptime Kuma, test de débit) et ajoute, chacune
optionnelle (absente de l'environnement = onglet « à brancher ») :
  - Beszel (BESZEL_URL, BESZEL_EMAIL, BESZEL_PASSWORD) : processeur, mémoire, disques, températures, conteneurs ;
  - Proxmox VE (PROXMOX_URL, PROXMOX_TOKEN_ID, PROXMOX_TOKEN_SECRET, rôle PVEAuditor) : nœuds, VM, conteneurs ;
  - Sonarr, Radarr, Lidarr, Prowlarr, Jellyseerr, Plex, qBittorrent, Authentik : voir .env.example ;
  - PBS (PBS_URL, PBS_TOKEN_ID, PBS_TOKEN_SECRET, PBS_DATASTORE) : remplissage et dernières sauvegardes ;
  - Jellyfin (JELLYFIN_URL, JELLYFIN_API_KEY) : lectures en cours ;
  - Zoraxy (ZORAXY_URL, ZORAXY_USER, ZORAXY_PASSWORD) : règles du reverse proxy ;
  - Cloudflare (CLOUDFLARE_API_TOKEN avec « Zone Analytics : lecture », CLOUDFLARE_ZONE_ID) : trafic de la zone.
Relevé chaque minute ; les séries (processeur, mémoire, températures) sont gardées 8 jours dans la base.

GET /api/services          page Services
GET /api/metriques?p=1j|1s  page Métriques (tous les onglets)
"""

import calendar
import http.cookiejar
import json
import logging
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("vigie-tableau")
A = None  # module app (mesures réseau, base, environnement), fixé par demarrer()

import config

CONF = config.C
SSO = CONF["sso"]["name"]


def _applis():
    """Applis de la page Services (config.yaml, « apps ») : (icône, nom, adresse, groupe, sonde Uptime Kuma)."""
    res = []
    for a in CONF["apps"]:
        if a.get("name"):
            res.append((a.get("icon") or "web", a["name"], a.get("url") or "#", a.get("group") or "Applis", a.get("monitor")))
    return res


APPLIS = _applis()
GROUPES = list(CONF["groups"]) or list(dict.fromkeys(a[3] for a in APPLIS))
PERIODE = 60
SEUIL_TEMP, SEUIL_DISQUE = 80, 85


def env(*cles):
    return all(A.ENV.get(c) for c in cles)


def lire(url, entetes=None, donnees=None, delai=15, ouvreur=None):
    req = urllib.request.Request(url, headers=entetes or {}, data=donnees)
    with (ouvreur.open(req, timeout=delai) if ouvreur else urllib.request.urlopen(req, timeout=delai, context=A.SANS_TLS)) as r:
        return json.load(r)


# ------------------------------------------------------------------ sources

def beszel():
    url = A.ENV["BESZEL_URL"].rstrip("/")
    jeton = lire(f"{url}/api/collections/users/auth-with-password", {"Content-Type": "application/json"},
                 json.dumps({"identity": A.ENV["BESZEL_EMAIL"], "password": A.ENV["BESZEL_PASSWORD"]}).encode())["token"]
    h = {"Authorization": jeton}
    systemes = lire(f"{url}/api/collections/systems/records?perPage=100&sort=name", h)["items"]
    res = []
    for s in systemes:
        i = s.get("info") or {}
        stats = lire(f"{url}/api/collections/system_stats/records?perPage=1&sort=-created&filter="
                     + urllib.parse.quote(f"system='{s['id']}' && type='1m'"), h)["items"]
        st = stats[0]["stats"] if stats else {}
        conteneurs = lire(f"{url}/api/collections/container_stats/records?perPage=1&sort=-created&filter="
                          + urllib.parse.quote(f"system='{s['id']}' && type='1m'"), h)["items"]
        res.append({
            "nom": s["name"], "etat": s["status"], "cpu": i.get("cpu"), "mem": i.get("mp"), "disque": i.get("dp"),
            "charge": i.get("la"), "uptime": i.get("u"), "temp": i.get("dt"), "coeurs": i.get("c") or i.get("t"),
            "modele": i.get("m"), "version": i.get("v"), "volumes": i.get("efs") or {},
            "mem_go": st.get("m"), "mem_util_go": st.get("mu"), "swap": st.get("su"), "disque_go": st.get("d"),
            "disque_util_go": st.get("du"), "net_rx": st.get("nr"), "net_tx": st.get("ns"), "lect": st.get("dr"),
            "ecr": st.get("dw"), "temps": st.get("t") or {},
            "conteneurs": sorted(({"nom": c.get("n"), "cpu": c.get("c"), "mem": c.get("m"), "net": (c.get("ns") or 0) + (c.get("nr") or 0)}
                                  for c in (conteneurs[0]["stats"] if conteneurs else [])), key=lambda c: -(c["cpu"] or 0))})
    return res


def proxmox_api(chemin):
    url = A.ENV["PROXMOX_URL"].rstrip("/")
    return lire(f"{url}/api2/json/{chemin}", {"Authorization": f'PVEAPIToken={A.ENV["PROXMOX_TOKEN_ID"]}={A.ENV["PROXMOX_TOKEN_SECRET"]}'})["data"]


_PM = {"macs": None, "ts": 0}


def proxmox():
    """État du cluster (ressources) et, par nœud, nombre de mises à jour en attente (relu toutes les 30 min)."""
    rs = proxmox_api("cluster/resources")
    noeuds = {}
    for r in rs:
        if r["type"] == "node" and r.get("status") == "online":
            ancien = ((RELEVE.donnees.get("proxmox") or {}).get("noeuds") or {}).get(r["node"]) or {}
            if time.time() - ancien.get("ts", 0) < 1800:
                noeuds[r["node"]] = ancien
                continue
            try:
                noeuds[r["node"]] = {"maj": len(proxmox_api(f'nodes/{r["node"]}/apt/update')), "ts": time.time()}
            except Exception:  # droit Sys.Audit absent : pas de compte des mises à jour
                noeuds[r["node"]] = {"ts": time.time()}
    if time.time() - _PM["ts"] > 600:  # cartes réseau des VM et conteneurs : MAC → nom
        macs = {}
        for r in rs:
            if r["type"] in ("qemu", "lxc") and not r.get("template"):
                try:
                    conf = proxmox_api(f'nodes/{r["node"]}/{r["type"]}/{r["vmid"]}/config')
                except Exception:
                    continue
                for k, v in conf.items():
                    m = re.match(r"^net\d+$", k) and re.search(r"(?:hwaddr=|=)([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", str(v))
                    if m:
                        macs[m.group(1).lower()] = (r.get("name") or str(r["vmid"]), f'{"vm" if r["type"] == "qemu" else "ct"} {r["vmid"]} · {r["node"]}',
                                                    "vm" if r["type"] == "qemu" else "conteneur")
        _PM.update(macs=macs, ts=time.time())
    return {"ts": int(time.time()), "ressources": rs, "noeuds": noeuds}


def proxmox_macs():
    return _PM["macs"] if env("PROXMOX_URL", "PROXMOX_TOKEN_ID", "PROXMOX_TOKEN_SECRET") else None


def pbs():
    url, ds = A.ENV["PBS_URL"].rstrip("/"), A.ENV["PBS_DATASTORE"]
    h = {"Authorization": f'PBSAPIToken={A.ENV["PBS_TOKEN_ID"]}:{A.ENV["PBS_TOKEN_SECRET"]}'}
    st = lire(f"{url}/api2/json/admin/datastore/{ds}/status", h)["data"]
    groupes = lire(f"{url}/api2/json/admin/datastore/{ds}/groups", h)["data"]
    taches = lire(f"{url}/api2/json/nodes/localhost/tasks?typefilter=backup&limit=200&since={int(time.time()) - 7 * 86400}", h)["data"]
    return {"total": st.get("total"), "utilise": st.get("used"), "libre": st.get("avail"),
            "groupes": [{"id": f'{g["backup-type"]}/{g["backup-id"]}', "dernier": g.get("last-backup"), "n": g.get("backup-count"),
                         "commentaire": g.get("comment")} for g in groupes],
            "echecs_7j": sum(1 for t in taches if t.get("status") not in (None, "OK")),
            "echecs": [{"ts": t["starttime"], "id": (t.get("worker_id") or "").split(":")[-1],
                        "raison": "ménage d'une ancienne sauvegarde impossible (dossier non vide)" if "Directory not empty" in (t.get("status") or "")
                        else (t.get("status") or "")[:120]} for t in taches if t.get("status") not in (None, "OK")],
            "reussies_7j": sum(1 for t in taches if t.get("status") == "OK")}


def jellyfin():
    url = A.ENV["JELLYFIN_URL"].rstrip("/")
    h = {"Authorization": f'MediaBrowser Token="{A.ENV["JELLYFIN_API_KEY"]}"'}
    sessions = lire(f"{url}/Sessions?activeWithinSeconds=300", h)
    comptes = lire(f"{url}/Items/Counts", h)
    lectures = []
    for s in sessions:
        it = s.get("NowPlayingItem")
        if not it:
            continue
        pos, duree = (s.get("PlayState") or {}).get("PositionTicks") or 0, it.get("RunTimeTicks") or 0
        titre = it.get("Name", "")
        if it.get("SeriesName"):
            titre = f'{it["SeriesName"]} · S{it.get("ParentIndexNumber", 0):02d}E{it.get("IndexNumber", 0):02d}'
        lectures.append({"titre": titre, "qui": s.get("UserName"), "appareil": s.get("DeviceName"), "client": s.get("Client"),
                         "transcodage": (s.get("PlayState") or {}).get("PlayMethod") == "Transcode",
                         "pct": round(100 * pos / duree) if duree else None, "pause": (s.get("PlayState") or {}).get("IsPaused")})
    return {"lectures": lectures, "films": comptes.get("MovieCount"), "series": comptes.get("SeriesCount"),
            "episodes": comptes.get("EpisodeCount"), "albums": comptes.get("MusicAlbumCount")}


def zoraxy():
    ouvreur = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    url = A.ENV["ZORAXY_URL"].rstrip("/")
    jeton = lambda page: re.search(r'name="zoraxy.csrf.Token" content="([^"]+)"', ouvreur.open(url + page, timeout=15).read().decode()).group(1)
    j = jeton("/login.html")
    ouvreur.open(urllib.request.Request(url + "/api/auth/login", headers={"X-CSRF-Token": j},
                                        data=urllib.parse.urlencode({"username": A.ENV["ZORAXY_USER"], "password": A.ENV["ZORAXY_PASSWORD"]}).encode()),
                 timeout=15).read()
    regles = lire(url + "/api/proxy/list?type=host", ouvreur=ouvreur)
    return [{"domaine": r.get("RootOrMatchingDomain"), "actif": not r.get("Disabled"),
             "vers": ", ".join(o.get("Domain", "") for o in (r.get("ActiveOrigins") or [])) or r.get("Domain"),
             "local": bool((r.get("AccessFilterUUID") or "") not in ("", "default"))} for r in regles]


def cloudflare():
    depuis = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    q = ('{viewer{zones(filter:{zoneTag:"%s"}){httpRequests1hGroups(limit:24,orderBy:[datetime_ASC],filter:{datetime_geq:"%s"})'
         '{dimensions{datetime} sum{requests cachedRequests bytes cachedBytes threats pageViews '
         'countryMap{clientCountryName requests} responseStatusMap{edgeResponseStatus requests}} uniq{uniques}}}}}') % (A.ENV["CLOUDFLARE_ZONE_ID"], depuis)
    r = lire("https://api.cloudflare.com/client/v4/graphql", {"Authorization": "Bearer " + A.ENV["CLOUDFLARE_API_TOKEN"],
                                                               "Content-Type": "application/json"}, json.dumps({"query": q}).encode())
    if r.get("errors"):
        raise RuntimeError(r["errors"][0].get("message", "erreur")[:160])
    heures = r["data"]["viewer"]["zones"][0]["httpRequests1hGroups"]
    pays, codes = {}, {}
    for h in heures:
        for p in h["sum"]["countryMap"]:
            pays[p["clientCountryName"]] = pays.get(p["clientCountryName"], 0) + p["requests"]
        for c in h["sum"]["responseStatusMap"]:
            codes[c["edgeResponseStatus"]] = codes.get(c["edgeResponseStatus"], 0) + c["requests"]
    somme = lambda k: sum(h["sum"][k] for h in heures)
    return {"requetes": somme("requests"), "cache": somme("cachedRequests"), "octets": somme("bytes"),
            "octets_cache": somme("cachedBytes"), "menaces": somme("threats"), "pages": somme("pageViews"),
            "visiteurs": max((h["uniq"]["uniques"] for h in heures), default=0),
            "serie": [[h["dimensions"]["datetime"], h["sum"]["requests"], h["sum"]["cachedRequests"]] for h in heures],
            "pays": sorted(pays.items(), key=lambda x: -x[1])[:6], "codes": sorted(codes.items())}


def kuma_metriques():
    """Jours restants des certificats, par sonde (/metrics d'Uptime Kuma, clé d'API en lecture)."""
    import base64
    auth = base64.b64encode(f':{A.ENV["KUMA_API_KEY"]}'.encode()).decode()
    req = urllib.request.Request(A.ENV["KUMA_URL"].rstrip("/") + "/metrics", headers={"Authorization": f"Basic {auth}"})
    certs = {}
    for l in urllib.request.urlopen(req, timeout=15).read().decode().splitlines():
        m = re.match(r'monitor_cert_days_remaining\{.*?monitor_name="([^"]*)".*\} (-?[\d.]+)', l)
        if m:
            certs[m.group(1)] = float(m.group(2))
    return {"certificats": certs}


def arr(nom, v, cle, media):
    """Sonarr, Radarr, Lidarr (API v3 ou v1) : version, santé, file de téléchargement, sorties à venir, taille de la bibliothèque."""
    url, h = f'{A.ENV[nom.upper() + "_URL"].rstrip("/")}/api/{v}', {"X-Api-Key": A.ENV[cle]}
    st = lire(f"{url}/system/status", h)
    file = lire(f"{url}/queue?pageSize=50&includeUnknownSeriesItems=true", h)
    jour = time.strftime("%Y-%m-%d")
    fin = time.strftime("%Y-%m-%d", time.localtime(time.time() + 7 * 86400))
    res = {"version": st.get("version"), "sante": [{"type": x.get("type"), "msg": x.get("message")} for x in lire(f"{url}/health", h)],
           "file_n": file.get("totalRecords", 0),
           "file": [{"titre": x.get("title"), "etat": x.get("trackedDownloadState") or x.get("status"), "alerte": x.get("trackedDownloadStatus") not in (None, "ok"),
                     "taille": x.get("size"), "reste": x.get("sizeleft"), "fin": x.get("timeleft"), "client": x.get("downloadClient")}
                    for x in file.get("records", [])],
           "a_venir": len(lire(f"{url}/calendar?start={jour}&end={fin}", h))}
    cache = ARR_CACHE.get(nom)
    if not cache or time.time() - cache[0] > 600:  # la liste complète est lourde : relue toutes les 10 min
        tout = lire(f"{url}/{media}", h, delai=60)
        if nom == "lidarr":
            n = {"artistes": len(tout), "albums": sum((a.get("statistics") or {}).get("albumCount", 0) for a in tout)}
        elif nom == "sonarr":
            n = {"series": len(tout), "episodes": sum((s.get("statistics") or {}).get("episodeFileCount", 0) for s in tout),
                 "manquants": sum((s.get("statistics") or {}).get("episodeCount", 0) - (s.get("statistics") or {}).get("episodeFileCount", 0) for s in tout if s.get("monitored"))}
        else:
            n = {"films": len(tout), "presents": sum(1 for m in tout if m.get("hasFile")), "manquants": sum(1 for m in tout if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable"))}
        n["octets"] = sum(((x.get("statistics") or {}).get("sizeOnDisk") or x.get("sizeOnDisk") or 0) for x in tout)
        ARR_CACHE[nom] = cache = (time.time(), n)
    return {**res, "biblio": cache[1]}


ARR_CACHE = {}


def prowlarr():
    url, h = f'{A.ENV["PROWLARR_URL"].rstrip("/")}/api/v1', {"X-Api-Key": A.ENV["PROWLARR_API_KEY"]}
    indexeurs = lire(f"{url}/indexer", h)
    pannes = {s["indexerId"]: s for s in lire(f"{url}/indexerstatus", h)}
    stats = {s["indexerId"]: s for s in (lire(f"{url}/indexerstats", h).get("indexers") or [])}
    return {"version": lire(f"{url}/system/status", h).get("version"),
            "sante": [{"type": x.get("type"), "msg": x.get("message")} for x in lire(f"{url}/health", h)],
            "indexeurs": [{"nom": i["name"], "actif": i.get("enable"), "panne": i["id"] in pannes,
                           "requetes": (stats.get(i["id"]) or {}).get("numberOfQueries"), "echecs": (stats.get(i["id"]) or {}).get("numberOfFailedQueries"),
                           "ms": (stats.get(i["id"]) or {}).get("averageResponseTime"), "saisies": (stats.get(i["id"]) or {}).get("numberOfGrabs")}
                          for i in sorted(indexeurs, key=lambda i: i["name"].lower())]}


def jellyseerr():
    url, h = f'{A.ENV["JELLYSEERR_URL"].rstrip("/")}/api/v1', {"X-Api-Key": A.ENV["JELLYSEERR_API_KEY"]}
    st = lire(f"{url}/status", h)
    n = lire(f"{url}/request/count", h)
    dem = lire(f"{url}/request?take=10&skip=0&sort=added&filter=all", h).get("results", [])
    etat = {1: "en attente", 2: "acceptée", 3: "refusée", 4: "échouée", 5: "terminée"}
    return {"version": st.get("version"), "maj": st.get("updateAvailable"), "compte": n,
            "demandes": [{"ts": calendar.timegm(time.strptime(x["createdAt"][:19], "%Y-%m-%dT%H:%M:%S")) if x.get("createdAt") else None,
                          "type": "série" if x.get("type") == "tv" else "film", "qui": (x.get("requestedBy") or {}).get("displayName"),
                          "etat": etat.get(x.get("status"), "—"), "dispo": (x.get("media") or {}).get("status") == 5,
                          "tmdb": (x.get("media") or {}).get("tmdbId")} for x in dem]}


def plex():
    url = A.ENV["PLEX_URL"].rstrip("/")
    h = {"X-Plex-Token": A.ENV["PLEX_TOKEN"], "Accept": "application/json"}
    ident = lire(f"{url}/identity", h)["MediaContainer"]
    sess = lire(f"{url}/status/sessions", h)["MediaContainer"].get("Metadata") or []
    sections = lire(f"{url}/library/sections", h)["MediaContainer"].get("Directory") or []
    biblio = []
    for s in sections:
        mc = lire(f'{url}/library/sections/{s["key"]}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=0', h)["MediaContainer"]
        biblio.append({"nom": s.get("title"), "type": s.get("type"), "n": mc.get("totalSize", mc.get("size"))})
    lectures = []
    for m in sess:
        titre = m.get("title", "")
        if m.get("type") == "episode":
            titre = f'{m.get("grandparentTitle")} · S{int(m.get("parentIndex") or 0):02d}E{int(m.get("index") or 0):02d}'
        tr = m.get("TranscodeSession") or {}
        lectures.append({"titre": titre, "qui": (m.get("User") or {}).get("title"), "appareil": (m.get("Player") or {}).get("title"),
                         "transcodage": tr.get("videoDecision") == "transcode", "pause": (m.get("Player") or {}).get("state") == "paused",
                         "pct": round(100 * (m.get("viewOffset") or 0) / m["duration"]) if m.get("duration") else None,
                         "debit": ((m.get("Session") or {}).get("bandwidth") or 0) * 1000, "distant": (m.get("Session") or {}).get("location") == "wan"})
    return {"version": ident.get("version"), "lectures": lectures, "biblio": biblio}


def authentik():
    """Connexions réussies et ratées sur 24 h (jeton d'un compte de service Authentik, lecture des événements)."""
    url, h = A.ENV["AUTHENTIK_URL"].rstrip("/"), {"Authorization": "Bearer " + A.ENV["AUTHENTIK_TOKEN"]}
    depuis = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    ev = lambda action: lire(f"{url}/api/v3/events/events/?action={action}&ordering=-created&page_size=100&created__gte={depuis}", h).get("results", [])
    ts = lambda e: calendar.timegm(time.strptime(e["created"][:19], "%Y-%m-%dT%H:%M:%S"))
    def qui(e):
        u = e.get("user") or {}
        if u.get("is_anonymous") and "/recovery/" in ((e.get("context") or {}).get("http_request") or {}).get("path", ""):
            return "lien de secours"  # chemin non conservé : il contient le jeton
        return u.get("username") or (e.get("context") or {}).get("username") or "—"
    ligne = lambda e, ok: {"ts": ts(e), "ok": ok, "qui": qui(e), "ip": e.get("client_ip"),
                           "pays": ((e.get("context") or {}).get("geo") or {}).get("country") or ""}
    reussies, ratees = ev("login"), ev("login_failed")
    return {"reussies": len(reussies), "ratees": len(ratees),
            "dernieres": sorted([ligne(e, True) for e in reussies] + [ligne(e, False) for e in ratees], key=lambda x: -x["ts"])[:30]}


QBIT = [q for q in CONF["qbittorrent"] if q.get("url")][:2]  # au plus deux clients : principal, puis seed (cross-seed)


def qbit(url):
    """qBittorrent sans mot de passe : l'adresse de Vigie doit être dans la liste blanche de l'interface web
    (Options → Interface web → « Contourner l'authentification pour les clients du sous-réseau »)."""
    url = url.rstrip("/") + "/api/v2"
    try:
        tr = lire(f"{url}/transfer/info")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError("permission : ajouter l'adresse de Vigie à la liste blanche de qBittorrent") from None
        raise
    tous = lire(f"{url}/torrents/info", delai=30)
    actifs = [t for t in tous if t.get("dlspeed") or t.get("upspeed")]
    etats = {}
    for t in tous:
        etats[t.get("state")] = etats.get(t.get("state"), 0) + 1
    return {"recu": tr.get("dl_info_speed"), "envoye": tr.get("up_info_speed"), "recu_total": tr.get("dl_info_data"), "envoye_total": tr.get("up_info_data"),
            "n": len(tous), "etats": etats, "erreurs": sum(1 for t in tous if t.get("state") in ("error", "missingFiles")),
            "cross_seed": sum(1 for t in tous if "cross-seed" in (t.get("tags") or "") or t.get("category") == "cross-seed"),
            "ratio": statistics.mean([t["ratio"] for t in tous if t.get("ratio") is not None] or [0]),
            "actifs": [{"nom": t["name"], "recu": t.get("dlspeed"), "envoye": t.get("upspeed"), "pct": round(100 * (t.get("progress") or 0)),
                        "taille": t.get("size"), "ratio": t.get("ratio")} for t in sorted(actifs, key=lambda t: -((t.get("dlspeed") or 0) + (t.get("upspeed") or 0)))[:10]]}


TRACKERS = {str(k).lower(): str(v) for k, v in (CONF["trackers"] or {}).items()}  # morceau d'adresse → nom affiché
_CS = {"t": 0, "d": None}


def crossseed():
    """Page Cross-seed, en direct (cache 2 s) : qBittorrent principal et qBittorrent de seed (alimenté par cross-seed),
    débits, détail par tracker, torrents qui envoient, dernières injections. Lecture seule."""
    if _CS["d"] and time.time() - _CS["t"] < 2:
        return _CS["d"]

    def tracker(t):
        u = (t.get("tracker") or "").lower()
        for k, n in TRACKERS.items():
            if k in u:
                return n
        return re.sub(r"^(?:https?|udp)://(?:[^/]*\.)?([^./]+)\.[a-z]+(?::\d+)?/.*$", r"\1", u).upper() if u else "—"

    def releve(base_url):  # les deux clients sont lus en parallèle (état du serveur + liste des torrents)
        url = base_url.rstrip("/") + "/api/v2"
        try:
            return lire(f"{url}/sync/maindata?rid=0", delai=30).get("server_state", {}), lire(f"{url}/torrents/info", delai=30), None
        except Exception as x:
            return None, None, str(x)[:120]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        lus = dict(zip(("seed",) if len(QBIT) == 1 else ("principal", "seed"), ex.map(releve, [q["url"] for q in QBIT])))
    clients = {}
    for cle, (st, tous, err) in lus.items():
        if err:
            clients[cle] = {"erreur": err}
            continue
        par = {}
        for t in tous:
            n = tracker(t)
            p = par.setdefault(n, {"tracker": n, "torrents": 0, "taille": 0, "envoye": 0, "debit": 0, "actifs": 0})
            p["torrents"] += 1; p["taille"] += t.get("size") or 0; p["envoye"] += t.get("uploaded") or 0
            p["debit"] += t.get("upspeed") or 0; p["actifs"] += 1 if (t.get("upspeed") or 0) > 0 else 0
        resume = lambda t: {"nom": t.get("name"), "tracker": tracker(t), "envoi": t.get("upspeed") or 0, "reception": t.get("dlspeed") or 0,
                            "envoye": t.get("uploaded") or 0, "taille": t.get("size") or 0, "ratio": round(t.get("ratio") or 0, 2),
                            "ajout": t.get("added_on") or 0, "etat": t.get("state"), "categorie": t.get("category") or ""}
        clients[cle] = {
            "envoi": st.get("up_info_speed") or 0, "reception": st.get("dl_info_speed") or 0,
            "envoye_session": st.get("up_info_data") or 0, "envoye_total": st.get("alltime_ul") or 0,
            "recu_total": st.get("alltime_dl") or 0, "ratio": st.get("global_ratio"), "torrents": len(tous),
            "erreurs": sum(1 for t in tous if t.get("state") in ("error", "missingFiles")),
            "pairs": st.get("total_peer_connections") or 0,
            "trackers": sorted(par.values(), key=lambda p: (-p["debit"], -p["torrents"])),
            "actifs": [resume(t) for t in sorted(tous, key=lambda t: -(t.get("upspeed") or 0)) if (t.get("upspeed") or 0) > 0][:20],
            "recents": [resume(t) for t in sorted(tous, key=lambda t: -(t.get("added_on") or 0))[:20]],
        }
    # le démon cross-seed n'a pas d'API publique : son activité se lit à la date de la dernière injection
    recents = (clients.get("seed") or {}).get("recents") or []
    derniere = max((r["ajout"] for r in recents), default=0)
    _CS.update(t=time.time(), d={"maintenant": int(time.time()), "derniere_injection": derniere, "clients": clients})
    return _CS["d"]


SOURCES = {
    "auth": (("AUTHENTIK_URL", "AUTHENTIK_TOKEN"), authentik),
    "sonarr": (("SONARR_URL", "SONARR_API_KEY"), lambda: arr("sonarr", "v3", "SONARR_API_KEY", "series")),
    "radarr": (("RADARR_URL", "RADARR_API_KEY"), lambda: arr("radarr", "v3", "RADARR_API_KEY", "movie")),
    "lidarr": (("LIDARR_URL", "LIDARR_API_KEY"), lambda: arr("lidarr", "v1", "LIDARR_API_KEY", "artist")),
    "prowlarr": (("PROWLARR_URL", "PROWLARR_API_KEY"), prowlarr),
    "jellyseerr": (("JELLYSEERR_URL", "JELLYSEERR_API_KEY"), jellyseerr),
    "plex": (("PLEX_URL", "PLEX_TOKEN"), plex),
    "proxmox": (("PROXMOX_URL", "PROXMOX_TOKEN_ID", "PROXMOX_TOKEN_SECRET"), proxmox),
    "kuma": (("KUMA_URL", "KUMA_API_KEY"), kuma_metriques),
    "beszel": (("BESZEL_URL", "BESZEL_EMAIL", "BESZEL_PASSWORD"), beszel),
    "pbs": (("PBS_URL", "PBS_TOKEN_ID", "PBS_TOKEN_SECRET", "PBS_DATASTORE"), pbs),
    "jellyfin": (("JELLYFIN_URL", "JELLYFIN_API_KEY"), jellyfin),
    "zoraxy": (("ZORAXY_URL", "ZORAXY_USER", "ZORAXY_PASSWORD"), zoraxy),
    "cloudflare": (("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"), cloudflare),
}
for _cle, _q in zip(("qbit", "seed"), QBIT):
    SOURCES[_cle] = ((), lambda u=_q["url"]: qbit(u))


class Releve:
    def __init__(self):
        self.donnees, self.erreurs, self.ts = {}, {}, 0

    def tour(self):
        if A.ENV.get("DEMO") == "1":
            return
        for nom, (cles, f) in SOURCES.items():
            if not env(*cles):
                continue
            try:
                self.donnees[nom] = f()
                self.erreurs.pop(nom, None)
            except Exception as e:
                self.erreurs[nom] = str(e)[:200]
                LOG.warning("%s : %s", nom, e)
        t = int(time.time())
        lignes = []
        for s in self.donnees.get("beszel") or []:
            for k in ("cpu", "mem", "temp"):
                if s.get(k) is not None:
                    lignes.append((t, f'{s["nom"]}.{k}', s[k]))
        for r in (self.donnees.get("proxmox") or {}).get("ressources", []):
            if r["type"] == "node" and r.get("status") == "online":
                lignes += [(t, f'{r["node"]}.pcpu', 100 * r.get("cpu", 0)), (t, f'{r["node"]}.pmem', 100 * r.get("mem", 0) / (r.get("maxmem") or 1))]
        with A.base() as c:
            c.executemany("INSERT INTO serie VALUES (?,?,?)", lignes)
            if t % 3600 < PERIODE:
                c.execute("DELETE FROM serie WHERE ts < ?", (t - 8 * 86400,))
        self.ts = t

    def boucle(self):
        with A.base() as c:
            c.execute("CREATE TABLE IF NOT EXISTS serie (ts INTEGER, cle TEXT, val REAL)")
            c.execute("CREATE INDEX IF NOT EXISTS serie_cle ON serie(cle, ts)")
        while True:
            debut = time.time()
            try:
                self.tour()
            except Exception:
                LOG.exception("tableau")
            time.sleep(max(5, PERIODE - (time.time() - debut)))


RELEVE = Releve()


def demarrer(app, lancer_fil):
    global A
    A = app
    lancer_fil(RELEVE.boucle)


# ------------------------------------------------------------------ alertes

def sondes_kuma():
    return [{**s, "groupe": g["nom"]} for g in (A.MESURES.services or []) for s in g["sondes"]]


def alertes():
    """Liste triée par gravité : critique, attention, info."""
    res = []
    for s in sondes_kuma():
        if s["etat"] == 0:
            res.append(("critique", f'{s["nom"]} ne répond plus', s["msg"] or "sonde en échec", "Uptime Kuma"))
        elif s["etat"] == 2:
            res.append(("attention", f'{s["nom"]} répond mal', s["msg"] or "sonde en attente", "Uptime Kuma"))
    for s in RELEVE.donnees.get("beszel") or []:
        if s["etat"] != "up":
            res.append(("critique", f'{s["nom"]} injoignable', "l'agent Beszel ne répond plus", "Beszel"))
            continue
        if (s.get("temp") or 0) >= SEUIL_TEMP:
            res.append(("attention", f'{s["nom"]} chauffe : {s["temp"]:.0f} °C', f"seuil {SEUIL_TEMP} °C", "Beszel"))
        for v, p in [("système", s.get("disque"))] + list(s.get("volumes", {}).items()):
            if (p or 0) >= SEUIL_DISQUE:
                res.append(("attention", f'{s["nom"]} : {v} plein à {p:.0f} %', f"seuil {SEUIL_DISQUE} %", "Beszel"))
    for n, d in ((RELEVE.donnees.get("proxmox") or {}).get("noeuds") or {}).items():
        if d.get("maj"):
            res.append(("info", f"Mises à jour en attente sur {n}", f'{d["maj"]} paquets', "Proxmox"))
    for nom, j in ((RELEVE.donnees.get("kuma") or {}).get("certificats") or {}).items():
        if 0 <= j < 14:
            res.append(("attention", f"Certificat de {nom} bientôt expiré", f"{j:.0f} jours restants", "Uptime Kuma"))
    p = RELEVE.donnees.get("pbs")
    if p and p["echecs"]:
        dernier = max(p["echecs"], key=lambda e: e["ts"])
        res.append(("attention" if time.time() - dernier["ts"] < 2 * 86400 else "info", f'{len(p["echecs"])} sauvegarde(s) avec erreur sur 7 jours',
                    f'dernière le {time.strftime("%d/%m", time.localtime(dernier["ts"]))} : {dernier["raison"]}', "PBS"))
    for po, e in ((A.topologie()["switch"].get("erreurs") or {}).items()):
        if e.get("hausse", 0) > 0:
            res.append(("attention", f"Erreurs sur le port {po} du switch", f'{e["hausse"]} nouvelle(s) erreur(s) en 10 min (câble ou prise à vérifier)', "Switch"))
    for nom in ("sonarr", "radarr", "lidarr", "prowlarr"):
        x = RELEVE.donnees.get(nom) or {}
        for s in x.get("sante") or []:
            res.append(("attention" if s["type"] == "error" else "info", f"{nom.capitalize()} : {s['msg'][:90]}", "contrôle de santé de l'appli", nom.capitalize()))
        bloques = [f["titre"] for f in x.get("file") or [] if f["alerte"]]
        if bloques:
            res.append(("info", f"{len(bloques)} téléchargement(s) bloqué(s) dans {nom.capitalize()}", ", ".join(bloques[:2])[:120], nom.capitalize()))
    au = RELEVE.donnees.get("auth")
    if au and au["ratees"] >= 10:
        res.append(("attention", f'{au["ratees"]} connexions ratées sur {SSO} en 24 h', "à regarder dans Métriques → Sécurité", SSO))
    attente = ((RELEVE.donnees.get("jellyseerr") or {}).get("compte") or {}).get("pending")
    if attente:
        res.append(("info", f"{attente} demande(s) Jellyseerr à valider", "dans Jellyseerr", "Jellyseerr"))
    existants = {f'{"vm" if r["type"] == "qemu" else "ct"}/{r["vmid"]}' for r in ((RELEVE.donnees.get("proxmox") or {}).get("ressources") or [])
                 if r["type"] in ("qemu", "lxc")}
    if p and existants:
        vieux = [g["id"] for g in p["groupes"] if g["id"] in existants and g.get("dernier") and time.time() - g["dernier"] > 2 * 86400]
        if vieux:
            res.append(("attention", f"{len(vieux)} sauvegarde(s) de plus de 2 jours", ", ".join(vieux[:4]), "PBS"))
    with A.base() as c:
        h0 = int(time.time()) - 86400
        for r in c.execute("""SELECT p.src, p.dst, p.port, sum(p.n) AS n, coalesce(a.nom, a.nom_auto, p.src) AS nom FROM pare_feu p
                              LEFT JOIN appareil a ON a.ip=p.src WHERE p.genre='interne' AND p.heure>=?
                              GROUP BY p.src, p.dst, p.port HAVING n >= 20 ORDER BY n DESC LIMIT 3""", (h0 - h0 % 3600,)):
            res.append(("info", f'{r["nom"]} bloqué vers {r["dst"]}:{r["port"]}', f'{r["n"]} tentatives refusées sur 24 h', "OPNsense"))
        nouveaux = c.execute("SELECT count(*) FROM appareil WHERE connu=0 AND premiere_vue>=?", (h0,)).fetchone()[0]
        if nouveaux:
            res.append(("info", f"{nouveaux} nouvel(s) appareil(s) sur 24 h", "à identifier dans Appareils", "Réseau"))
    for nom, e in list(A.COLLECTE.erreurs.items()) + list(RELEVE.erreurs.items()):
        if "permission" in e:  # accès pas encore autorisé : signalé dans l'onglet de la source, pas comme une panne
            continue
        res.append(("attention", f"Source indisponible : {nom}", e, "Tableau de bord"))
    ordre = {"critique": 0, "attention": 1, "info": 2}
    return [{"niveau": n, "titre": t, "detail": d, "source": s} for n, t, d, s in sorted(res, key=lambda x: ordre[x[0]])]


# ------------------------------------------------------------------ page Services

def services():
    kuma = {s["nom"]: s for s in sondes_kuma()}
    d = RELEVE.donnees
    dns = A.MESURES.dns or {}
    bz = {s["nom"]: s for s in d.get("beszel") or []}
    topo = A.topologie()
    pm = d.get("proxmox")
    vms = [r for r in (pm or {}).get("ressources", []) if r["type"] in ("qemu", "lxc") and not r.get("template")]
    stats = {
        "adguard": f'{dns.get("num_dns_queries", 0):,} requêtes · {100 * dns.get("num_blocked_filtering", 0) / (dns.get("num_dns_queries") or 1):.1f} % bloquées'.replace(",", " ") if dns else None,
        "kuma": f"{len(kuma)} sondes · {sum(1 for s in kuma.values() if s['etat'] == 1)} en ligne",
        "proxmox": (f'{sum(1 for v in vms if v.get("status") == "running")} / {len(vms)} VM et CT allumés' if pm
                    else f'{sum(len(v) for v in topo["hotes"].values())} VM et CT'),
        "beszel": f"{len(bz)} systèmes suivis" if bz else None,
        "zoraxy": f'{len(d["zoraxy"])} règles' if d.get("zoraxy") else None,
        "jellyfin": (f'{len(d["jellyfin"]["lectures"])} en lecture · {d["jellyfin"]["films"]} films' if d.get("jellyfin") else None),
        "pbs": (f'{100 * d["pbs"]["utilise"] / (d["pbs"]["total"] or 1):.0f} % utilisé · {d["pbs"]["reussies_7j"]} sauvegardes en 7 j' if d.get("pbs") else None),
        "plex": (f'{len(d["plex"]["lectures"])} en lecture · {sum(b["n"] or 0 for b in d["plex"]["biblio"] if b["type"] == "movie")} films' if d.get("plex") else None),
        "seerr": (f'{d["jellyseerr"]["compte"].get("pending", 0)} à valider · {d["jellyseerr"]["compte"].get("total", 0)} demandes' if d.get("jellyseerr") else None),
        "sonarr": (f'{d["sonarr"]["file_n"]} en cours · {d["sonarr"]["a_venir"]} épisodes cette semaine' if d.get("sonarr") else None),
        "radarr": (f'{d["radarr"]["file_n"]} en cours · {d["radarr"]["biblio"]["presents"]} films' if d.get("radarr") else None),
        "lidarr": (f'{d["lidarr"]["file_n"]} en cours · {d["lidarr"]["biblio"]["albums"]} albums' if d.get("lidarr") else None),
        "prowlarr": (f'{sum(1 for i in d["prowlarr"]["indexeurs"] if i["actif"] and not i["panne"])} indexeur(s) actif(s)'
                     + (f' · {sum(1 for i in d["prowlarr"]["indexeurs"] if i["actif"] and i["panne"])} en panne' if any(i["actif"] and i["panne"] for i in d["prowlarr"]["indexeurs"]) else "")
                     if d.get("prowlarr") else None),
        "qbit": (f'↓ {d["qbit"]["recu"] / 1e6:.1f} Mo/s · {d["qbit"]["n"]} torrents' if d.get("qbit") else None),
        "seed": (f'↑ {d["seed"]["envoye"] / 1e6:.1f} Mo/s · {d["seed"]["cross_seed"]} cross-seed' if d.get("seed") else None),
    }
    groupes = []
    for g in GROUPES:
        apps = []
        for ic, nom, url, grp, sonde in APPLIS:
            if grp != g:
                continue
            s = kuma.get(sonde) if sonde else None
            etat = "inconnu" if not s else {1: "ok", 0: "ko", 2: "attente", 3: "maintenance"}.get(s["etat"], "inconnu")
            if etat == "inconnu" and ic in SOURCES:  # pas de sonde Kuma, mais la source répond (ou pas) au relevé
                etat = "ok" if d.get(ic) else ("ko" if ic in RELEVE.erreurs else "inconnu")
            stat = stats.get(ic) or (f'en ligne · {s["ping"]} ms' if s and s["etat"] == 1 and s.get("ping") is not None
                                     else (s["msg"] or "hors ligne") if s and s["etat"] == 0 else "en ligne" if s else "")
            apps.append({"icone": ic, "nom": nom, "url": url, "etat": etat, "stat": stat})
        groupes.append({"nom": g, "apps": apps, "en_ligne": sum(a["etat"] == "ok" for a in apps),
                        "suivies": sum(a["etat"] != "inconnu" for a in apps)})
    noeuds = []
    hotes = [h if isinstance(h, dict) else {"name": str(h)} for h in CONF["hosts"]]
    if not hotes:  # rien de décrit : les nœuds Proxmox, puis les machines Beszel
        hotes = [{"name": r["node"]} for r in (pm or {}).get("ressources", []) if r["type"] == "node"] + [{"name": n} for n in bz][:6]
    for h in hotes:
        nom, nas = str(h["name"]), bool(h.get("nas"))
        b = bz.get(nom) or {}
        r = next((x for x in (pm or {}).get("ressources", []) if x["type"] == "node" and x["node"] == nom), None)
        n_vm = len(topo["hotes"].get(nom, [])) if not pm else sum(1 for v in vms if v["node"] == nom)
        noeuds.append({
            "nom": nom, "etat": "ok" if (b.get("etat") == "up" or (r and r.get("status") == "online") or (kuma.get(nom) or {}).get("etat") == 1)
            else ("ko" if b or r or nom in kuma else "inconnu"),
            "cpu": b.get("cpu") if b else (100 * r["cpu"] if r else None),
            "mem": b.get("mem") if b else (100 * r["mem"] / r["maxmem"] if r else None),
            "disque": b.get("disque"), "temp": b.get("temp"), "uptime": b.get("uptime") or (r or {}).get("uptime"),
            "coeurs": b.get("coeurs") or (r or {}).get("maxcpu"), "modele": b.get("modele"),
            "vm": n_vm if not nas else None, "conteneurs": len(b.get("conteneurs") or []) if nas else None,
            "volumes": b.get("volumes") if nas else None, "nas": nas})
    tests = A.vue("1j")["speedtest"]["derniers"]
    al = alertes()
    ok, total = sum(1 for s in kuma.values() if s["etat"] == 1), len(kuma)
    if not kuma:  # sans Uptime Kuma : les applis dont l'état est connu par leur propre intégration
        ok, total = sum(g["en_ligne"] for g in groupes), sum(g["suivies"] for g in groupes)
    return {
        "maintenant": int(time.time()), "alertes": al[:5], "n_alertes": sum(1 for a in al if a["niveau"] != "info"),
        "kpis": {"services": {"ok": ok, "total": total},
                 "internet": tests[0] if tests else None,
                 "proxmox": {"noeuds": sum(1 for n in noeuds if not n["nas"] and n["etat"] == "ok"),
                             "total": sum(1 for r in (pm or {}).get("ressources", []) if r["type"] == "node") or len(topo["hotes"]),
                             "vm_on": sum(1 for v in vms if v.get("status") == "running") if pm else None,
                             "vm": len(vms) if pm else sum(len(v) for v in topo["hotes"].values())},
                 "nas": next((bz.get(n["nom"]) for n in noeuds if n["nas"] and bz.get(n["nom"])), None), "dns": {"requetes": dns.get("num_dns_queries"), "bloquees": dns.get("num_blocked_filtering")} if dns else None},
        "noeuds": noeuds, "groupes": groupes,
        "lectures": ([{**l, "source": "plex"} for l in (d.get("plex") or {}).get("lectures") or []]
                     + [{**l, "source": "jellyfin"} for l in (d.get("jellyfin") or {}).get("lectures") or []]) if d.get("plex") or d.get("jellyfin") else None,
        "pbs": d.get("pbs"), "sources": {k: k in d and d[k] is not None for k in SOURCES}}


# ------------------------------------------------------------------ page Métriques

def K(titre, note, items):
    return {"type": "kpi", "titre": titre, "note": note,
            "items": [{"l": i[0], "v": i[1], "d": i[2] if len(i) > 2 else "", "c": i[3] if len(i) > 3 else None} for i in items]}


def T(titre, note, cols, lignes, droite=None):
    return {"type": "table", "titre": titre, "note": note, "cols": cols, "lignes": lignes, "droite": list(range(1, len(cols))) if droite is None else droite}


def B(titre, note, listes):
    return {"type": "barres", "titre": titre, "note": note, "listes": [{"t": t, "l": l} for t, l in listes]}


def C(titre, note, courbes):
    return {"type": "courbes", "titre": titre, "note": note, "courbes": courbes}


def AB(titre, texte):
    return {"type": "brancher", "titre": titre, "texte": texte}


def serie(c, cle, debut, pas):
    return [[r[0] * pas, r[1]] for r in c.execute("SELECT ts/? AS b, avg(val) FROM serie WHERE cle=? AND ts>=? GROUP BY b ORDER BY b",
                                                   (pas, cle, debut))]


def stats_latence(vals):
    ok = sorted(v for v in vals if v is not None)
    if not ok:
        return None
    q = statistics.quantiles(ok, n=20) if len(ok) > 2 else [ok[-1]] * 19
    gigue = statistics.mean(abs(a - b) for a, b in zip(ok, ok[1:])) if len(ok) > 1 else 0
    return {"moy": statistics.mean(ok), "med": statistics.median(ok), "p95": q[18], "max": ok[-1], "gigue": gigue,
            "pertes": 100 * (len(vals) - len(ok)) / len(vals)}


def duree_en(texte):
    """« 36 days, 15:22:01 » (OPNsense) → « 36 j 15 h »."""
    m = re.match(r"(?:(\d+) days?, )?(\d+):(\d+)", texte or "")
    if not m:
        return texte or "—"
    j, h, mn = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return f"{j} j {h} h" if j else f"{h} h {mn} min"


def metriques(periode):
    t = int(time.time())
    duree, pas = (7 * 86400, 1800) if periode == "1s" else (86400, 300)
    debut = t - duree
    v = A.vue(periode)
    d = RELEVE.donnees
    onglets = {}
    with A.base() as c:
        q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
        # -------------------------------------------------------- OPNsense
        gw = v["passerelle"] or {}
        cibles = (["Box"] if A.BOX_IP else []) + [n for n, _ in A.CIBLES]
        lat = {cible: stats_latence([r["ms"] for r in q("SELECT ms FROM latence WHERE cible=? AND ts>=?", cible, debut)])
               for cible in cibles}
        lat_series = {n: q("SELECT ts/? AS b, avg(ms) AS ms FROM latence WHERE cible=? AND ts>=? GROUP BY b ORDER BY b", pas, n, debut)
                      for n in cibles[:2]}
        res = q("""SELECT reseau, sum(rx) AS rx, sum(tx) AS tx, max(rx*8/dt) AS mrx, max(tx*8/dt) AS mtx FROM debit
                   WHERE ts>=? GROUP BY reseau ORDER BY reseau='WAN' DESC, rx+tx DESC""", debut)
        ms = lambda x: f"{x:.1f} ms" if x is not None else "—"
        fw = v["pare_feu"]
        onglets["opnsense"] = {"nom": "OPNsense", "source": f'Pare-feu et routeur · {gw.get("version", "")}', "sections": [
            K("Passerelle", "état de la machine et de la sortie Internet", [
                ["Mémoire", f'{gw.get("memoire", "—")} %', gw.get("processeur", "")], ["Charge", " · ".join(x.strip() for x in str(gw.get("charge", "—")).split(",")), "1 / 5 / 15 min"],
                ["Allumée depuis", duree_en(gw.get("uptime")), gw.get("nom", "")], ["Connexions suivies", fw.get("etats"), "table d'états"],
                ["IP côté box", gw.get("ip_wan", "—"), f'box {gw.get("passerelle", "")}'], ["Disponibilité Internet", f'{v["internet"]["disponibilite"]} %' if v["internet"]["disponibilite"] is not None else "—", "sur la période"],
                ["Bloqués depuis Internet", fw.get("internet"), "sur la période"], ["Refusés entre réseaux", fw.get("interne"), "sur la période"]]),
            T("Trafic par réseau", "reçu et envoyé par l'interface, débit le plus haut sur une minute",
              ["Réseau", "Reçu", "Envoyé", "Pic reçu", "Pic envoyé"],
              [[r["reseau"], ["o", r["rx"]], ["o", r["tx"]], ["b", r["mrx"]], ["b", r["mtx"]]] for r in res]),
            T("Latence", "mesurée chaque minute (3 pings)", ["Cible", "Moyenne", "Médiane", "95 %", "Max", "Gigue", "Pertes"],
              [[A.BOX if n == "Box" else n, ms(s["moy"]), ms(s["med"]), ms(s["p95"]), ms(s["max"]), ms(s["gigue"]), f'{s["pertes"]:.1f} %'] for n, s in lat.items() if s]),
            B("Pare-feu", "ce qui est bloqué et d'où", [
                ("Ports visés depuis Internet", [[p["cle"], p["n"]] for p in fw["ports"]]),
                ("Adresses qui insistent", [[p["cle"], p["n"]] for p in fw["sources"]]),
                ("Refus entre réseaux", [[f'{r["nom"] or r["src"]} → {r["cible"] or ""}', r["n"]] for r in fw["refus"]])]),
            C("Courbes", "", [
                {"t": "Débit Internet", "u": "b/s", "s": [{"n": "reçu", "p": [[w[0], w[1]] for w in v["series"]["wan"]]},
                                                          {"n": "envoyé", "p": [[w[0], w[2]] for w in v["series"]["wan"]]}]},
                {"t": "Latence", "u": "ms", "s": [{"n": A.BOX if n == "Box" else n, "p": [[r["b"] * pas, r["ms"]] for r in lat_series[n]]}
                                                  for n in lat_series]}]),
            T("Tests de débit", "4 flux vers Cloudflare, toutes les 6 h", ["Date", "Réception", "Envoi", "Ping", "Serveur"],
              [[["d", x["ts"]], ["b", x["down"]], ["b", x["up"]], ms(x["ping"]), x["serveur"]] for x in v["speedtest"]["derniers"]])]}
        # -------------------------------------------------------- Switch
        topo = A.topologie()
        sw = topo["switch"]
        nom_port = {p["port"]: (p["principal"] or {}).get("nom") or ", ".join(a["nom"] for a in p["appareils"]) or p["nom"] or "" for p in topo["ports"]}
        onglets["switch"] = {"nom": "Switch", "source": f'{sw.get("modele") or "Cisco"} · lecture en SSH', "sections": [
            K("Matériel", "", [["Modèle", sw.get("modele") or "—", "Cisco Catalyst"], ["Allumé depuis", sw.get("uptime") or "—", ""],
                               ["Ports actifs", f'{sw.get("ports_actifs") or 0} / {sw.get("ports") or 48}', ""],
                               ["Adresses MAC vues", sum(p["n_macs"] for p in topo["ports"]), "sur les ports actifs"]]),
            T("Ports actifs", "ce qui est branché, vitesse et VLAN", ["Port", "Branché", "État", "Vitesse", "VLAN", "Adresses MAC"],
              [[p["port"], (p["principal"] or {}).get("nom") or ", ".join(a["nom"] for a in p["appareils"]) or p["nom"] or "—", p["etat"],
                f'{p["vitesse"]} Mb/s' if str(p["vitesse"]).isdigit() else p["vitesse"], "tronc" if p["tronc"] else p["vlan"], p["n_macs"]]
               for p in topo["ports"] if p["etat"] == "connected"], droite=[5]),
            T("Erreurs des ports", "depuis le démarrage du switch ; seuls les ports concernés", ["Port", "Branché", "CRC (FCS)", "Réception", "Émission", "Alignement", "Paquets rejetés", "Nouvelles"],
              [[po, nom_port.get(po, "—"), e["fcs"], e["rcv"], e["xmit"], e["align"], e["rejets"], e["hausse"]]
               for po, e in sorted((sw.get("erreurs") or {}).items(), key=lambda x: int(x[0].split("/")[-1])) if any(e[k] for k in ("fcs", "rcv", "xmit", "align", "rejets"))])
            if sw.get("erreurs") else AB("Erreurs des ports", "Pas encore relevées (toutes les 10 min).")]}
        # -------------------------------------------------------- Wi-Fi
        cond, args = A.sql_wifi()
        wifi = q(f"""SELECT coalesce(nom, nom_auto, fabricant, mac) AS nom, ip, reseau, fabricant, derniere_vue FROM appareil
                     WHERE en_ligne=1 AND {cond} ORDER BY reseau, nom""", *args)
        pw = next((p for p in topo["ports"] if p["port"] == A.PORT_WIFI), {})
        wf = CONF["wifi"]
        onglets["wifi"] = {"nom": "Wi-Fi", "source": wf.get("name", "Wi-Fi") + (f" · port {A.PORT_WIFI}" if A.PORT_WIFI else ""), "sections": [
            K("La borne", "", [["Appareils en Wi-Fi", len(wifi), "réseaux Wi-Fi et invités"],
                              ["Lien vers le switch", f'{pw.get("vitesse", "—")} Mb/s', "plafonne le débit du Wi-Fi" if str(pw.get("vitesse")) == "100" else "",
                               "attention" if str(pw.get("vitesse")) == "100" else None],
                              ["Mode", "routeur", "passer en point d'accès pour voir chaque client", "attention"] if wf.get("router_mode")
                              else ["Mode", "point d'accès", "chaque client est visible"]]),
            T("Appareils en Wi-Fi", "", ["Appareil", "IP", "Réseau", "Fabricant"], [[w["nom"], w["ip"], w["reseau"], w["fabricant"] or ""] for w in wifi], droite=[]),
            C("Courbe", "", [{"t": "Appareils connectés", "u": "", "s": [{"n": "tous", "p": [[x[0], x[1]] for x in v["series"]["clients"]]},
                                                                           {"n": "Wi-Fi", "p": [[x[0], x[2]] for x in v["series"]["clients"]]}]}])]}
        # -------------------------------------------------------- DNS
        dn = A.MESURES.dns
        if dn:
            tot = dn.get("num_dns_queries") or 0
            noms = lambda l: [[next(iter(x)), next(iter(x.values()))] for x in l or []]
            onglets["dns"] = {"nom": "DNS", "source": "AdGuard Home · 24 h", "sections": [
                K("Requêtes", "sur 24 h", [["Requêtes", tot, ""], ["Bloquées", dn.get("num_blocked_filtering"), f'{100 * (dn.get("num_blocked_filtering") or 0) / (tot or 1):.1f} %'],
                                           ["Temps de traitement", f'{1000 * (dn.get("avg_processing_time") or 0):.1f} ms', "moyenne"],
                                           ["Recherche sécurisée", dn.get("num_replaced_safesearch", 0), "remplacées"],
                                           ["Navigation sécurisée", dn.get("num_replaced_safebrowsing", 0), "sites dangereux bloqués"]]),
                B("Classements", "", [("Les plus demandés", noms(dn.get("top_queried_domains"))[:8]),
                                      ("Les plus bloqués", noms(dn.get("top_blocked_domains"))[:8]),
                                      ("Qui demande", noms(dn.get("top_clients"))[:8]),
                                      ("Serveurs amont (ms)", [[next(iter(x)), round(1000 * next(iter(x.values())), 1)] for x in dn.get("top_upstreams_avg_time") or []][:5])]),
                C("Courbe", "", [{"t": "Requêtes par heure", "u": "", "s": [
                    {"n": "traitées", "p": [[t - (len(dn.get("dns_queries", [])) - i) * 3600, x] for i, x in enumerate(dn.get("dns_queries", []))]},
                    {"n": "bloquées", "p": [[t - (len(dn.get("blocked_filtering", [])) - i) * 3600, x] for i, x in enumerate(dn.get("blocked_filtering", []))]}]}])]}
        else:
            onglets["dns"] = {"nom": "DNS", "source": "AdGuard", "sections": [AB("DNS", "Statistiques AdGuard indisponibles pour le moment.")]}
        # -------------------------------------------------------- Cloudflare
        cf = d.get("cloudflare")
        if cf:
            onglets["cloudflare"] = {"nom": "Cloudflare", "source": f'Zone {A.ENV.get("CLOUDFLARE_ZONE_NAME") or "Cloudflare"} · 24 h', "sections": [
                K("Trafic passant par Cloudflare", "sous-domaines en mode proxy", [
                    ["Requêtes", cf["requetes"], f'{100 * cf["cache"] / (cf["requetes"] or 1):.0f} % depuis le cache'],
                    ["Données servies", ["o", cf["octets"]], ""], ["Visiteurs uniques", cf["visiteurs"], "heure la plus chargée"],
                    ["Menaces bloquées", cf["menaces"], ""], ["Pages vues", cf["pages"], ""]]),
                B("Classements", "", [("Pays", cf["pays"]), ("Codes de réponse", [[str(k), n] for k, n in cf["codes"]])]),
                C("Courbe", "", [{"t": "Requêtes par heure", "u": "", "s": [
                    {"n": "toutes", "p": [[int(time.mktime(time.strptime(h[0][:19], "%Y-%m-%dT%H:%M:%S"))), h[1]] for h in cf["serie"]]},
                    {"n": "depuis le cache", "p": [[int(time.mktime(time.strptime(h[0][:19], "%Y-%m-%dT%H:%M:%S"))), h[2]] for h in cf["serie"]]}]}])]}
        else:
            onglets["cloudflare"] = {"nom": "Cloudflare", "source": "Zone " + (A.ENV.get("CLOUDFLARE_ZONE_NAME") or "Cloudflare"), "sections": [AB(
                "Statistiques Cloudflare", RELEVE.erreurs.get("cloudflare", "") and
                "La clé d'API Cloudflare n'a pas le droit de lire les statistiques. Dans Cloudflare : Mon profil → Jetons d'API → "
                "modifier le jeton et ajouter « Zone → Analytics → Lecture » pour la zone." if RELEVE.erreurs.get("cloudflare")
                else "Pas encore relevé." if env("CLOUDFLARE_API_TOKEN") else "Cloudflare n'est pas configuré (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID).")]}
        # -------------------------------------------------------- Zoraxy
        zx = d.get("zoraxy")
        onglets["zoraxy"] = {"nom": "Zoraxy", "source": "Reverse proxy" + (f' · {urllib.parse.urlparse(A.ENV["ZORAXY_URL"]).hostname}' if A.ENV.get("ZORAXY_URL") else ""), "sections": [
            K("Règles", "", [["Règles", len(zx), f'{sum(r["actif"] for r in zx)} actives'], ["Réservées au réseau local", sum(r["local"] for r in zx), ""]]),
            T("Sites", "chaque sous-domaine et sa destination", ["Domaine", "Vers", "Accès", "État"],
              [[r["domaine"], r["vers"], "réseau local" if r["local"] else "Internet", "actif" if r["actif"] else "désactivé"] for r in zx], droite=[])]
            if zx else [AB("Zoraxy", "Accès au reverse proxy pas encore donné au tableau de bord.")]}
        # -------------------------------------------------------- Beszel
        bz = d.get("beszel")
        go = lambda x: f"{x:.1f} Go" if x is not None else "—"
        pc = lambda x: f"{x:.0f} %" if x is not None else "—"
        if bz:
            onglets["beszel"] = {"nom": "Beszel", "source": f"{len(bz)} machines suivies par Beszel", "sections": [
                T("Machines", "dernière minute", ["Machine", "Processeur", "Mémoire", "Disque", "Charge 1/5/15", "Réseau ↓ / ↑", "Lecture / écriture", "Temp.", "Allumée depuis"],
                  [[s["nom"], pc(s["cpu"]), f'{pc(s["mem"])} · {go(s["mem_util_go"])}', pc(s["disque"]),
                    " ".join(f"{x:.2f}" for x in s["charge"] or []) or "—", f'{s["net_rx"] or 0:.2f} / {s["net_tx"] or 0:.2f} Mo/s',
                    f'{s["lect"] or 0:.1f} / {s["ecr"] or 0:.1f} Mo/s', f'{s["temp"]:.0f} °C' if s["temp"] else "—", ["s", s["uptime"]]] for s in bz]),
                T("Températures", "chaque capteur", ["Capteur", "Maintenant"],
                  [[f'{s["nom"]} · {k}', f"{val:.0f} °C"] for s in bz for k, val in sorted(s["temps"].items(), key=lambda x: -x[1])[:6]]),
                T("Conteneurs Docker", "les plus gourmands", ["Conteneur", "Machine", "Processeur", "Mémoire"],
                  [[x["nom"], s["nom"], pc(x["cpu"]), f'{x["mem"] or 0:.0f} Mo'] for s in bz for x in s["conteneurs"][:8]]),
                C("Courbes", "", [{"t": "Processeur", "u": "%", "s": [{"n": s["nom"], "p": serie(c, f'{s["nom"]}.cpu', debut, pas)} for s in bz]},
                                  {"t": "Mémoire", "u": "%", "s": [{"n": s["nom"], "p": serie(c, f'{s["nom"]}.mem', debut, pas)} for s in bz]},
                                  {"t": "Température", "u": "°C", "s": [{"n": s["nom"], "p": serie(c, f'{s["nom"]}.temp', debut, pas)} for s in bz]}])]}
        else:
            onglets["beszel"] = {"nom": "Beszel", "source": "Machines", "sections": [AB("Beszel", "Accès à Beszel pas encore donné au tableau de bord.")]}
        # -------------------------------------------------------- Uptime Kuma
        sk = sondes_kuma()
        certs = (d.get("kuma") or {}).get("certificats", {})
        et = {1: "en ligne", 0: "hors ligne", 2: "en attente", 3: "maintenance"}
        onglets["kuma"] = {"nom": "Uptime Kuma", "source": f'{len(sk)} sondes · page d\'état « {CONF["uptime_kuma"]["status_page"]} »', "sections": [
            K("Résumé", "", [["En ligne", f'{sum(s["etat"] == 1 for s in sk)} / {len(sk)}', ""],
                             ["Disponibilité moyenne", f'{100 * statistics.mean([s["dispo"] for s in sk if s["dispo"] is not None] or [0]):.2f} %', "sur 24 h"],
                             ["Réponse moyenne", f'{statistics.mean([s["ping"] for s in sk if s["ping"]] or [0]):.0f} ms', "toutes sondes"],
                             ["Plus lent", max(sk, key=lambda s: s["ping"] or 0)["nom"] if sk else "—", f'{max((s["ping"] or 0) for s in sk) if sk else 0} ms']]),
            T("Chaque sonde", "dernier relevé", ["Service", "Groupe", "État", "Réponse", "Dispo. 24 h", "Certificat", "Message"],
              [[s["nom"], s["groupe"], et.get(s["etat"], "—"), f'{s["ping"]} ms' if s["ping"] is not None else "—",
                f'{100 * s["dispo"]:.2f} %' if s["dispo"] is not None else "—",
                f'{certs[s["nom"]]:.0f} j' if s["nom"] in certs and certs[s["nom"]] >= 0 else "—", s["msg"][:60]] for s in sk], droite=[3, 4, 5])]}
        # -------------------------------------------------------- Proxmox
        pm = d.get("proxmox")
        if pm:
            rs = pm["ressources"]
            nd = [r for r in rs if r["type"] == "node"]
            vm = sorted([r for r in rs if r["type"] in ("qemu", "lxc") and not r.get("template")], key=lambda r: -(r.get("cpu") or 0))
            stk = [r for r in rs if r["type"] == "storage" and r.get("maxdisk")]
            onglets["proxmox"] = {"nom": "Proxmox", "source": f'{", ".join(n["node"] for n in nd)} · API, chaque minute', "sections": [
                T("Nœuds", "", ["Nœud", "État", "Processeur", "Mémoire", "Disque", "Allumé depuis", "Mises à jour"],
                  [[n["node"], n.get("status"), pc(100 * n.get("cpu", 0)), f'{pc(100 * n.get("mem", 0) / (n.get("maxmem") or 1))} de {n.get("maxmem", 0) / 2**30:.0f} Go',
                    pc(100 * n.get("disk", 0) / (n.get("maxdisk") or 1)), ["s", n.get("uptime")],
                    (pm["noeuds"].get(n["node"]) or {}).get("maj", "—")] for n in nd]),
                T("VM et conteneurs", "triés par processeur", ["Nom", "ID", "Nœud", "État", "Processeur", "Mémoire", "Disque", "Réseau ↓ / ↑", "Allumé depuis"],
                  [[r.get("name"), r["vmid"], r["node"], "allumé" if r.get("status") == "running" else "arrêté",
                    pc(100 * (r.get("cpu") or 0)), f'{(r.get("mem") or 0) / 2**30:.1f} / {(r.get("maxmem") or 0) / 2**30:.0f} Go',
                    f'{(r.get("maxdisk") or 0) / 2**30:.0f} Go', [["o", r.get("netin")], ["o", r.get("netout")]], ["s", r.get("uptime")]] for r in vm]),
                C("Courbes", "", [{"t": "Processeur des nœuds", "u": "%", "s": [{"n": n["node"], "p": serie(c, f'{n["node"]}.pcpu', debut, pas)} for n in nd]},
                                  {"t": "Mémoire des nœuds", "u": "%", "s": [{"n": n["node"], "p": serie(c, f'{n["node"]}.pmem', debut, pas)} for n in nd]}])]}
        else:
            onglets["proxmox"] = {"nom": "Proxmox", "source": "Proxmox VE", "sections": [
                T("VM et conteneurs", "d'après les cartes réseau vues sur le réseau", ["Nom", "ID", "Nœud", "État", "IP"],
                  [[x["nom"], x["id"], h, "en ligne" if x["en_ligne"] else "hors ligne", x["ip"] or "—"] for h, l in sorted(topo["hotes"].items()) for x in l], droite=[]),
                AB("Processeur et mémoire des VM", "Proxmox n'est pas configuré : donner un jeton d'API en lecture (rôle PVEAuditor) dans PROXMOX_URL, PROXMOX_TOKEN_ID et PROXMOX_TOKEN_SECRET.")]}
            stk = []
        # -------------------------------------------------------- Stockage
        sect = []
        if bz:
            sect.append(K("Volumes", "remplissage", [[f'{s["nom"]} · {k}', pc(p), "", "attention" if p >= SEUIL_DISQUE else None]
                                                      for s in bz for k, p in [("système", s["disque"] or 0)] + list(s["volumes"].items())]))
        pb = d.get("pbs")
        if pb:
            sect.append(K("Sauvegardes Proxmox (PBS)", "", [["Datastore", pc(100 * pb["utilise"] / (pb["total"] or 1)), f'{pb["libre"] / 2**40:.2f} To libres'],
                                                           ["Réussies sur 7 j", pb["reussies_7j"], ""], ["Échecs sur 7 j", pb["echecs_7j"], "", "attention" if pb["echecs_7j"] else None]]))
            sect.append(T("Dernière sauvegarde de chaque machine", "", ["Machine", "Dernière", "Nombre"],
                          [[g["commentaire"] or g["id"], ["d", g["dernier"]], g["n"]] for g in sorted(pb["groupes"], key=lambda g: g["dernier"] or 0)]))
            if pb["echecs"]:
                sect.append(T("Erreurs de sauvegarde", "sur 7 jours", ["Date", "Machine", "Raison"],
                              [[["d", x["ts"]], x["id"], x["raison"]] for x in sorted(pb["echecs"], key=lambda x: -x["ts"])], droite=[]))
        if stk:
            sect.append(T("Stockages Proxmox", "", ["Stockage", "Nœud", "Utilisé", "Taille"],
                          [[s["storage"], s["node"], pc(100 * s.get("disk", 0) / s["maxdisk"]), f'{s["maxdisk"] / 2**30:.0f} Go'] for s in stk]))
        onglets["stockage"] = {"nom": "Stockage", "source": "NAS, serveurs et sauvegardes", "sections": sect or [
            AB("Stockage", "Beszel et PBS ne sont pas encore branchés sur le tableau de bord.")]}
        # -------------------------------------------------------- Sécurité
        nv = q("""SELECT coalesce(nom, nom_auto, fabricant, mac) AS nom, ip, reseau, premiere_vue FROM appareil
                  WHERE premiere_vue>=? ORDER BY premiere_vue DESC LIMIT 10""", debut)
        onglets["securite"] = {"nom": "Sécurité", "source": "Pare-feu, nouveaux appareils, sauvegardes", "sections": [
            K("Résumé", "sur la période", [["Bloqués depuis Internet", fw.get("internet"), "pare-feu OPNsense"],
                                           ["Refusés entre réseaux", fw.get("interne"), ""], ["Nouveaux appareils", len(nv), ""],
                                           ["Alertes ouvertes", sum(1 for a in alertes() if a["niveau"] != "info"), ""]]),
            T("Nouveaux appareils", "", ["Appareil", "IP", "Réseau", "Vu la première fois"], [[x["nom"], x["ip"] or "—", x["reseau"] or "—", ["d", x["premiere_vue"]]] for x in nv], droite=[]),
            (T(f"Connexions {SSO}", f'{d["auth"]["reussies"]} réussie(s) et {d["auth"]["ratees"]} ratée(s) sur 24 h', ["Quand", "Qui", "Résultat", "Adresse", "Pays"],
               [[["d", x["ts"]], x["qui"], "réussie" if x["ok"] else "ratée", x["ip"] or "—", x["pays"] or "—"] for x in d["auth"]["dernieres"]], droite=[])
             if d.get("auth") else AB(f"Connexions {SSO}", "Authentik n'est pas configuré (AUTHENTIK_URL, AUTHENTIK_TOKEN) : jeton d'un compte de service qui peut lire les événements."))]}
        # -------------------------------------------------------- Applis
        jf, px, js = d.get("jellyfin"), d.get("plex"), d.get("jellyseerr")
        so, ra, li, pr = d.get("sonarr"), d.get("radarr"), d.get("lidarr"), d.get("prowlarr")
        sect = []
        lect = [{**l, "src": "Plex"} for l in (px or {}).get("lectures", [])] + [{**l, "src": "Jellyfin"} for l in (jf or {}).get("lectures", [])]
        if px:
            nb_type = lambda t: sum(b["n"] or 0 for b in px["biblio"] if b["type"] == t)
            sect.append(K("Plex", f'version {px["version"]}', [
                ["En lecture", len(px["lectures"]), f'{sum(l["transcodage"] for l in px["lectures"])} transcodage(s) · {sum(l["distant"] for l in px["lectures"])} hors de la maison'],
                ["Films", nb_type("movie"), ""], ["Séries", nb_type("show"), ""], ["Musique", nb_type("artist"), "artistes"]]))
        if jf:
            sect.append(K("Jellyfin", "", [["En lecture", len(jf["lectures"]), f'{sum(l["transcodage"] for l in jf["lectures"])} transcodage(s)'],
                                           ["Films", jf["films"], ""], ["Séries", jf["series"], f'{jf["episodes"]} épisodes'], ["Albums", jf["albums"], ""]]))
        if px or jf:
            sect.append(T("Lectures en cours", "Plex et Jellyfin", ["Titre", "Où", "Qui", "Appareil", "Avancement", "Débit"],
                          [[l["titre"], l["src"], l["qui"], l["appareil"], f'{l["pct"]} %' if l["pct"] is not None else "—",
                            ["b", l["debit"]] if l.get("debit") else "—"] for l in lect], droite=[4, 5]))
        if js:
            n = js["compte"]
            sect.append(K("Jellyseerr", f'version {js["version"]}' + (" · mise à jour disponible" if js["maj"] else ""), [
                ["À valider", n.get("pending", 0), "", "attention" if n.get("pending") else None], ["Acceptées", n.get("approved", 0), ""],
                ["En cours", n.get("processing", 0), "téléchargement"], ["Disponibles", n.get("available", 0), ""], ["Total", n.get("total", 0), "demandes"]]))
            sect.append(T("Dernières demandes", "", ["Date", "Type", "Qui", "État"],
                          [[["d", x["ts"]], x["type"], x["qui"] or "—", "disponible" if x["dispo"] else x["etat"]] for x in js["demandes"]], droite=[]))
        if so or ra or li:
            k = []
            if so:
                k += [["Séries", so["biblio"]["series"], f'{so["biblio"]["episodes"]} épisodes'], ["Épisodes manquants", so["biblio"]["manquants"], "séries suivies"],
                      ["Cette semaine", so["a_venir"], "épisodes attendus"]]
            if ra:
                k += [["Films", ra["biblio"]["presents"], f'sur {ra["biblio"]["films"]} suivis'], ["Films manquants", ra["biblio"]["manquants"], "déjà sortis"]]
            if li:
                k += [["Albums", li["biblio"]["albums"], f'{li["biblio"]["artistes"]} artistes']]
            k.append(["Bibliothèque", ["o", sum(x["biblio"]["octets"] for x in (so, ra, li) if x)], "sur le disque"])
            sect.append(K("Sonarr, Radarr, Lidarr", "", k))
            file = [[a, f["titre"], f["etat"] + (" · bloqué" if f["alerte"] else ""), ["o", f["reste"]], f["fin"] or "—"]
                    for a, x in (("Sonarr", so), ("Radarr", ra), ("Lidarr", li)) if x for f in x["file"]]
            sect.append(T("File de téléchargement", f'{sum(x["file_n"] for x in (so, ra, li) if x)} élément(s)', ["Appli", "Titre", "État", "Reste", "Fin estimée"], file, droite=[3, 4]))
        if pr:
            sect.append(T("Indexeurs (Prowlarr)", f'version {pr["version"]}', ["Indexeur", "État", "Requêtes", "Échecs", "Réponse", "Récupérés"],
                          [[i["nom"], "en panne" if i["panne"] else ("actif" if i["actif"] else "désactivé"), i["requetes"] or 0, i["echecs"] or 0,
                            f'{i["ms"]} ms' if i["ms"] else "—", i["saisies"] or 0] for i in pr["indexeurs"]]))
        sante_applis = [[a, s["type"], s["msg"]] for a, x in (("Sonarr", so), ("Radarr", ra), ("Lidarr", li), ("Prowlarr", pr)) if x for s in x["sante"]]
        if sante_applis:
            sect.append(T("Contrôles de santé", "signalés par les applis", ["Appli", "Niveau", "Message"], sante_applis, droite=[]))
        for cle, titre in (("qbit", "qBittorrent"), ("seed", "qBittorrent seed et cross-seed")):
            x = d.get(cle)
            if x:
                sect.append(K(titre, "", [["Réception", ["b", 8 * (x["recu"] or 0)], "en ce moment"], ["Envoi", ["b", 8 * (x["envoye"] or 0)], "en ce moment"],
                                          ["Torrents", x["n"], f'{x["cross_seed"]} cross-seed' if x["cross_seed"] else ""],
                                          ["En erreur", x["erreurs"], "", "attention" if x["erreurs"] else None], ["Ratio moyen", f'{x["ratio"]:.2f}', ""]]))
        actifs = [[n, t["nom"], f'{t["pct"]} %', ["b", 8 * (t["recu"] or 0)], ["b", 8 * (t["envoye"] or 0)], f'{t["ratio"]:.2f}' if t["ratio"] is not None else "—"]
                  for n, cle in (("qBittorrent", "qbit"), ("seed", "seed")) for t in (d.get(cle) or {}).get("actifs", [])]
        if d.get("qbit") or d.get("seed"):
            sect.append(T("Torrents actifs", "les plus rapides en ce moment", ["Client", "Torrent", "Fait", "Réception", "Envoi", "Ratio"], actifs, droite=[2, 3, 4, 5]))
        manquent = [n for n, ok in (("qBittorrent", d.get("qbit")), ("qBittorrent seed et cross-seed", d.get("seed"))) if not ok]
        if manquent:
            sect.append(AB(", ".join(manquent), "Le tableau de bord n'a pas encore le droit de lire qBittorrent."))
        onglets["applis"] = {"nom": "Applis", "source": "Médias et téléchargements", "sections": sect}
    # -------------------------------------------------------- Vue d'ensemble
    al = alertes()
    def sante(nom, ok, phrase):
        return [nom, "OK" if ok is True else ("à brancher" if ok is None else "À regarder"), phrase,
                "ok" if ok is True else ("neutre" if ok is None else "attention")]
    kuma_ko = [s for s in sk if s["etat"] != 1]
    # onglets des intégrations non configurées : retirés (rien à montrer, rien à brancher pour l'utilisateur)
    actives = {"opnsense": A.OPNSENSE, "switch": env("CISCO_HOST"), "wifi": bool(A.WIFI or A.PORT_WIFI), "dns": env("ADGUARD_URL"),
               "cloudflare": env("CLOUDFLARE_API_TOKEN"), "zoraxy": env("ZORAXY_URL"), "beszel": env("BESZEL_URL"), "kuma": env("KUMA_URL"),
               "proxmox": bool(pm or topo["hotes"]), "stockage": bool(bz or pb or stk), "applis": bool(onglets["applis"]["sections"])}
    for k, ok in actives.items():
        if not ok:
            onglets.pop(k, None)
    tendances = [{"t": "Débit Internet", "u": "b/s", "s": [{"n": "reçu", "p": [[w[0], w[1]] for w in v["series"]["wan"]]},
                                                       {"n": "envoyé", "p": [[w[0], w[2]] for w in v["series"]["wan"]]}]}] if A.OPNSENSE else []
    tendances.append({"t": "Latence", "u": "ms", "s": [{"n": A.BOX if n == "Box" else n, "p": [[r["b"] * pas, r["ms"]] for r in lat_series[n]]} for n in lat_series]})
    onglets = {"global": {"nom": "Vue d'ensemble", "source": "Un résumé par source", "sections": [
        K("Santé par source", "", [x for x in [
            actives["opnsense"] and sante("OPNsense", "mesures" not in A.COLLECTE.erreurs, f'{fw.get("etats") or 0} connexions · {v["internet"]["disponibilite"]} % de dispo'),
            actives["switch"] and sante("Switch", bool(sw.get("ports_actifs")), f'{sw.get("ports_actifs") or 0} ports actifs'),
            actives["wifi"] and sante("Wi-Fi", not CONF["wifi"].get("router_mode"),
                                      f"{len(wifi)} appareils" + (" · borne en mode routeur" if CONF["wifi"].get("router_mode") else "")),
            actives["dns"] and sante("DNS", bool(dn), f'{(dn or {}).get("num_dns_queries", 0)} requêtes'),
            actives["cloudflare"] and sante("Cloudflare", True if cf else None, f'{cf["requetes"]} requêtes' if cf else "pas encore relevé"),
            actives["zoraxy"] and sante("Zoraxy", True if zx else False, f"{len(zx)} règles" if zx else "injoignable"),
            actives["beszel"] and sante("Beszel", (all(s["etat"] == "up" for s in bz)) if bz else False, f"{len(bz)} machines" if bz else "injoignable"),
            actives["kuma"] and sante("Uptime Kuma", not kuma_ko if sk else None, f'{len(sk) - len(kuma_ko)} / {len(sk)} en ligne'),
            env("PROXMOX_URL") and sante("Proxmox", True if pm else False, f'{len([r for r in pm["ressources"] if r["type"] == "node"])} nœuds' if pm else "injoignable"),
            env("PBS_URL") and sante("Sauvegardes", (not pb["echecs_7j"]) if pb else False, f'{pb["reussies_7j"]} en 7 j' if pb else "injoignable")] if x]),
        T("Alertes", "triées par gravité", ["Gravité", "Quoi", "Détail", "Source"], [[a["niveau"], a["titre"], a["detail"], a["source"]] for a in al], droite=[]),
        C("Tendances", "", tendances + [{"t": "Appareils connectés", "u": "", "s": [{"n": "tous", "p": [[x[0], x[1]] for x in v["series"]["clients"]]}]}])]}, **onglets}
    return {"maintenant": t, "periode": periode, "onglets": onglets, "alertes": al,
            "n_alertes": sum(1 for a in al if a["niveau"] != "info")}
