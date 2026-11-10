"""Mode démo (DEMO=1) : remplit une base neuve de données entièrement fictives et n'interroge aucune source.

Sert aux captures d'écran du README et à essayer Vigie sans rien brancher. Les noms, adresses (plages privées et
plages réservées à la documentation, RFC 5737), titres et chiffres sont inventés.
"""

import math
import os
import random
import time

import tableau

R = random.Random(42)
MAINTENANT = int(time.time())

# (nom, type, réseau, dernier octet, fabricant, port du switch ou None)
APPAREILS = [
    ("Box Fibre", "reseau", None, None, "Sagemcom", "Gi1/0/48"),
    ("Switch cœur", "reseau", "SERVERS", 2, "Cisco Systems", None),
    ("Point d'accès Wi-Fi", "reseau", "SERVERS", 3, "Ubiquiti", "Gi1/0/12"),
    ("nova", "serveur", "SERVERS", 11, "ASRock Rack", "Gi1/0/1"),
    ("orion", "serveur", "SERVERS", 12, "Super Micro", "Gi1/0/2"),
    ("vega", "serveur", "SERVERS", 13, "Lenovo", "Gi1/0/3"),
    ("nas", "serveur", "SERVERS", 20, "Synology", "Gi1/0/5"),
    ("bureau-tour", "ordinateur", "LAN", 21, "Micro-Star International", "Gi1/0/7"),
    ("portable-camille", "ordinateur", "WIFI", 31, "Intel Corporate", None),
    ("macbook-sam", "ordinateur", "WIFI", 32, "Apple", None),
    ("imprimante", "imprimante", "LAN", 40, "Brother Industries", "Gi1/0/9"),
    ("tv-salon", "tv", "LAN", 50, "LG Electronics", "Gi1/0/14"),
    ("console", "console", "LAN", 51, "Sony Interactive Entertainment", "Gi1/0/15"),
    ("enceinte-cuisine", "enceinte", "WIFI", 52, "Sonos", None),
    ("iphone-camille", "telephone", "WIFI", 60, "", None),
    ("pixel-sam", "telephone", "WIFI", 61, "", None),
    ("tablette", "tablette", "WIFI", 62, "", None),
    ("thermostat", "iot", "IOT", 70, "Netatmo", None),
    ("prise-bureau", "iot", "IOT", 71, "Shelly", None),
    ("prise-salon", "iot", "IOT", 72, "Shelly", None),
    ("ampoules-hue", "iot", "IOT", 73, "Signify", "Gi1/0/18"),
    ("aspirateur", "iot", "IOT", 74, "Espressif", None),
    ("camera-entree", "camera", "IOT", 80, "Reolink", "Gi1/0/20"),
    ("camera-jardin", "camera", "IOT", 81, "Reolink", "Gi1/0/21"),
    ("telephone-invite", "telephone", "GUEST", 100, "", None),
    (None, "inconnu", "IOT", 90, "Tuya Smart", None),
]
# VM et conteneurs Proxmox : (nom, id, nœud, type, en ligne)
INVITES = [
    ("opnsense", 100, "nova", "vm", True), ("adguard", 101, "nova", "conteneur", True), ("zoraxy", 102, "nova", "conteneur", True),
    ("authentik", 103, "nova", "conteneur", True), ("vigie", 104, "nova", "conteneur", True), ("vaultwarden", 105, "nova", "conteneur", True),
    ("medias", 200, "orion", "vm", True), ("immich", 201, "orion", "conteneur", True), ("nextcloud", 202, "orion", "conteneur", True),
    ("home-assistant", 203, "orion", "vm", True), ("paperless", 204, "orion", "conteneur", True),
    ("forgejo", 300, "vega", "conteneur", True), ("wiki", 301, "vega", "conteneur", True), ("backup-server", 302, "vega", "vm", True),
    ("essais", 303, "vega", "vm", False),
]
PREFIXE = {"LAN": "192.168.10", "IOT": "192.168.20", "GUEST": "192.168.30", "SERVERS": "192.168.40", "WIFI": "192.168.50"}
VLAN = {"LAN": 10, "IOT": 20, "GUEST": 30, "SERVERS": 40, "WIFI": 50}


