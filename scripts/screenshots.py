#!/usr/bin/env python3
"""Captures d'écran du README, prises sur le mode démo (données entièrement fictives).

Usage : python3 scripts/screenshots.py [dossier de sortie]   (Playwright pour Python, Chromium et la police Inter)
Lance Vigie en mode démo sur un port libre, dans une base temporaire, puis photographie chaque page.
Variables : CHROMIUM (chemin du navigateur, sinon celui de Playwright), PYTHON (interpréteur pour l'appli).
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ICI = Path(__file__).resolve().parent.parent
SORTIE = Path(sys.argv[1] if len(sys.argv) > 1 else ICI / "docs" / "images")
PAGES = [
    ("services", "/", 1440, 1180, None),
    ("network", "/appareils", 1440, 1240, None),
    ("devices", "/appareils#appareils", 1440, 1000, None),
    ("topology", "/appareils#topologie", 1440, 1240, None),
    ("metrics", "/metriques#global", 1440, 1050, None),
    ("metrics-proxmox", "/metriques#proxmox", 1440, 1000, None),
    ("seeding", "/crossseed", 1440, 1050, None),
    ("mobile", "/", 390, 844, 3),
]


def port_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    SORTIE.mkdir(parents=True, exist_ok=True)
    port = port_libre()
    with tempfile.TemporaryDirectory() as donnees:
        env = {**os.environ, "DEMO": "1", "DATA": donnees, "PORT": str(port), "HOST": "127.0.0.1"}
        env.pop("CONFIG", None)
        serveur = subprocess.Popen([os.environ.get("PYTHON", sys.executable), str(ICI / "app" / "app.py")], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                    break
                except OSError:
                    time.sleep(0.2)
            with sync_playwright() as p:
                nav = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
                for nom, chemin, l, h, echelle in PAGES:
                    page = nav.new_page(viewport={"width": l, "height": h}, device_scale_factor=echelle or 1.5,
                                        is_mobile=bool(echelle), has_touch=bool(echelle))
                    page.add_init_script("localStorage.setItem('vigie-fond', 'minuit'); localStorage.setItem('vigie-fond-anime', '0');")
                    page.emulate_media(reduced_motion="reduce")
                    page.goto(f"http://127.0.0.1:{port}{chemin}", wait_until="networkidle")
                    page.wait_for_timeout(2500 if nom == "seeding" else 1200)
                    page.screenshot(path=str(SORTIE / f"{nom}.png"))
                    page.close()
                    print("ok", nom)
                nav.close()
        finally:
            serveur.terminate()


if __name__ == "__main__":
    main()
