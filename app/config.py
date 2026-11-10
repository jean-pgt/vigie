"""Configuration de Vigie : un fichier YAML (structure : réseaux, applis, machines…) et des variables d'environnement
(adresses et secrets des intégrations, voir .env.example). Tout est optionnel : sans fichier, Vigie démarre avec
des valeurs par défaut et n'affiche que ce qu'il sait mesurer seul (balayage du réseau local, latence, test de débit).
"""

import ipaddress
import logging
import os
from pathlib import Path

try:
    import yaml
except ImportError:  # PyYAML absent : configuration par défaut seulement
    yaml = None

LOG = logging.getLogger("vigie")
ENV = os.environ

DEFAUT = {
    "title": "Vigie",
    "base_url": "",
    # Authentification : Vigie se place derrière un reverse proxy qui authentifie (Authentik, Authelia, oauth2-proxy…)
    # et transmet les groupes de l'utilisateur dans un en-tête. Sans groupe administrateur, tout le monde peut modifier.
    "sso": {"name": "SSO", "groups_header": "X-Authentik-Groups", "groups_separator": "|", "admin_group": ""},
    "networks": [],
    "scan": [],
    "local_network": "LAN",
    "latency_targets": [{"name": "Cloudflare", "ip": "1.1.1.1"}, {"name": "Google", "ip": "8.8.8.8"}, {"name": "Quad9", "ip": "9.9.9.9"}],
    "modem": {"name": "Box", "ip": "", "label": ""},
    "isp": "",
    "speedtest": {"enabled": True, "every_hours": 6},
    "switch": {"trunk_ports": [], "wifi_port": ""},
    "wifi": {"name": "Borne Wi-Fi", "router_mode": False},
    "opnsense": {"interfaces": ["lan"]},
    "uptime_kuma": {"status_page": "default"},
    "hosts": [],
    "groups": [],
    "apps": [],
    "qbittorrent": [],
    "trackers": {},
}


def _fusion(base, ajout):
    res = dict(base)
    for k, v in (ajout or {}).items():
        if v is None:  # clé laissée vide (« networks: » suivi de commentaires) : valeur par défaut
            continue
        res[k] = _fusion(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return res


def charger():
    if ENV.get("DEMO") == "1":  # mode démo : toujours le homelab fictif fourni
        chemin = Path(__file__).parent / "demo.yaml"
    else:
        chemin = Path(ENV.get("CONFIG") or "/config/config.yaml")
    lu = {}
    if chemin.is_file():
        if yaml is None:
            LOG.warning("PyYAML absent : %s ignoré", chemin)
        else:
            lu = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    else:
        LOG.info("pas de fichier %s : réglages par défaut", chemin)
    return _fusion(DEFAUT, lu)


C = charger()

# Réseaux : nom (tel que le routeur le nomme), libellé, plage, VLAN, couleur, Wi-Fi ou non
RESEAUX = []
for r in C["networks"]:
    try:
        RESEAUX.append({"name": str(r["name"]), "label": str(r.get("label") or r["name"]), "vlan": r.get("vlan"),
                        "color": r.get("color") or "#71717a", "wifi": bool(r.get("wifi")),
                        "cidr": ipaddress.ip_network(r["cidr"], strict=False) if r.get("cidr") else None})
    except (KeyError, ValueError) as e:
        LOG.warning("réseau ignoré (%s) : %s", e, r)
NOMS_WIFI = [r["name"] for r in RESEAUX if r["wifi"]]
BALAYAGE = [str(x) for x in C["scan"]] or [str(r["cidr"]) for r in RESEAUX if r["cidr"]]


def reseau_de(ip):
    """IP → réseau configuré (dict) ou None."""
    try:
        a = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return None
    return next((r for r in RESEAUX if r["cidr"] and a in r["cidr"]), None)


def pour_la_page():
    """Ce que les pages ont besoin de connaître (aucun secret)."""
    return {"titre": C["title"], "sso": C["sso"]["name"],
            "reseaux": {r["name"]: [r["label"], r["color"]] for r in RESEAUX},
            "vlans": {str(r["vlan"]): r["name"] for r in RESEAUX if r["vlan"] is not None},
            "wifi": NOMS_WIFI, "modem": C["modem"].get("label") or "", "qbittorrent": bool(C["qbittorrent"]),
            "opnsense": bool(ENV.get("OPNSENSE_URL")), "switch": bool(ENV.get("CISCO_HOST")),
            "adguard": bool(ENV.get("ADGUARD_URL")), "kuma": bool(ENV.get("KUMA_URL")), "wifi_port": C["switch"].get("wifi_port") or ""}


def admin(entetes):
    """Vrai si l'utilisateur (d'après l'en-tête du proxy) peut modifier. Sans groupe configuré : tout le monde."""
    g = C["sso"].get("admin_group")
    if not g:
        return True
    return g in (entetes.get(C["sso"]["groups_header"]) or "").split(C["sso"].get("groups_separator") or "|")