def mac(prive=False):
    o = [R.randrange(256) for _ in range(6)]
    o[0] = (o[0] & 0xFC) | (0x02 if prive else 0)
    return ":".join(f"{x:02x}" for x in o)


def courbe(i, n, base, amp, bruit=0.15, phase=0.0):
    """Valeur qui suit le rythme d'une journée (creux la nuit), avec un peu de bruit."""
    h = ((MAINTENANT - (n - i) * 300) % 86400) / 3600
    jour = 0.5 + 0.5 * math.sin((h - 9 + phase) / 24 * 2 * math.pi)
    return max(0.0, base + amp * jour * (1 + R.uniform(-bruit, bruit)))


def remplir_base(A):
    with A.base() as c:
        if c.execute("SELECT count(*) FROM appareil").fetchone()[0]:
            return  # base déjà remplie (redémarrage)
        macs = {}
        for nom, typ, res, octet, fab, port in APPAREILS:
            m = mac(prive=typ in ("telephone", "tablette") and not fab)
            ip = f"{PREFIXE[res]}.{octet}" if res else "192.168.0.1"
            vu = MAINTENANT - (R.randrange(60, 300) if typ != "imprimante" else 5 * 3600)
            premiere = MAINTENANT - R.randrange(20, 200) * 86400 if nom else MAINTENANT - 3 * 3600
            c.execute("""INSERT INTO appareil(mac, ip, reseau, vlan, fabricant, nom_auto, nom, type, connu, port, port_desc, source,
                         premiere_vue, derniere_vue, en_ligne) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (m, ip, res or "WAN", VLAN.get(res), fab, (nom or "").lower(), nom, typ, 1 if nom else 0, port, None, "",
                       premiere, vu, 0 if typ == "imprimante" else 1))
            macs[nom or "?"] = (m, ip)
            for k in range(R.randrange(1, 4)):
                c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)",
                          (m, MAINTENANT - k * R.randrange(3600, 30000), R.choice(["connexion", "deconnexion"]), ip))
            c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)", (m, premiere, "nouveau", ip))
        pm = {}
        for nom, vid, noeud, typ, en_ligne in INVITES:
            m = "bc:24:11:" + ":".join(f"{R.randrange(256):02x}" for _ in range(3))
            ip = f"192.168.40.{100 + vid % 100 + (vid // 100) * 20}"
            pm[m] = (nom, f'{"vm" if typ == "vm" else "ct"} {vid} · {noeud}', typ)
            c.execute("""INSERT INTO appareil(mac, ip, reseau, vlan, fabricant, nom_auto, nom, type, connu, port, source,
                         premiere_vue, derniere_vue, en_ligne) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                      (m, ip if en_ligne else None, "SERVERS", 40, "Proxmox Server Solutions", nom, None, typ,
                       {"nova": "Gi1/0/1", "orion": "Gi1/0/2", "vega": "Gi1/0/3"}[noeud], pm[m][1],
                       MAINTENANT - 90 * 86400, MAINTENANT - (60 if en_ligne else 4 * 86400), 1 if en_ligne else 0))
            macs[nom] = (m, ip)
        tableau._PM.update(macs=pm, ts=time.time() + 10 ** 9)

        # séries toutes les 5 min sur 7 jours
        n = 7 * 288
        lignes_debit, lignes_lat, lignes_pres, lignes_serie = [], [], [], []
        for i in range(n):
            t = MAINTENANT - (n - i) * 300
            wan_rx, wan_tx = courbe(i, n, 4e6, 120e6), courbe(i, n, 1e6, 18e6)
            if R.random() < 0.01:
                wan_rx *= 6  # un gros téléchargement de temps en temps
            lignes_debit.append((t, "WAN", wan_rx * 300 / 8, wan_tx * 300 / 8, 300))
            for res, part in (("LAN", 0.45), ("WIFI", 0.3), ("SERVERS", 0.35), ("IOT", 0.03), ("GUEST", 0.02)):
                lignes_debit.append((t, res, wan_rx * part * 300 / 8 * R.uniform(.8, 1.2), wan_tx * part * 300 / 8 * R.uniform(.8, 1.2), 300))
            for cible, base in (("Box", 0.6), ("Cloudflare", 4.2), ("Google", 5.1), ("Quad9", 6.3)):
                ms = None if R.random() < 0.002 else round(base + R.expovariate(1 / (base * 0.15)), 1)
                lignes_lat.append((t, cible, ms))
            tot = round(courbe(i, n, 30, 12, 0.05))
            lignes_pres.append((t, tot, round(tot * 0.4)))
            for nom, cpu, mem, temp in (("nova", 9, 48, 44), ("orion", 22, 71, 52), ("vega", 6, 39, 41), ("nas", 7, 33, 38)):
                lignes_serie += [(t, f"{nom}.cpu", courbe(i, n, cpu * .6, cpu, .3)), (t, f"{nom}.mem", mem + R.uniform(-2, 2)),
                                 (t, f"{nom}.temp", temp + courbe(i, n, 0, 6, .2))]
                if nom != "nas":
                    lignes_serie += [(t, f"{nom}.pcpu", courbe(i, n, cpu * .6, cpu, .3)), (t, f"{nom}.pmem", mem + R.uniform(-2, 2))]
        c.executemany("INSERT INTO debit VALUES (?,?,?,?,?)", lignes_debit)
        c.executemany("INSERT INTO latence VALUES (?,?,?)", lignes_lat)
        c.executemany("INSERT OR REPLACE INTO presence VALUES (?,?,?)", lignes_pres)
        c.execute("CREATE TABLE IF NOT EXISTS serie (ts INTEGER, cle TEXT, val REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS serie_cle ON serie(cle, ts)")
        c.executemany("INSERT INTO serie VALUES (?,?,?)", lignes_serie)

        # consommation par appareil et par destination, à l'heure
        gros = [("tv-salon", 9e9, 3e8), ("bureau-tour", 5e9, 2e9), ("medias", 4e9, 6e9), ("macbook-sam", 2.5e9, 6e8),
                ("console", 2e9, 2e8), ("iphone-camille", 9e8, 2e8), ("immich", 3e8, 1.2e9), ("nextcloud", 2e8, 6e8),
                ("pixel-sam", 5e8, 1e8), ("tablette", 4e8, 5e7)]
        dests = [("Netflix", 8e9, 1e8), ("Google", 5e9, 6e8), ("Cloudflare", 3e9, 2e9), ("Akamai", 2.5e9, 1e8), ("Apple", 1.5e9, 3e8),
                 ("Steam", 1.8e9, 5e7), ("Amazon", 9e8, 4e8), ("Microsoft", 6e8, 2e8), ("Non identifié", 1.2e9, 7e8)]
        for h in range(7 * 24):
            heure = MAINTENANT - MAINTENANT % 3600 - h * 3600
            f = 0.3 + 0.7 * max(0, math.sin(((heure % 86400) / 3600 - 9) / 24 * 2 * math.pi))
            for nom, rx, tx in gros:
                c.execute("INSERT INTO conso VALUES (?,?,?,?)", (heure, macs[nom][0], rx / 24 * f * R.uniform(.5, 1.5), tx / 24 * f * R.uniform(.5, 1.5)))
            for nom, rx, tx in dests:
                c.execute("INSERT INTO destination VALUES (?,?,?,?)", (heure, nom, rx / 24 * f * R.uniform(.5, 1.5), tx / 24 * f * R.uniform(.5, 1.5)))
            for _ in range(R.randrange(20, 40)):
                port = R.choice(["22/TCP", "23/TCP", "3389/TCP", "445/TCP", "8080/TCP", "5060/UDP", "443/TCP", "1433/TCP"])
                src = R.choice(["203.0.113.", "198.51.100.", "192.0.2."]) + str(R.choice([7, 14, 33, 58, 91, 120, 177, 201, 230]))
                c.execute("""INSERT INTO pare_feu VALUES (?,?,?,?,?,?,?) ON CONFLICT(heure, genre, src, dst, port, proto)
                             DO UPDATE SET n=n+excluded.n""", (heure, "internet", src, "192.168.0.2", port.split("/")[0], port.split("/")[1], R.randrange(1, 12)))
            for src, dst, port in (("192.168.20.74", "192.168.10.21", "445"), ("192.168.30.100", "192.168.40.20", "5000"),
                                   ("192.168.20.90", "192.168.40.101", "53")):
                if R.random() < 0.6:
                    c.execute("""INSERT INTO pare_feu VALUES (?,?,?,?,?,?,?) ON CONFLICT(heure, genre, src, dst, port, proto)
                                 DO UPDATE SET n=n+excluded.n""", (heure, "interne", src, dst, port, "TCP", R.randrange(1, 6)))
        for k in range(12):
            c.execute("INSERT INTO speedtest VALUES (?,?,?,?,?)", (MAINTENANT - k * 6 * 3600 - 1800, R.uniform(1.62e9, 1.94e9),
                                                                  R.uniform(5.6e8, 6.9e8), R.uniform(3.6, 5.2), "Cloudflare · Paris"))
    return macs


