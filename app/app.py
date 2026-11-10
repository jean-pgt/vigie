"""Vigie — la liste de tout ce qui est branché sur le réseau, façon « clients » UniFi, et un tableau de bord du homelab.

Menus Services, Appareils, Métriques : voir tableau.py. Configuration : config.yaml et variables d'environnement
(voir config.example.yaml et .env.example) ; chaque intégration est optionnelle.

Sources, relues en tâche de fond :
  - table ARP locale et balayage ping des réseaux configurés (toujours) ;
  - OPNsense (API, optionnel) : table ARP de tous les VLAN (IP, MAC, réseau, fabricant) et baux DHCP (nom d'hôte) ;
    le balayage ping oblige OPNsense à apprendre l'adresse MAC de chaque appareil présent,
    même de ceux qui ne répondent pas au ping (la résolution ARP a lieu avant) ;
  - switch Cisco IOS (SSH, lecture seule, optionnel) : sur quel port chaque MAC est branchée ;
  - Proxmox VE (jeton d'API en lecture, optionnel) : nom des VM et conteneurs d'après leur MAC ;
  - DNS inverse en dernier recours pour le nom.
Un nouvel appareil (MAC jamais vue) déclenche un mail. Noms, types et « connu » se règlent depuis la page.

Vue d'ensemble (façon tableau de bord UniFi), relevée chaque minute :
  - débit de chaque réseau (compteurs des interfaces d'OPNsense) ;
  - consommation par appareil et par destination Internet : « top talkers » d'OPNsense, répartis au prorata
    des compteurs de l'interface (une estimation, pas une comptabilité exacte) ;
  - latence vers Cloudflare, Google et Quad9 (fping) et santé de la connexion ;
  - état des équipements (passerelle, switch, borne Wi-Fi, serveurs) ;
  - DNS : statistiques d'AdGuard Home (compte en lecture) ;
  - pare-feu : journal d'OPNsense, connexions bloquées venant d'Internet ou refusées entre réseaux ;
  - services : une page d'état d'Uptime Kuma ;
  - test de débit (4 flux vers speed.cloudflare.com) toutes les 6 h ou à la demande.
Topologie : façade du switch (état, vitesse, VLAN de chaque port, appareils branchés) et hôtes Proxmox avec leurs VM.

GET  /                      page
GET  /api/vue?p=1j|1s       vue d'ensemble (JSON)
GET  /api/topologie         switch, ports et hôtes (JSON)
GET  /api/crossseed         qBittorrent de seed et principal en direct, par tracker (JSON, page /crossseed)
POST /api/debit/test        lance un test de débit — réservé au groupe administrateur
GET  /api/appareils         liste (JSON)
GET  /api/appareils/<mac>   détail + historique
POST /api/appareils/<mac>   {nom, type, connu, note} — réservé au groupe administrateur (en-tête du proxy)
GET  /api/config            titre, réseaux, nom du SSO (pour les pages)
GET  /healthz
"""

import base64
import sys
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import tableau

LOG = logging.getLogger("vigie")
ENV = os.environ
CONF = config.C
ICI = Path(__file__).parent
BASE = Path(ENV.get("DATA", "/data")) / "appareils.db"
PERIODE = int(ENV.get("PERIODE", "120"))         # relève ARP / baux
PERIODE_LENTE = int(ENV.get("PERIODE_LENTE", "600"))  # balayage ping, switch, Proxmox, DNS inverse
EN_LIGNE = int(ENV.get("EN_LIGNE", "420"))       # vu il y a moins de 7 min = en ligne
RESEAUX = config.BALAYAGE
PORTS_TRONC = set(CONF["switch"].get("trunk_ports") or [])
SANS_TLS = ssl.create_default_context()
SANS_TLS.check_hostname, SANS_TLS.verify_mode = False, ssl.CERT_NONE
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
INTERNES = list(CONF["opnsense"].get("interfaces") or ["lan"])  # interfaces internes d'OPNsense (lan, opt1…)
PORT_WIFI = CONF["switch"].get("wifi_port") or ""  # port du switch où est branchée la borne Wi-Fi
CIBLES = [(c["name"], c["ip"]) for c in CONF["latency_targets"]]
BOX = CONF["modem"].get("name") or "Box"
BOX_IP = CONF["modem"].get("ip") or ""
WIFI = config.NOMS_WIFI
OPNSENSE = bool(ENV.get("OPNSENSE_URL") and ENV.get("OPNSENSE_KEY"))
# Nom inverse (DNS) d'une destination Internet → nom lisible
DESTINATIONS = [(r"1e100\.net|google|youtube|googlevideo", "Google"), (r"facebook|fbcdn|tfbnw|instagram|whatsapp", "Meta"),
                (r"akamai", "Akamai"), (r"amazonaws|cloudfront|\baws\b|amazon", "Amazon"), (r"cloudflare", "Cloudflare"),
                (r"apple|aaplimg|icloud", "Apple"), (r"netflix|nflx", "Netflix"), (r"microsoft|msedge|azure|windows", "Microsoft"),
                (r"plex", "Plex"), (r"spotify", "Spotify"), (r"valve|steam", "Steam"), (r"fastly", "Fastly"),
                (r"twitch|justin\.tv", "Twitch"), (r"proxad|free\.fr", "Free"), (r"ovh", "OVH"), (r"hetzner|your-server", "Hetzner"),
                (r"datapacket", "DataPacket"), (r"orange|wanadoo", "Orange"), (r"sfr", "SFR"), (r"bbox|bouygues", "Bouygues")]
TYPES = ["inconnu", "serveur", "vm", "conteneur", "reseau", "ordinateur", "telephone", "tablette", "tv", "console",
         "iot", "imprimante", "camera", "enceinte"]

# Fabricant → type probable (le type se corrige à la main depuis la page)
DEVINE = [(r"proxmox", "vm"), (r"cisco|ubiquiti|tp-?link|netgear|freebox|sagemcom|arcadyan|mikrotik|cudy|asus|d-link|zyxel|aruba|ruckus", "reseau"),
          (r"\blg\b|lg electronics|samsung electronics.*tv|sony|tcl|hisense|philips|vizio|roku", "tv"),
          (r"nintendo|sony interactive|microsoft.*xbox", "console"),
          (r"espressif|tuya|shelly|xiaomi|aqara|signify|ikea|sonoff|nest|ring|tado|netatmo", "iot"),
          (r"hp inc|hewlett|epson|brother|canon", "imprimante"), (r"sonos|bose|google.*home|amazon", "enceinte"),
          (r"apple", "telephone"), (r"asrock|asustek|gigabyte|micro-star|msi|intel corporate|dell|lenovo", "ordinateur"),
          (r"synology|qnap|super micro|supermicro", "serveur")]