def switch(A, macs):
    branche = {}
    for nom, typ, res, octet, fab, port in APPAREILS:
        if port:
            branche.setdefault(port, []).append(macs[nom or "?"][0])
    for nom, vid, noeud, typ, _ in INVITES:
        branche.setdefault({"nova": "Gi1/0/1", "orion": "Gi1/0/2", "vega": "Gi1/0/3"}[noeud], []).append(macs[nom][0])
    detail = []
    for i in range(1, 49):
        p = f"Gi1/0/{i}"
        tronc = p in A.PORTS_TRONC
        on = p in branche or i in (4, 6)
        detail.append({"port": p, "nom": {1: "nova", 2: "orion", 3: "vega", 48: "box"}.get(i, ""), "etat": "connected" if on else "notconnect",
                       "vlan": "trunk" if tronc else str(R.choice([10, 20, 40]) if on else 1), "vitesse": "100" if i == 21 else "1000",
                       "macs": branche.get(p, []), "tronc": tronc})
    A.SWITCH.update(modele="C9200L-48T-4G", uptime="23 weeks, 4 days, 7 hours, 12 minutes", ports=52, ts=MAINTENANT,
                    ports_actifs=sum(d["etat"] == "connected" for d in detail), detail=detail,
                    erreurs={f"Gi1/0/{i}": {"align": 0, "fcs": f, "xmit": 0, "rcv": f, "court": 0, "rejets": 0, "hausse": h}
                             for i, f, h in ((21, 142, 3), (14, 2, 0))})


def mesures(A):
    A.MESURES.passerelle = {"nom": "OPNsense", "version": "25.7.6-amd64", "uptime": "41 days, 06:12:44", "charge": "0.21, 0.18, 0.15",
                            "memoire": 23, "ip_wan": "192.168.0.2", "passerelle": "192.168.0.1",
                            "processeur": "AMD Ryzen 5 5600U with Radeon Graphics (6 cores, 12 threads)", "fai": A.CONF["isp"]}
    A.MESURES.etats = 1873
    heures = [round(courbe(i, 24, 900, 2600, .2) * 12 / 12) for i in range(24)]
    A.MESURES.dns = {
        "num_dns_queries": sum(heures), "num_blocked_filtering": int(sum(heures) * 0.142), "avg_processing_time": 0.0031,
        "num_replaced_safesearch": 12, "num_replaced_safebrowsing": 3,
        "dns_queries": heures, "blocked_filtering": [int(h * R.uniform(.11, .17)) for h in heures],
        "top_queried_domains": [{"connectivitycheck.gstatic.com": 3920}, {"api.example.net": 2711}, {"time.cloudflare.com": 1980},
                                {"updates.example.org": 1544}, {"cdn.example.com": 1201}, {"push.example.net": 988}, {"weather.example.org": 640}],
        "top_blocked_domains": [{"telemetry.example.com": 1850}, {"ads.example.net": 1322}, {"tracker.example.org": 911},
                                {"metrics.example.io": 604}, {"pixel.example.com": 377}, {"beacon.example.net": 212}],
        "top_clients": [{"192.168.10.50": 5120}, {"192.168.50.60": 3801}, {"192.168.10.21": 3302}, {"192.168.20.73": 2402},
                        {"192.168.50.32": 1950}, {"192.168.40.201": 1200}],
        "top_upstreams_avg_time": [{"https://dns.quad9.net:443/dns-query": 0.0182}, {"https://cloudflare-dns.com:443/dns-query": 0.0124}]}
    sondes = {"Médias": ["Plex", "Jellyfin", "Immich", "Jellyseerr", "Navidrome", "Audiobookshelf"],
              "Téléchargements": ["Sonarr", "Radarr", "Lidarr", "Prowlarr"],
              "Réseau et sécurité": ["OPNsense", "AdGuard Home", "Zoraxy", "Authentik", "Vaultwarden", "WireGuard"],
              "Infrastructure": ["Proxmox", "Backup Server", "Beszel", "Forgejo", "Wiki", "nova", "orion", "vega", "nas"],
              "Maison": ["Home Assistant", "Paperless-ngx", "Nextcloud", "Webmail"]}
    A.MESURES.services = [{"nom": g, "sondes": [{"nom": s, "etat": 2 if s == "Audiobookshelf" else 1, "ping": R.randrange(8, 140),
                                                 "msg": "délai dépassé (48 s)" if s == "Audiobookshelf" else "", "dispo": R.uniform(.993, 1)}
                                                for s in l]} for g, l in sondes.items()]