def base():
    c = sqlite3.connect(BASE, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def initialiser():
    BASE.parent.mkdir(parents=True, exist_ok=True)
    with base() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS appareil (
            mac TEXT PRIMARY KEY, ip TEXT, reseau TEXT, vlan INTEGER, fabricant TEXT,
            nom_auto TEXT, nom TEXT, type TEXT, connu INTEGER DEFAULT 0, note TEXT,
            port TEXT, port_desc TEXT, source TEXT,
            premiere_vue INTEGER, derniere_vue INTEGER, en_ligne INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS evenement (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mac TEXT, ts INTEGER, type TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS evenement_mac ON evenement(mac, ts);
        CREATE TABLE IF NOT EXISTS debit (ts INTEGER, reseau TEXT, rx REAL, tx REAL, dt REAL);
        CREATE INDEX IF NOT EXISTS debit_ts ON debit(ts);
        CREATE TABLE IF NOT EXISTS latence (ts INTEGER, cible TEXT, ms REAL);
        CREATE INDEX IF NOT EXISTS latence_ts ON latence(ts);
        CREATE TABLE IF NOT EXISTS presence (ts INTEGER PRIMARY KEY, total INTEGER, wifi INTEGER);
        CREATE TABLE IF NOT EXISTS conso (heure INTEGER, mac TEXT, rx REAL, tx REAL, PRIMARY KEY (heure, mac));
        CREATE TABLE IF NOT EXISTS destination (heure INTEGER, nom TEXT, rx REAL, tx REAL, PRIMARY KEY (heure, nom));
        CREATE TABLE IF NOT EXISTS speedtest (ts INTEGER PRIMARY KEY, down REAL, up REAL, ping REAL, serveur TEXT);
        CREATE TABLE IF NOT EXISTS pare_feu (heure INTEGER, genre TEXT, src TEXT, dst TEXT, port TEXT, proto TEXT, n INTEGER,
            PRIMARY KEY (heure, genre, src, dst, port, proto));
        """)


# ------------------------------------------------------------------ sources

def opnsense(chemin):
    auth = base64.b64encode(f'{ENV["OPNSENSE_KEY"]}:{ENV["OPNSENSE_SECRET"]}'.encode()).decode()
    req = urllib.request.Request(f'{ENV["OPNSENSE_URL"].rstrip("/")}/api/{chemin}', headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=20, context=SANS_TLS) as r:
        return json.load(r)


def opnsense_curl(chemin):
    """Pour les grosses réponses : urllib s'arrête à 64 Ko sur l'encodage « chunked » d'OPNsense, curl lit tout.
    Identifiants passés sur l'entrée standard (pas dans la ligne de commande)."""
    conf = f'user = "{ENV["OPNSENSE_KEY"]}:{ENV["OPNSENSE_SECRET"]}"\n'
    r = subprocess.run(["curl", "-sfk", "--max-time", "30", "-K", "-", f'{ENV["OPNSENSE_URL"].rstrip("/")}/api/{chemin}'],
                       input=conf, capture_output=True, text=True, timeout=40)
    if r.returncode:
        raise RuntimeError(f"curl {r.returncode}")
    return json.loads(r.stdout)


def propre(nom):
    nom = (nom or "").strip()
    return "" if nom in ("*", "?") else nom


def arp_et_baux():
    if not OPNSENSE:
        return {}, {}
    vus = {}
    for r in opnsense("diagnostics/interface/get_arp"):
        mac = (r.get("mac") or "").lower()
        if not MAC_RE.match(mac) or r.get("expired") or r.get("permanent"):
            continue  # « permanent » = les adresses d'OPNsense lui-même
        vus[mac] = {"ip": r.get("ip"), "reseau": r.get("intf_description") or r.get("intf"),
                    "fabricant": r.get("manufacturer") or "", "nom_auto": propre(r.get("hostname"))}
    baux = {}
    for r in opnsense("dnsmasq/leases/search").get("rows", []):
        mac = (r.get("hwaddr") or "").lower()
        if MAC_RE.match(mac):
            baux[mac] = {"ip": r.get("address"), "nom": propre(r.get("hostname")), "reseau": r.get("if_descr"),
                         "fabricant": r.get("mac_info") or ""}
    return vus, baux


def voisins():
    """Table ARP locale (réseau(x) où tourne Vigie) : ces voisins parlent directement à Vigie pendant le balayage,
    le routeur ne les voit pas forcément. Sans OPNsense, c'est la seule source d'adresses MAC."""
    res = {}
    for n in json.loads(subprocess.run(["ip", "-j", "neigh"], capture_output=True, text=True, timeout=10).stdout or "[]"):
        mac = (n.get("lladdr") or "").lower()
        if MAC_RE.match(mac) and not set(n.get("state", [])) & {"FAILED", "INCOMPLETE"} and connu_ip(n.get("dst")):
            res[mac] = n["dst"]
    return res


OUI = {}


def charger_oui():
    """Base des fabricants (fichier « manuf » de Wireshark, copié à la construction de l'image)."""
    try:
        for l in open(ICI / "manuf", encoding="utf-8", errors="replace"):
            p = l.rstrip("\n").split("\t")
            if len(p) >= 2 and len(p[0]) == 8:
                OUI[p[0].lower()] = p[2] if len(p) > 2 and p[2] else p[1]
    except OSError:
        LOG.warning("base des fabricants absente")


def fabricant_oui(mac):
    return OUI.get(mac[:8], "")


def balayer():
    for r in RESEAUX:
        subprocess.run(["fping", "-q", "-r", "0", "-t", "150", "-i", "2", "-g", r],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def cisco():
    """MAC → (port, description du port, vlan). Les ports de tronc (vers les pve, la box) sont ignorés :
    les MAC qui y passent sont des VM ou des appareils derrière, sans intérêt pour « où est-il branché »."""
    if not ENV.get("CISCO_HOST"):
        return {}
    ssh = ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={BASE.parent / 'known_hosts'}",
           "-o", "KexAlgorithms=+diffie-hellman-group14-sha1", "-o", "HostKeyAlgorithms=+ssh-rsa",
           "-o", "ConnectTimeout=8", f'{ENV["CISCO_USER"]}@{ENV["CISCO_HOST"]}']
    env = {**os.environ, "SSHPASS": ENV["CISCO_PASSWORD"]}
    lance = lambda c: subprocess.run(ssh + [c], capture_output=True, text=True, timeout=60, env=env).stdout.replace("\r", "")
    desc = {}
    for l in lance("show interfaces description").splitlines():
        m = re.match(r"^(Gi\S+|Te\S+)\s+(?:admin down|up|down)\s+(?:up|down)\s*(.*)$", l)
        if m:
            desc[m.group(1)] = m.group(2).strip()
    ports = [l for l in lance("show interfaces status").splitlines() if l.startswith("Gi") and "Not Present" not in l]
    detail = []
    for l in ports:
        m = re.match(r"^(Gi\S+)\s+(.*?)\s*(connected|notconnect|disabled|err-disabled|inactive|monitoring)\s+(\S+)\s+(\S+)\s+(\S+)", l)
        if m:
            detail.append({"port": m.group(1), "nom": desc.get(m.group(1), m.group(2).strip()), "etat": m.group(3),
                           "vlan": m.group(4), "vitesse": m.group(6).replace("a-", "")})
    version = lance("show version | include uptime|Model number")
    m = re.search(r"uptime is (.+)", version)
    SWITCH.update(ports_actifs=sum(1 for p in ports if " connected " in p), ports=len(ports),
                  uptime=m.group(1).strip() if m else "", modele=(re.search(r"Model number\s*:\s*(\S+)", version) or [None, ""])[1],
                  ts=int(time.time()))
    res, par_port = {}, {}
    for l in lance("show mac address-table dynamic").splitlines():
        m = re.match(r"^\s*(\d+)\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+DYNAMIC\s+(\S+)", l)
        if m:
            h = m.group(2).replace(".", "")
            mac = ":".join(h[i:i + 2] for i in range(0, 12, 2))
            par_port.setdefault(m.group(3), []).append(mac)
            if m.group(3) not in PORTS_TRONC:
                res[mac] = (m.group(3), desc.get(m.group(3), ""), int(m.group(1)))
    for d in detail:
        d["macs"] = sorted(set(par_port.get(d["port"], [])))
        d["tronc"] = d["vlan"] == "trunk" or d["port"] in PORTS_TRONC
    SWITCH["detail"] = detail
    # compteurs d'erreurs depuis le démarrage du switch ; « hausse » = erreurs apparues depuis le relevé précédent
    avant = {k: v for k, v in (SWITCH.get("erreurs") or {}).items()}
    err = {}
    for l in lance("show interfaces counters errors").splitlines():
        m = re.match(r"^(Gi\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", l)
        if m:
            v = dict(zip(("align", "fcs", "xmit", "rcv", "court", "rejets"), map(int, m.groups()[1:])))
            v["hausse"] = sum(v[k] - (avant.get(m.group(1)) or {}).get(k, v[k]) for k in ("align", "fcs", "xmit", "rcv")) if avant else 0
            err[m.group(1)] = v
    if err:
        SWITCH["erreurs"] = err
    return res


SWITCH = {}  # état du switch, relevé avec la table MAC


def proxmox():
    """MAC → (nom, « vm 123 · nœud », type), lu dans l'API Proxmox par tableau.py ; None si Proxmox n'est pas configuré."""
    return tableau.proxmox_macs()


def dns_inverse(ips):
    def un(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0].split(".")[0]
        except Exception:
            return ip, ""
    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        return dict(ex.map(un, ips))


# ------------------------------------------------------------------ collecte

def devine_type(fabricant, mac):
    if int(mac[1], 16) & 2:
        return "telephone"  # adresse « privée » aléatoire : téléphones et tablettes, presque toujours
    for motif, t in DEVINE:
        if re.search(motif, fabricant or "", re.I):
            return t
    return "inconnu"


def vlan_de(ip):
    r = config.reseau_de(ip)
    return r["vlan"] if r else None


def reseau_ip(ip):
    r = config.reseau_de(ip)
    return r["name"] if r else CONF["local_network"]


def connu_ip(ip):
    """Adresse à suivre : dans un réseau configuré, ou privée si aucun réseau n'est décrit."""
    if config.RESEAUX:
        return config.reseau_de(ip) is not None
    return prive(ip)


def sql_wifi(col_port="port", col_reseau="reseau"):
    """Condition SQL « appareil en Wi-Fi » : branché derrière la borne, ou dans un réseau marqué wifi."""
    cond = [f"{col_port}=?"] if PORT_WIFI else []
    if WIFI:
        cond.append(f"{col_reseau} IN ({','.join('?' * len(WIFI))})")
    return f"({' OR '.join(cond) or '0'})", ([PORT_WIFI] if PORT_WIFI else []) + WIFI


class Collecte:
    def __init__(self):
        self.lent = {"cisco": {}, "proxmox": {}, "dns": {}}
        self.derniere_lente = 0
        self.erreurs = {}
        self.maj = 0

    def passe(self):
        maintenant = int(time.time())
        if maintenant - self.derniere_lente >= PERIODE_LENTE:
            for nom, f in [("balayage", balayer), ("cisco", cisco), ("proxmox", proxmox)]:
                try:
                    r = f()
                    if r is not None:
                        self.lent[nom] = r
                    self.erreurs.pop(nom, None)
                except Exception as e:
                    self.erreurs[nom] = str(e)[:200]
                    LOG.warning("%s : %s", nom, e)
            self.derniere_lente = maintenant
        vus, baux = arp_et_baux()
        for mac, b in baux.items():  # bail actif sans entrée ARP : appareil peut-être parti, on ne le compte pas présent
            if mac in vus and not vus[mac]["nom_auto"]:
                vus[mac]["nom_auto"] = b["nom"]
        for mac, ip in voisins().items():
            vus.setdefault(mac, {"ip": ip, "reseau": reseau_ip(ip), "fabricant": "", "nom_auto": ""})
        for mac, (port, pdesc, vlan) in self.lent["cisco"].items():
            vus.setdefault(mac, {"ip": None, "reseau": None, "fabricant": "", "nom_auto": ""})
        for mac, v in vus.items():
            v["fabricant"] = v["fabricant"] or fabricant_oui(mac)
        if maintenant - self.lent.get("dns_ts", 0) >= PERIODE_LENTE:
            self.lent["dns"] = dns_inverse([v["ip"] for v in vus.values() if v["ip"]])
            self.lent["dns_ts"] = maintenant
        nouveaux = []
        with base() as c:
            vide = c.execute("SELECT count(*) FROM appareil").fetchone()[0] == 0
            avant = {r["mac"]: r for r in c.execute("SELECT * FROM appareil")}
            for mac, v in vus.items():
                pve = self.lent["proxmox"].get(mac)
                port = self.lent["cisco"].get(mac)
                nom_auto = v["nom_auto"] or (baux.get(mac) or {}).get("nom") or (pve[0] if pve else "") \
                    or self.lent["dns"].get(v["ip"] or "", "")
                fab = v["fabricant"] or (baux.get(mac) or {}).get("fabricant") or ""
                source = pve[1] if pve else ""
                a = avant.get(mac)
                if a is None:
                    # premier inventaire : tout ce qui est déjà là est considéré comme connu
                    c.execute("""INSERT INTO appareil(mac, ip, reseau, vlan, fabricant, nom_auto, type, port, port_desc, source,
                                 premiere_vue, derniere_vue, en_ligne, connu) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                              (mac, v["ip"], v["reseau"], vlan_de(v["ip"]) if v["ip"] else (port[2] if port else None), fab,
                               nom_auto, pve[2] if pve else devine_type(fab, mac), port[0] if port else None,
                               port[1] if port else None, source, maintenant, maintenant, 1 if vide else 0))
                    c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)",
                              (mac, maintenant, "nouveau", v["ip"] or ""))
                    if not vide:
                        nouveaux.append({"mac": mac, "ip": v["ip"], "reseau": v["reseau"], "fabricant": fab, "nom": nom_auto,
                                         "port": port[0] if port else ""})
                else:
                    if not a["en_ligne"]:
                        c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)",
                                  (mac, maintenant, "connexion", v["ip"] or a["ip"] or ""))
                    elif v["ip"] and a["ip"] and v["ip"] != a["ip"]:
                        c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)",
                                  (mac, maintenant, "ip", f'{a["ip"]} → {v["ip"]}'))
                    c.execute("""UPDATE appareil SET ip=coalesce(?, ip), reseau=coalesce(?, reseau), vlan=coalesce(?, vlan),
                                 fabricant=CASE WHEN ?<>'' THEN ? ELSE fabricant END,
                                 nom_auto=CASE WHEN ?<>'' THEN ? ELSE nom_auto END,
                                 port=coalesce(?, port), port_desc=coalesce(?, port_desc),
                                 source=CASE WHEN ?<>'' THEN ? ELSE source END, derniere_vue=?, en_ligne=1 WHERE mac=?""",
                              (v["ip"], v["reseau"], vlan_de(v["ip"]) if v["ip"] else None, fab, fab, nom_auto, nom_auto,
                               port[0] if port else None, port[1] if port else None, source, source, maintenant, mac))
            for r in c.execute("SELECT mac, ip FROM appareil WHERE en_ligne=1 AND derniere_vue < ?", (maintenant - EN_LIGNE,)).fetchall():
                c.execute("UPDATE appareil SET en_ligne=0 WHERE mac=?", (r["mac"],))
                c.execute("INSERT INTO evenement(mac, ts, type, detail) VALUES (?,?,?,?)", (r["mac"], maintenant, "deconnexion", r["ip"] or ""))
            c.execute("DELETE FROM evenement WHERE ts < ?", (maintenant - 90 * 86400,))
        self.maj = maintenant
        for n in nouveaux:
            alerter(n)

    def boucle(self):
        while True:
            try:
                self.passe()
                self.erreurs.pop("collecte", None)
            except Exception as e:
                self.erreurs["collecte"] = str(e)[:200]
                LOG.exception("collecte")
            time.sleep(PERIODE)


def alerter(n):
    if not ENV.get("SMTP_HOST") or not ENV.get("ALERTE_A"):
        return
    m = EmailMessage()
    m["Subject"] = f'Nouvel appareil sur le réseau : {n["nom"] or n["fabricant"] or n["mac"]}'
    m["From"], m["To"] = ENV["SMTP_FROM"], ENV["ALERTE_A"]
    m.set_content("\n".join([
        "Un appareil jamais vu vient de se connecter.", "",
        f'Nom : {n["nom"] or "—"}', f'Fabricant : {n["fabricant"] or "inconnu"}', f'Adresse IP : {n["ip"] or "—"}',
        f'Réseau : {n["reseau"] or "—"}', f'MAC : {n["mac"]}', f'Port du switch : {n["port"] or "—"}', "",
        f'Détails et nom à lui donner : {CONF["base_url"].rstrip("/")}/appareils' if CONF["base_url"] else ""]))
    try:
        port = int(ENV.get("SMTP_PORT", "465"))
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(ENV["SMTP_HOST"], port, timeout=20) as s:
            if port != 465:
                s.starttls()
            s.login(ENV["SMTP_USER"], ENV["SMTP_PASSWORD"])
            s.send_message(m)
    except Exception as e:
        LOG.warning("mail : %s", e)



# ------------------------------------------------------------------ mesures (vue d'ensemble)

def prive(ip):
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def destination(nom_inverse, ip):
    n = (nom_inverse or "").rstrip(".").lower()
    for motif, lib in DESTINATIONS:
        if re.search(motif, n):
            return lib
    if n and not re.match(r"^[\d.-]+$", n):
        p = n.split(".")
        return ".".join(p[-3:] if len(p[-2]) <= 3 and len(p) > 2 else p[-2:])  # co.uk, com.br…
    return "Non identifié"


def adguard(chemin):
    auth = base64.b64encode(f'{ENV["ADGUARD_USER"]}:{ENV["ADGUARD_PASSWORD"]}'.encode()).decode()
    req = urllib.request.Request(f'{ENV["ADGUARD_URL"].rstrip("/")}/control/{chemin}', headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def services_kuma():
    """Sondes d'une page d'état publique d'Uptime Kuma (KUMA_URL, réglage uptime_kuma.status_page)."""
    if not ENV.get("KUMA_URL"):
        return None
    base_url, slug = ENV["KUMA_URL"].rstrip("/"), CONF["uptime_kuma"]["status_page"]
    lire = lambda c: json.load(urllib.request.urlopen(f"{base_url}/api/status-page/{c}", timeout=10))
    page, battements = lire(slug), lire(f"heartbeat/{slug}")
    groupes = []
    for g in page.get("publicGroupList", []):
        sondes = []
        for m in g.get("monitorList", []):
            hb = (battements["heartbeatList"].get(str(m["id"])) or [{}])[-1]
            sondes.append({"nom": m["name"], "etat": hb.get("status"), "ping": hb.get("ping"), "msg": hb.get("msg") or "",
                           "dispo": battements["uptimeList"].get(f'{m["id"]}_24')})
        groupes.append({"nom": g["name"], "sondes": sondes})
    return groupes


def bruit(r):
    """Diffusion, multicast et lien local : bloqués en permanence, sans intérêt."""
    try:
        src, dst = ipaddress.ip_address(r["src"]), ipaddress.ip_address(r["dst"])
    except ValueError:
        return True
    return (dst.is_multicast or src.is_link_local or dst.is_link_local or r["dst"] == "255.255.255.255"
            or (dst.version == 4 and r["dst"].endswith(".255")))


def latences():
    """Latence moyenne (ms) vers chaque cible, None si aucune réponse."""
    ips = [ip for _, ip in CIBLES] + ([BOX_IP] if BOX_IP else [])
    if not ips:
        return {}
    r = subprocess.run(["fping", "-q", "-C", "3", "-p", "250", "-t", "800", *ips], capture_output=True, text=True, timeout=30)
    res = {}
    for l in r.stderr.splitlines():
        ip, _, v = l.partition(" : ")
        ms = [float(x) for x in v.split() if x != "-"]
        res[ip.strip()] = round(sum(ms) / len(ms), 1) if ms else None
    return res


SITES_CF = {"CDG": "Paris", "MRS": "Marseille", "LYS": "Lyon", "FRA": "Francfort", "AMS": "Amsterdam", "LHR": "Londres",
            "BRU": "Bruxelles", "ZRH": "Zurich", "GVA": "Genève", "MAD": "Madrid", "MXP": "Milan", "YUL": "Montréal"}


class Debit:
    """Test de débit Internet : toutes les 6 h, ou à la demande depuis la page."""
    def __init__(self):
        self.en_cours = False
        self.verrou = threading.Lock()
        self.erreur = None

    def lancer(self):
        if not self.verrou.acquire(blocking=False):
            return False
        threading.Thread(target=self._test, daemon=True).start()
        return True

    def _flux(self, sens, fin, total):
        """Un flux : enchaîne des requêtes de 25 Mo (réception) ou 10 Mo (envoi) jusqu'à l'heure de fin."""
        while time.time() < fin:
            if sens == "down":
                cmd = ["curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{size_download}",
                       "https://speed.cloudflare.com/__down?bytes=25000000"]
            else:
                cmd = ["curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{size_upload}",
                       "--data-binary", "@/tmp/envoi.bin", "https://speed.cloudflare.com/__up"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            with self.verrou_total:
                total[0] += float(r.stdout or 0)

    def _mesure(self, sens, flux=4, duree=8):
        total, fin = [0.0], time.time() + duree
        debut = time.time()
        fils = [threading.Thread(target=self._flux, args=(sens, fin, total)) for _ in range(flux)]
        for f in fils:
            f.start()
        for f in fils:
            f.join()
        return total[0] * 8 / (time.time() - debut)

    def _test(self):
        """Test multi-flux vers Cloudflare (servi depuis le site le plus proche) : speedtest-cli situe parfois mal la ligne
        et choisit des serveurs lointains."""
        self.en_cours = True
        self.verrou_total = threading.Lock()
        try:
            with open("/tmp/envoi.bin", "wb") as f:
                f.write(b"\0" * 10_000_000)
            pings, site = [], ""
            for _ in range(5):
                r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-D", "-", "-w", "%{time_connect}",
                                    "https://speed.cloudflare.com/__down?bytes=0"], capture_output=True, text=True, timeout=10)
                m = re.search(r"cf-ray: \S+-([A-Z]{3})", r.stdout, re.I)
                site = m.group(1) if m else site
                pings.append(float(r.stdout.rsplit("\n", 1)[-1] or 0) * 1000)  # -w s'écrit après les en-têtes
            ping = min(p for p in pings if p > 0)
            down, up = self._mesure("down"), self._mesure("up")
            with base() as c:
                c.execute("INSERT OR REPLACE INTO speedtest VALUES (?,?,?,?,?)",
                          (int(time.time()), down, up, ping, f"Cloudflare · {SITES_CF.get(site, site)}"))
            self.erreur = None
        except Exception as e:
            self.erreur = str(e)[:200]
            LOG.warning("test de débit : %s", e)
        finally:
            self.en_cours = False
            self.verrou.release()

    def boucle(self):
        st = CONF["speedtest"]
        if not st.get("enabled", True) or not float(st.get("every_hours") or 0):
            return  # tests automatiques coupés : seulement à la demande
        time.sleep(120)
        while True:
            with base() as c:
                dernier = c.execute("SELECT max(ts) FROM speedtest").fetchone()[0] or 0
            if time.time() - dernier >= 3600 * float(st["every_hours"]):
                self.lancer()
            time.sleep(600)


class Mesures:
    def __init__(self):
        self.compteurs = None
        self.passerelle = {}
        self.info_ts = 0
        self.interfaces = {}  # vtnet1 → WAN
        self.fw_vus = set()
        self.dns = None
        self.services = None
        self.etats = None

    def infos_passerelle(self):
        sysinfo = opnsense("diagnostics/system/system_information")
        horloge = opnsense("diagnostics/system/system_time")
        mem = opnsense("diagnostics/system/system_resources").get("memory", {})
        lignes = opnsense("interfaces/overview/interfaces_info").get("rows", [])
        self.interfaces = {r["device"]: r.get("description") or r["identifier"] for r in lignes if r.get("device") and r.get("identifier")}
        wan = next((r for r in lignes if r.get("identifier") == "wan"), {})
        gw = next((g for g in opnsense("routes/gateway/status").get("items", []) if g.get("name") in ("WAN_DHCP", "WAN_GW")), {})
        cpu = opnsense("diagnostics/cpu_usage/getcputype")
        self.passerelle = {
            "nom": sysinfo.get("name", "OPNsense"), "version": (sysinfo.get("versions") or [""])[0],
            "uptime": horloge.get("uptime", ""), "charge": horloge.get("loadavg", ""),
            "memoire": round(100 * float(mem.get("used", 0)) / float(mem.get("total") or 1)),
            "ip_wan": (wan.get("addr4") or "").split("/")[0], "passerelle": gw.get("address", ""),
            "processeur": (cpu or [""])[0], "fai": CONF["isp"]}
        self.info_ts = time.time()

    def tour(self):
        t = int(time.time())
        heure = t - t % 3600
        if OPNSENSE and t - self.info_ts > 600:
            try:
                self.infos_passerelle()
            except Exception as e:
                LOG.warning("passerelle : %s", e)
        cpt = {i: (float(v["bytes received"]), float(v["bytes transmitted"]), v.get("name") or i)
               for i, v in opnsense("diagnostics/traffic/interface")["interfaces"].items()} if OPNSENSE else {}
        deltas = {}
        if self.compteurs:
            t0, avant = self.compteurs
            for i, (rx, tx, nom) in cpt.items():
                if i in avant and t > t0 and rx >= avant[i][0] and tx >= avant[i][1]:
                    deltas[i] = (rx - avant[i][0], tx - avant[i][1], nom, t - t0)
        self.compteurs = (t, cpt)
        top = opnsense("diagnostics/traffic/top/" + ",".join(["wan"] + INTERNES)) if deltas else {}
        lat = latences()
        if ENV.get("ADGUARD_URL"):
            try:
                self.dns = adguard("stats")
                COLLECTE.erreurs.pop("adguard", None)
            except Exception as e:
                COLLECTE.erreurs["adguard"] = str(e)[:200]
        try:
            self.services = services_kuma()
            COLLECTE.erreurs.pop("uptime-kuma", None)
        except Exception as e:
            COLLECTE.erreurs["uptime-kuma"] = str(e)[:200]
        nouveaux = []
        try:  # ~2500 lignes couvrent plus d'une minute de journal ; au-delà OPNsense coupe la réponse
            if not OPNSENSE:
                raise LookupError
            self.etats = int(opnsense("diagnostics/firewall/pf_states").get("current", 0))
            journal = [r for r in opnsense_curl("diagnostics/firewall/log?limit=2500") if r.get("action") == "block" and r.get("dir") == "in"]
            vus = {r["__digest__"] for r in journal}
            nouveaux = [r for r in journal if r["__digest__"] not in self.fw_vus] if self.fw_vus else []
            self.fw_vus = vus
            COLLECTE.erreurs.pop("pare-feu", None)
        except LookupError:
            pass
        except Exception as e:
            COLLECTE.erreurs["pare-feu"] = str(e)[:200]
        with base() as c:
            for r in nouveaux:
                if bruit(r):
                    continue
                src = ipaddress.ip_address(r["src"])
                genre = "interne" if src.is_private else "internet"
                if genre == "interne" and self.interfaces.get(r["interface"]) == "WAN":
                    continue  # réseau de la box : seulement de la diffusion
                h = int(time.mktime(time.strptime(r["__timestamp__"][:19], "%Y-%m-%dT%H:%M:%S")))
                c.execute("""INSERT INTO pare_feu VALUES (?,?,?,?,?,?,1) ON CONFLICT(heure, genre, src, dst, port, proto)
                             DO UPDATE SET n=n+1""", (h - h % 3600, genre, r["src"], r["dst"], r.get("dstport") or "", r.get("protoname") or ""))
            for i, (drx, dtx, nom, dt) in deltas.items():
                c.execute("INSERT INTO debit VALUES (?,?,?,?,?)", (t, nom, drx, dtx, dt))
            for nom, ip in CIBLES + ([("Box", BOX_IP)] if BOX_IP else []):
                c.execute("INSERT INTO latence VALUES (?,?,?)", (t, nom, lat.get(ip)))
            ip_mac = {r["ip"]: r["mac"] for r in c.execute("SELECT ip, mac FROM appareil WHERE ip IS NOT NULL ORDER BY derniere_vue")}
            # Destinations Internet : l'octet « reçu » par l'adresse distante est un envoi de la maison
            if "wan" in deltas:
                drx, dtx = deltas["wan"][:2]
                recs = (top.get("wan") or {}).get("records", [])
                s_in, s_out = sum(r["rate_bits_out"] for r in recs) or 1, sum(r["rate_bits_in"] for r in recs) or 1
                par = {}
                for r in recs:
                    d = destination(r.get("rname"), r["address"])
                    a, b = par.get(d, (0, 0))
                    par[d] = (a + drx * r["rate_bits_out"] / s_in, b + dtx * r["rate_bits_in"] / s_out)
                for d, (a, b) in par.items():
                    c.execute("""INSERT INTO destination VALUES (?,?,?,?) ON CONFLICT(heure, nom)
                                 DO UPDATE SET rx=rx+excluded.rx, tx=tx+excluded.tx""", (heure, d, a, b))
            # Appareils : au prorata du trafic de leur interface (in = reçu par l'appareil)
            for i in INTERNES:
                if i not in deltas:
                    continue
                drx, dtx = deltas[i][:2]
                recs = [r for r in (top.get(i) or {}).get("records", []) if r["address"] in ip_mac]
                s_in, s_out = sum(r["rate_bits_in"] for r in recs) or 1, sum(r["rate_bits_out"] for r in recs) or 1
                for r in recs:
                    c.execute("""INSERT INTO conso VALUES (?,?,?,?) ON CONFLICT(heure, mac)
                                 DO UPDATE SET rx=rx+excluded.rx, tx=tx+excluded.tx""",
                              (heure, ip_mac[r["address"]], dtx * r["rate_bits_in"] / s_in, drx * r["rate_bits_out"] / s_out))
            cond, args = sql_wifi()
            n = c.execute(f"SELECT count(*), sum({cond}) FROM appareil WHERE en_ligne=1", args).fetchone()
            c.execute("INSERT OR REPLACE INTO presence VALUES (?,?,?)", (t, n[0], n[1] or 0))
            if t % 3600 < 60:
                for table in ("debit", "latence", "presence"):
                    c.execute(f"DELETE FROM {table} WHERE ts < ?", (t - 8 * 86400,))
                for table in ("conso", "destination", "pare_feu"):
                    c.execute(f"DELETE FROM {table} WHERE heure < ?", (t - 35 * 86400,))

    def boucle(self):
        while True:
            debut = time.time()
            try:
                self.tour()
                COLLECTE.erreurs.pop("mesures", None)
            except Exception as e:
                COLLECTE.erreurs["mesures"] = str(e)[:200]
                LOG.exception("mesures")
            time.sleep(max(5, 60 - (time.time() - debut)))


def fabricant_court(f):
    """« TP-Link Technologies Co., Ltd. » → « TP-Link »."""
    f = re.sub(r"\b(co\.?,?\s*ltd\.?|ltd\.?|inc\.?|incorporation|corporation|corp\.?|sas|gmbh|technology|technologies|"
               r"computer|systems,?|electronics)(?=\W|$)", "", f or "", flags=re.I)
    f = re.sub(r"[\s,.]+$", "", re.sub(r"\s{2,}", " ", f)).strip()
    return f.title() if f.isupper() and len(f) > 4 else f


def equipements(c):
    """Équipements d'infrastructure : la passerelle (OPNsense), puis les appareils de type réseau ou serveur."""
    pm = proxmox() or {}
    invites = {}
    for nom, hote, genre in pm.values():
        n = hote.split("·")[-1].strip()
        invites[n] = invites.get(n, 0) + 1
    p = MESURES.passerelle
    res = [{"cle": "passerelle", "genre": "passerelle", "nom": "OPNsense", "en_ligne": bool(p),
            "ip": urllib.parse.urlparse(ENV["OPNSENSE_URL"]).hostname,
            "modele": p.get("version", "").replace("-amd64", ""), "infos": [["Disponible depuis", p.get("uptime", "")],
                                                                          ["Mémoire", f'{p.get("memoire", "–")} %']]}] if OPNSENSE else []
    for r in c.execute("""SELECT * FROM appareil WHERE type IN ('reseau', 'serveur') AND (nom IS NOT NULL OR en_ligne=1)
                          ORDER BY type, coalesce(nom, nom_auto)"""):
        nom = r["nom"] or r["nom_auto"] or r["fabricant"] or r["mac"]
        n = nom.lower()
        genre = ("switch" if "switch" in n else "box" if "box" in n else "borne" if re.search(r"wi-?fi|borne|point d.acc|access point|\bap\b|unifi|tp-?link", n)
                 else "nas" if "nas" in n else "serveur" if r["type"] == "serveur" else "reseau")
        e = {"cle": r["mac"], "genre": genre, "nom": nom, "en_ligne": bool(r["en_ligne"]), "ip": r["ip"], "modele": fabricant_court(r["fabricant"]),
             "infos": []}
        if genre == "switch" and SWITCH:
            e["modele"] = SWITCH.get("modele") or e["modele"]
            e["infos"] = [["Ports actifs", f'{SWITCH["ports_actifs"]} / {SWITCH["ports"]}'],
                          ["Disponible depuis", re.sub(r", \d+ minutes?$", "", SWITCH.get("uptime", ""))]]
        elif genre == "borne":
            cond, args = sql_wifi()
            n_wifi = c.execute(f"SELECT count(*) FROM appareil WHERE en_ligne=1 AND {cond} AND mac<>?", (*args, r["mac"])).fetchone()[0]
            # borne en mode routeur : ses clients sont cachés derrière elle, on ne les voit pas
            e["infos"] = [["Clients Wi-Fi", str(n_wifi)]] if n_wifi or not PORT_WIFI else [["Port du switch", re.sub(r"^\D+[\d/]*/", "", PORT_WIFI)]]
        elif n in invites:
            e["infos"] = [["VM et conteneurs", str(invites[n])]]
        res.append(e)
    return res


def topologie():
    pm = proxmox() or {}
    with base() as c:
        app = {r["mac"]: dict(r) for r in c.execute("SELECT mac, ip, nom, nom_auto, fabricant, type, en_ligne, reseau FROM appareil")}
    nom = lambda a: a["nom"] or a["nom_auto"] or a["fabricant"] or a["mac"]
    hotes = {}
    for mac, (n, hote, genre) in pm.items():
        h = hote.split("·")[-1].strip()
        a = app.get(mac)
        hotes.setdefault(h, []).append({"nom": n, "genre": genre, "id": hote.split("·")[0].strip(), "mac": mac,
                                        "en_ligne": bool(a and a["en_ligne"]), "ip": a["ip"] if a else None})
    for h, l in hotes.items():  # une VM a une entrée par carte réseau (un routeur virtuel en a plusieurs) : une ligne par VM
        par_id = {}
        for v in l:
            d = par_id.setdefault(v["id"], v)
            d["en_ligne"] = d["en_ligne"] or v["en_ligne"]
            d["ip"] = d["ip"] or v["ip"]
            if v["en_ligne"]:
                d["mac"] = v["mac"]
        hotes[h] = sorted(par_id.values(), key=lambda v: int(re.sub(r"\D", "", v["id"]) or 0))
    ports = []
    for d in SWITCH.get("detail", []):
        connus = [app[m] for m in d.get("macs", []) if m in app]
        # sur un tronc, l'équipement branché est le serveur ou l'équipement réseau ; le reste passe derrière lui
        principal = next((a for a in connus if a["type"] in ("serveur", "reseau")), None) if d["tronc"] else None
        ports.append({**{k: d[k] for k in ("port", "nom", "etat", "vlan", "vitesse", "tronc")}, "n_macs": len(d.get("macs", [])),
                      "principal": {"mac": principal["mac"], "nom": nom(principal), "type": principal["type"]} if principal else None,
                      "appareils": [] if d["tronc"] else [{"mac": a["mac"], "nom": nom(a), "type": a["type"], "en_ligne": bool(a["en_ligne"]),
                                                          "ip": a["ip"]} for a in connus]})
    return {"switch": {k: SWITCH.get(k) for k in ("modele", "uptime", "ports_actifs", "ports", "ts", "erreurs")}, "ports": ports,
            "hotes": hotes, "port_wifi": PORT_WIFI}


def vue(periode):
    t = int(time.time())
    duree, pas = (7 * 86400, 1800) if periode == "1s" else (86400, 300)
    debut = t - duree
    with base() as c:
        q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
        wan = q("""SELECT ts/? AS b, sum(rx)*8/sum(dt) AS rx, sum(tx)*8/sum(dt) AS tx FROM debit
                   WHERE reseau='WAN' AND ts>=? GROUP BY b ORDER BY b""", pas, debut)
        clients = q("SELECT ts/? AS b, avg(total) AS total, avg(wifi) AS wifi FROM presence WHERE ts>=? GROUP BY b", pas, debut)
        reseaux = q("""SELECT reseau, sum(rx) AS rx, sum(tx) AS tx FROM debit WHERE reseau<>'WAN' AND ts>=?
                       GROUP BY reseau ORDER BY rx+tx DESC""", debut)
        volume = c.execute("SELECT sum(rx), sum(tx) FROM debit WHERE reseau='WAN' AND ts>=?", (debut,)).fetchone()
        dests = q("""SELECT nom, sum(rx) AS rx, sum(tx) AS tx FROM destination WHERE heure>=? GROUP BY nom
                     ORDER BY rx+tx DESC""", debut - debut % 3600)
        actifs = q("""SELECT c.mac, sum(c.rx) AS rx, sum(c.tx) AS tx, a.nom, a.nom_auto, a.fabricant, a.type, a.en_ligne, a.ip
                      FROM conso c JOIN appareil a USING(mac) WHERE heure>=? GROUP BY c.mac ORDER BY sum(c.rx+c.tx) DESC LIMIT 10""",
                   debut - debut % 3600)
        # Santé d'Internet : par tranche, part des minutes où au moins une cible a répondu
        tranche = duree // 96
        sante = {r["b"]: r for r in q("""SELECT ts/? AS b, count(*) AS n, sum(ok) AS ok, avg(ms) AS ms FROM
                                          (SELECT ts, max(ms IS NOT NULL) AS ok, avg(ms) AS ms FROM latence
                                           WHERE cible<>'Box' AND ts>=? GROUP BY ts) GROUP BY b""", tranche, debut)}
        dernieres = q("""SELECT cible, ms FROM latence WHERE ts=(SELECT max(ts) FROM latence)""")
        actuel = c.execute("""SELECT rx*8/dt, tx*8/dt FROM debit WHERE reseau='WAN' ORDER BY ts DESC LIMIT 1""").fetchone()
        equip = equipements(c)
        tests = q("SELECT ts, down, up, ping, serveur FROM speedtest ORDER BY ts DESC LIMIT 12")
        h0 = debut - debut % 3600
        fw = lambda genre, col: q(f"""SELECT {col} AS cle, sum(n) AS n FROM pare_feu WHERE genre=? AND heure>=?
                                      GROUP BY cle ORDER BY n DESC LIMIT 6""", genre, h0)
        totaux = dict(c.execute("SELECT genre, sum(n) FROM pare_feu WHERE heure>=? GROUP BY genre", (h0,)).fetchall())
        refus = q("""SELECT p.src, sum(p.n) AS n, a.mac, coalesce(a.nom, a.nom_auto) AS nom, a.type,
                       (SELECT dst || ':' || port FROM pare_feu x WHERE x.genre='interne' AND x.src=p.src AND x.heure>=?
                        GROUP BY dst, port ORDER BY sum(n) DESC LIMIT 1) AS cible
                     FROM pare_feu p LEFT JOIN appareil a ON a.ip=p.src WHERE p.genre='interne' AND p.heure>=?
                     GROUP BY p.src ORDER BY n DESC LIMIT 6""", h0, h0)
        noms_ip = {r["ip"]: r["nom"] for r in q("SELECT ip, coalesce(nom, nom_auto) AS nom FROM appareil WHERE ip IS NOT NULL")}
    total_ok = sum(r["ok"] for r in sante.values())
    total_n = sum(r["n"] for r in sante.values())
    b0 = debut // tranche
    return {
        "maintenant": t, "periode": periode, "pas": pas, "port_wifi": PORT_WIFI, "passerelle": MESURES.passerelle,
        "internet": {
            "actuel": {"rx": actuel[0], "tx": actuel[1]} if actuel else None,
            "latences": [d for d in dernieres if d["cible"] != "Box"],
            "box": next((d["ms"] for d in dernieres if d["cible"] == "Box"), None),
            "disponibilite": round(100 * total_ok / total_n, 2) if total_n else None,
            "sante": [None if b not in sante else round(sante[b]["ok"] / sante[b]["n"], 3) for b in range(b0 + 1, b0 + 97)],
            "max": {"rx": max((w["rx"] or 0 for w in wan), default=0), "tx": max((w["tx"] or 0 for w in wan), default=0)}},
        "series": {"debut": debut - debut % pas, "wan": [[w["b"] * pas, w["rx"], w["tx"]] for w in wan],
                   "clients": [[r["b"] * pas, r["total"], r["wifi"]] for r in clients]},
        "volume": {"rx": volume[0] or 0, "tx": volume[1] or 0},
        "reseaux": reseaux, "destinations": dests, "actifs": actifs, "equipements": equip,
        "pare_feu": {"etats": MESURES.etats, "internet": totaux.get("internet", 0), "interne": totaux.get("interne", 0),
                     "ports": fw("internet", "port || '/' || proto"), "sources": fw("internet", "src"), "refus": refus},
        "dns": resume_dns(MESURES.dns, noms_ip), "services": MESURES.services,
        "speedtest": {"derniers": tests, "en_cours": DEBIT.en_cours, "erreur": DEBIT.erreur}, "erreurs": COLLECTE.erreurs}


def resume_dns(d, noms_ip):
    """Statistiques AdGuard (toujours sur ses dernières 24 h)."""
    if not d:
        return None
    paire = lambda l: [[k, v] for e in l[:6] for k, v in e.items()]
    return {"requetes": d.get("num_dns_queries", 0), "bloquees": d.get("num_blocked_filtering", 0),
            "temps_ms": round(1000 * d.get("avg_processing_time", 0), 1),
            "serie": list(zip(d.get("dns_queries", []), d.get("blocked_filtering", []))),
            "top_bloques": paire(d.get("top_blocked_domains", [])), "top_domaines": paire(d.get("top_queried_domains", [])),
            "clients": [[noms_ip.get(k) or k, v] for k, v in paire(d.get("top_clients", []))],
            "amont": [[k.split("//")[-1].split("/")[0].split(":")[0], round(1000 * v, 1)] for k, v in paire(d.get("top_upstreams_avg_time", []))]}


# ------------------------------------------------------------------ web

COLLECTE = Collecte()
MESURES = Mesures()
DEBIT = Debit()


def page_html(nom):
    """Page HTML avec le titre choisi et la configuration publique (window.VIGIE) injectés."""
    t = (ICI / nom).read_text(encoding="utf-8")
    titre = CONF["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    conf = json.dumps(config.pour_la_page(), ensure_ascii=False).replace("<", "\\u003c")
    t = t.replace("__TITRE__", titre).replace("</head>", f"<script>window.VIGIE = {conf};</script>\n</head>", 1)
    return t.encode()


def ligne(r):
    d = dict(r)
    d["nom_affiche"] = d["nom"] or d["nom_auto"] or ""
    return d


class Page(BaseHTTPRequestHandler):
    server_version = "vigie"

    def log_message(self, *a):
        pass

    def envoyer(self, code, corps, type_="application/json; charset=utf-8", cache="no-store"):
        b = corps if isinstance(corps, bytes) else json.dumps(corps, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        chemin = self.path.split("?")[0]
        pages = {"/appareils": "index.html", "/services": "services.html", "/metriques": "metriques.html", "/crossseed": "crossseed.html"}
        if chemin in ("/", "/index.html"):  # accueil = Services dès que la page existe
            chemin = "/services" if (ICI / "services.html").exists() else "/appareils"
        if chemin == "/crossseed" and not CONF["qbittorrent"]:
            chemin = "/services"
        if chemin in pages and (ICI / pages[chemin]).exists():
            return self.envoyer(200, page_html(pages[chemin]), "text/html; charset=utf-8")
        if chemin == "/s/manifest.webmanifest":
            t = CONF["title"]
            return self.envoyer(200, {"name": t, "short_name": t, "start_url": "/", "display": "standalone", "background_color": "#0b0b0d",
                                      "theme_color": "#0b0b0d", "icons": [{"src": "/s/icone.svg", "sizes": "any", "type": "image/svg+xml"}]},
                                "application/manifest+json")
        m = re.match(r"^/s/([a-z0-9_-]+(?:/[a-z0-9_-]+)?)\.(css|js|svg|png|webmanifest)$", chemin)
        if m and (ICI / "statique" / f"{m.group(1)}.{m.group(2)}").is_file():
            types = {"css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8", "svg": "image/svg+xml",
                     "png": "image/png", "webmanifest": "application/manifest+json"}
            return self.envoyer(200, (ICI / "statique" / f"{m.group(1)}.{m.group(2)}").read_bytes(), types[m.group(2)], "no-cache")
        if chemin in ("/logo.svg", "/favicon.svg"):
            return self.envoyer(200, (ICI / chemin[1:]).read_bytes(), "image/svg+xml", "max-age=86400")
        if chemin == "/healthz":
            return self.envoyer(200, {"ok": True, "maj": COLLECTE.maj, "erreurs": COLLECTE.erreurs})
        if chemin == "/api/config":
            return self.envoyer(200, config.pour_la_page())
        if chemin == "/api/vue":
            return self.envoyer(200, vue("1s" if "p=1s" in self.path else "1j"))
        if chemin == "/api/services":
            return self.envoyer(200, tableau.services())
        if chemin == "/api/metriques":
            return self.envoyer(200, tableau.metriques("1s" if "p=1s" in self.path else "1j"))
        if chemin == "/api/crossseed":
            return self.envoyer(200, tableau.crossseed())
        if chemin == "/api/topologie":
            return self.envoyer(200, topologie())
        if chemin == "/api/appareils":
            with base() as c:
                rows = [ligne(r) for r in c.execute("SELECT * FROM appareil ORDER BY en_ligne DESC, vlan, ip")]
            return self.envoyer(200, {"appareils": rows, "maj": COLLECTE.maj, "erreurs": COLLECTE.erreurs,
                                      "types": TYPES, "maintenant": int(time.time())})
        m = re.match(r"^/api/appareils/([0-9a-f:]{17})$", chemin)
        if m:
            with base() as c:
                r = c.execute("SELECT * FROM appareil WHERE mac=?", (m.group(1),)).fetchone()
                ev = [dict(e) for e in c.execute("SELECT ts, type, detail FROM evenement WHERE mac=? ORDER BY ts DESC LIMIT 50",
                                                 (m.group(1),))]
            return self.envoyer(200, {"appareil": ligne(r), "historique": ev}) if r else self.envoyer(404, {"erreur": "inconnu"})
        self.envoyer(404, {"erreur": "introuvable"})

    def do_POST(self):
        if self.path == "/api/debit/test":
            if not config.admin(self.headers):
                return self.envoyer(403, {"erreur": "réservé aux administrateurs"})
            with base() as c:
                dernier = c.execute("SELECT max(ts) FROM speedtest").fetchone()[0] or 0
            if time.time() - dernier < 600:  # Cloudflare limite les tests rapprochés (429) : résultats faussés
                return self.envoyer(429, {"erreur": "Dernier test il y a moins de 10 min"})
            return self.envoyer(202, {"lance": DEBIT.lancer()})
        m = re.match(r"^/api/appareils/([0-9a-f:]{17})$", self.path)
        if not m:
            return self.envoyer(404, {"erreur": "introuvable"})
        if not config.admin(self.headers):
            return self.envoyer(403, {"erreur": "réservé aux administrateurs"})
        try:
            d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10000)) or b"{}")
        except ValueError:
            return self.envoyer(400, {"erreur": "JSON invalide"})
        champs = {}
        if "nom" in d:
            champs["nom"] = str(d["nom"]).strip()[:80] or None
        if "note" in d:
            champs["note"] = str(d["note"]).strip()[:500] or None
        if d.get("type") in TYPES:
            champs["type"] = d["type"]
        if "connu" in d:
            champs["connu"] = 1 if d["connu"] else 0
        if champs:
            with base() as c:
                c.execute(f'UPDATE appareil SET {", ".join(k + "=?" for k in champs)} WHERE mac=?', (*champs.values(), m.group(1)))
        self.envoyer(200, {"ok": True})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    initialiser()
    if ENV.get("DEMO") == "1":  # données fictives, aucune source contactée (captures d'écran, essai)
        import demo
        demo.lancer(sys.modules[__name__])
        LOG.info("mode démo : http://0.0.0.0:%s", ENV.get("PORT", "8080"))
        return ThreadingHTTPServer((ENV.get("HOST", "0.0.0.0"), int(ENV.get("PORT", "8080"))), Page).serve_forever()
    charger_oui()
    tableau.demarrer(sys.modules[__name__], lambda f: threading.Thread(target=f, daemon=True).start())
    threading.Thread(target=COLLECTE.boucle, daemon=True).start()
    threading.Thread(target=MESURES.boucle, daemon=True).start()
    threading.Thread(target=DEBIT.boucle, daemon=True).start()
    ThreadingHTTPServer((ENV.get("HOST", "0.0.0.0"), int(ENV.get("PORT", "8080"))), Page).serve_forever()


if __name__ == "__main__":
    main()