def releve(A):
    t = MAINTENANT
    d = tableau.RELEVE.donnees
    machine = lambda nom, cpu, mem, disque, temp, coeurs, modele, volumes=None, conteneurs=(): {
        "nom": nom, "etat": "up", "cpu": cpu, "mem": mem, "disque": disque, "charge": [cpu / 25, cpu / 28, cpu / 30],
        "uptime": R.randrange(9, 60) * 86400, "temp": temp, "coeurs": coeurs, "modele": modele, "version": "0.14.1",
        "volumes": volumes or {}, "mem_go": 64, "mem_util_go": 64 * mem / 100, "swap": 0, "disque_go": 1000, "disque_util_go": 10 * disque,
        "net_rx": R.uniform(.2, 8), "net_tx": R.uniform(.1, 3), "lect": R.uniform(0, 30), "ecr": R.uniform(0, 12),
        "temps": {"cpu": temp, "nvme0": temp - 6, "carte mère": temp - 11},
        "conteneurs": [{"nom": n, "cpu": R.uniform(.1, 9), "mem": R.uniform(60, 1900), "net": R.uniform(0, 4e6)} for n in conteneurs]}
    d["beszel"] = [machine("nova", 9.4, 48, 31, 46, 12, "AMD Ryzen 9 7900"), machine("orion", 23.8, 71, 58, 55, 16, "Intel Core i5-13500"),
                   machine("vega", 5.1, 39, 22, 41, 8, "Intel N305"),
                   machine("nas", 6.7, 33, 12, 39, 4, "Intel Celeron J4125", {"volume1": 71, "volume2": 48},
                           ["jellyfin", "plex", "sonarr", "radarr", "lidarr", "prowlarr", "qbittorrent", "navidrome", "immich-ml"])]
    res = []
    for nom, cpu, mem, maxmem in (("nova", .094, 31e9, 64e9), ("orion", .238, 46e9, 64e9), ("vega", .051, 12.5e9, 32e9)):
        res.append({"type": "node", "node": nom, "status": "online", "cpu": cpu, "mem": mem, "maxmem": maxmem, "disk": 2.1e11,
                    "maxdisk": 9.6e11, "uptime": R.randrange(9, 60) * 86400, "maxcpu": 12})
        res.append({"type": "storage", "storage": "local-zfs", "node": nom, "disk": R.uniform(1e11, 6e11), "maxdisk": 9e11})
    for nom, vid, noeud, typ, en_ligne in INVITES:
        res.append({"type": "qemu" if typ == "vm" else "lxc", "vmid": vid, "name": nom, "node": noeud, "status": "running" if en_ligne else "stopped",
                    "cpu": R.uniform(.002, .2) if en_ligne else 0, "mem": R.uniform(.3e9, 6e9) if en_ligne else 0, "maxmem": 8 * 2 ** 30,
                    "maxdisk": 32 * 2 ** 30, "netin": R.uniform(1e8, 9e10), "netout": R.uniform(1e8, 4e10), "uptime": R.randrange(2, 40) * 86400 if en_ligne else 0})
    d["proxmox"] = {"ts": t, "ressources": res, "noeuds": {"nova": {"maj": 0}, "orion": {"maj": 7}, "vega": {"maj": 0}}}
    d["pbs"] = {"total": 8e12, "utilise": 3.1e12, "libre": 4.9e12, "echecs_7j": 1, "reussies_7j": 97,
                "groupes": [{"id": f'{"vm" if typ == "vm" else "ct"}/{vid}', "dernier": t - R.randrange(3, 30) * 3600, "n": R.randrange(14, 40),
                             "commentaire": nom} for nom, vid, _, typ, ok in INVITES if ok],
                "echecs": [{"ts": t - 4 * 86400, "id": "303", "raison": "machine arrêtée pendant la sauvegarde"}]}
    d["jellyfin"] = {"lectures": [{"titre": "Les Marées de Vénus", "qui": "camille", "appareil": "tv-salon", "client": "Jellyfin Android TV",
                                   "transcodage": False, "pct": 62, "pause": False}], "films": 812, "series": 164, "episodes": 6120, "albums": 940}
    d["plex"] = {"version": "1.42.2", "biblio": [{"nom": "Films", "type": "movie", "n": 806}, {"nom": "Séries", "type": "show", "n": 161},
                                                 {"nom": "Musique", "type": "artist", "n": 1204}],
                 "lectures": [{"titre": "Station Boréale · S02E05", "qui": "sam", "appareil": "iPad", "transcodage": True, "pause": False,
                               "pct": 34, "debit": 8.2e6, "distant": True}]}
    d["zoraxy"] = [{"domaine": f"{s}.example.com", "actif": s != "essais", "vers": f"192.168.40.{h}:{p}", "local": s in ("pve", "pbs", "proxy", "essais")}
                   for s, h, p in (("jellyfin", 20, 8096), ("photos", 201, 2283), ("cloud", 202, 443), ("vault", 105, 80), ("auth", 103, 9000),
                                   ("pve", 11, 8006), ("pbs", 202, 8007), ("proxy", 102, 8000), ("wiki", 141, 80), ("git", 140, 3000), ("essais", 143, 80))]
    d["cloudflare"] = {"requetes": 48210, "cache": 30140, "octets": 9.4e9, "octets_cache": 6.1e9, "menaces": 37, "pages": 2210, "visiteurs": 184,
                       "serie": [[time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(t - (24 - i) * 3600)), int(courbe(i, 24, 900, 2600) * 12 / 12),
                                  int(courbe(i, 24, 500, 1600))] for i in range(24)],
                       "pays": [["France", 30120], ["Belgique", 5210], ["Canada", 4102], ["Suisse", 2980], ["Allemagne", 1201], ["États-Unis", 960]],
                       "codes": [[200, 40210], [204, 2210], [301, 1900], [304, 2620], [404, 1110], [403, 160]]}
    d["kuma"] = {"certificats": {"Jellyfin": 61, "Immich": 61, "Nextcloud": 9, "Vaultwarden": 61, "Authentik": 61}}
    file = lambda titres: [{"titre": x, "etat": "downloading", "alerte": False, "taille": 4e9, "reste": R.uniform(2e8, 3e9), "fin": "00:12:40",
                            "client": "qBittorrent"} for x in titres]
    d["sonarr"] = {"version": "4.0.15", "sante": [], "file_n": 2, "file": file(["Station Boréale S02E06", "Le Phare du Nord S01E03"]), "a_venir": 9,
                   "biblio": {"series": 161, "episodes": 6020, "manquants": 41, "octets": 9.1e12}}
    d["radarr"] = {"version": "5.26.2", "sante": [{"type": "warning", "msg": "Un indexeur ne répond plus depuis 6 heures"}], "file_n": 1,
                   "file": file(["Les Marées de Vénus (2024)"]), "a_venir": 2, "biblio": {"films": 830, "presents": 806, "manquants": 11, "octets": 7.4e12}}
    d["lidarr"] = {"version": "2.13.3", "sante": [], "file_n": 0, "file": [], "a_venir": 1, "biblio": {"artistes": 1204, "albums": 2930, "octets": 1.3e12}}
    d["prowlarr"] = {"version": "2.0.5", "sante": [], "indexeurs": [
        {"nom": n, "actif": True, "panne": n == "Delta", "requetes": R.randrange(400, 3000), "echecs": R.randrange(0, 40), "ms": R.randrange(120, 900),
         "saisies": R.randrange(10, 300)} for n in ("Alpha", "Bravo", "Charlie", "Delta")]}
    d["jellyseerr"] = {"version": "2.7.3", "maj": False, "compte": {"pending": 2, "approved": 5, "processing": 1, "available": 214, "total": 236},
                       "demandes": [{"ts": t - k * 40000, "type": "série" if k % 2 else "film", "qui": R.choice(["camille", "sam", "alex"]),
                                     "etat": "en attente" if k < 2 else "acceptée", "dispo": k > 3, "tmdb": None} for k in range(8)]}
    d["auth"] = {"reussies": 23, "ratees": 4, "dernieres": [
        {"ts": t - k * 5400, "ok": k % 6 != 2, "qui": R.choice(["camille", "sam", "alex"]) if k % 6 != 2 else "admin",
         "ip": f"192.168.{R.choice([10, 50])}.{R.randrange(20, 70)}" if k % 6 != 2 else f"203.0.113.{R.randrange(2, 250)}",
         "pays": "FR" if k % 6 != 2 else "NL"} for k in range(14)]}
    iso = ["debian-13.1.0-amd64-netinst.iso", "ubuntu-24.04.3-desktop-amd64.iso", "fedora-workstation-43-x86_64.iso",
           "archlinux-2026.11.01-x86_64.iso", "linuxmint-22.2-cinnamon-64bit.iso", "proxmox-ve_9.1-1.iso"]
    qb = lambda recu, env: {"recu": recu, "envoye": env, "recu_total": 3.2e12, "envoye_total": 9.1e12, "n": 412, "etats": {"uploading": 380, "stalledUP": 30, "downloading": 2},
                            "erreurs": 0, "cross_seed": 260, "ratio": 2.4,
                            "actifs": [{"nom": x, "recu": R.uniform(0, 2e6), "envoye": R.uniform(1e5, 3e6), "pct": 100, "taille": 3e9, "ratio": R.uniform(.5, 9)} for x in iso[:4]]}
    d["qbit"], d["seed"] = qb(4.1e6, 1.2e6), qb(0, 3.4e6)
    tableau.RELEVE.ts = t


def crossseed():
    t = int(time.time())
    iso = ["debian-13.1.0-amd64-netinst.iso", "ubuntu-24.04.3-desktop-amd64.iso", "fedora-workstation-43-x86_64.iso",
           "archlinux-2026.11.01-x86_64.iso", "linuxmint-22.2-cinnamon-64bit.iso", "proxmox-ve_9.1-1.iso", "opnsense-25.7-dvd-amd64.iso",
           "alpine-standard-3.22.2-x86_64.iso", "kali-linux-2026.3-installer-amd64.iso", "rocky-10.0-x86_64-dvd.iso"]
    trackers = ["Alpha", "Bravo", "Charlie", "Delta"]
    t_r = random.Random(t // 2)

    def torrent(i):
        return {"nom": iso[i % len(iso)], "tracker": trackers[i % 4], "envoi": t_r.uniform(2e4, 9e5) if i < 8 else 0, "reception": 0,
                "envoye": t_r.uniform(1e9, 4e10), "taille": t_r.uniform(5e8, 9e9), "ratio": round(t_r.uniform(.3, 12), 2),
                "ajout": t - i * 5200, "etat": "uploading", "categorie": "cross-seed"}

    def client(fact):
        tors = [torrent(i) for i in range(20)]
        return {"envoi": sum(x["envoi"] for x in tors) * fact, "reception": 3.1e5 * fact, "envoye_session": 1.4e11 * fact, "envoye_total": 2.3e13 * fact,
                "recu_total": 4.1e12 * fact, "ratio": "5.61", "torrents": int(1840 * fact), "erreurs": 0, "pairs": int(212 * fact),
                "trackers": [{"tracker": n, "torrents": int(t_r.uniform(200, 700) * fact), "taille": t_r.uniform(1e12, 6e12), "envoye": t_r.uniform(2e12, 9e12),
                              "debit": t_r.uniform(1e5, 1.2e6) * fact, "actifs": t_r.randrange(2, 20)} for n in trackers],
                "actifs": [x for x in tors if x["envoi"]], "recents": tors}
    return {"maintenant": t, "derniere_injection": t - 1700, "clients": {"seed": client(1), "principal": client(.4)}}


def lancer(A):
    """Appelé par app.main() à la place des fils de relève."""
    for k, v in {"OPNSENSE_URL": "https://192.168.40.1", "CISCO_HOST": "192.168.40.2", "ADGUARD_URL": "http://192.168.40.101",
                 "KUMA_URL": "http://192.168.40.142:3001", "BESZEL_URL": "http://192.168.40.143:8090", "PROXMOX_URL": "https://192.168.40.11:8006",
                 "PROXMOX_TOKEN_ID": "demo", "PROXMOX_TOKEN_SECRET": "demo", "PBS_URL": "https://192.168.40.122:8007",
                 "CLOUDFLARE_API_TOKEN": "demo", "CLOUDFLARE_ZONE_NAME": "example.com", "ZORAXY_URL": "http://192.168.40.102:8000"}.items():
        os.environ[k] = v
    A.OPNSENSE = True
    macs = remplir_base(A) or {}
    if not macs:
        with A.base() as c:
            macs = {r["nom"] or r["nom_auto"] or "?": (r["mac"], r["ip"]) for r in c.execute("SELECT * FROM appareil")}
            tableau._PM.update(macs={r["mac"]: (r["nom_auto"], r["source"], r["type"]) for r in c.execute("SELECT * FROM appareil WHERE source<>''")},
                               ts=time.time() + 10 ** 9)
    switch(A, macs)
    mesures(A)
    with A.base() as c:
        c.execute("CREATE TABLE IF NOT EXISTS serie (ts INTEGER, cle TEXT, val REAL)")
    releve(A)
    A.COLLECTE.maj = int(time.time())
    tableau.demarrer(A, lambda f: None)
    tableau.crossseed = crossseed
