#!/usr/bin/env python3
"""
Vigilante de stock - iPhone 18 Pro Max · Negro · 256GB
Tiendas: Apple Millenia y Apple The Florida Mall (Orlando, FL) - recogida en tienda.

Uso:
    python3 iphone_watch.py            # vigila hasta mañana 23:59 (hora local)
    python3 iphone_watch.py --test     # prueba las alertas (sonido, notificación, push)
    python3 iphone_watch.py --once     # una sola revisión y sale
    python3 iphone_watch.py --part MXXXXLL/A   # forzar número de parte si la detección falla

Solo usa la librería estándar de Python 3.8+. No requiere instalar nada.
"""
import argparse
import json
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

# ======================= CONFIGURACIÓN =======================

# Página del modelo exacto (Pro Max = pantalla 6.9"). Puedes añadir variantes de operador
# (att, t-mobile, verizon) si también te sirven; el desbloqueado es el que necesitas en Colombia.
PRODUCT_PAGES = {
    "Desbloqueado": "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-black-unlocked",
    # "AT&T":     "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-black-att",
    # "T-Mobile": "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-black-t-mobile",
    # "Verizon":  "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-black-verizon",
}

ZIP_CODE = "32839"                              # Orlando, entre Millenia y Florida Mall
STORE_KEYWORDS = ["millenia", "florida mall"]   # coincidencia por nombre de tienda
INTERVAL_SECONDS = 30                           # cada cuánto revisar (+ 0-5 s de aleatoriedad)

# Push al celular gratis con la app "ntfy" (iOS/Android): instala la app, suscríbete a este
# tema y pon el mismo nombre aquí. Usa algo difícil de adivinar. Déjalo "" para desactivar.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# En la nube (GitHub Actions) no hay pantalla ni parlantes: solo push.
HEADLESS = os.environ.get("CI") == "true" or os.environ.get("HEADLESS") == "1"

# Fecha/hora (ISO, con zona) después de la cual el script ya no hace nada. Vacío = sin límite.
DEADLINE = os.environ.get("DEADLINE", "")

# =============================================================

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.apple.com/shop/buy-iphone/iphone-18-pro",
}
FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"
PART_RE = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{4,6}LL/A)"')


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def http_get(url, params=None, timeout=20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- Detección del número de parte ----------

def find_part_number(page_url):
    """Busca el número de parte (ej. MXXXXLL/A) del modelo exacto en la página de Apple."""
    html = http_get(page_url)
    slug = page_url.rstrip("/").split("/")[-1].lower()          # 6.9-inch-display-256gb-black-unlocked
    want_carrier = slug.split("-black-")[-1]                    # unlocked / att / t-mobile / verizon

    # 1) Objetos JSON que contienen partNumber + atributos del modelo
    best = None
    for m in re.finditer(r'\{[^{}]{0,1500}?"partNumber"\s*:\s*"[A-Z0-9]{4,6}LL/A"[^{}]{0,1500}?\}', html):
        blob = m.group(0).lower()
        pn = PART_RE.search(m.group(0)).group(1)
        score = 0
        score += "black" in blob
        score += "256" in blob
        score += ("6_9" in blob or "6.9" in blob or "6-9" in blob)
        if want_carrier == "unlocked":
            score += ("unlocked" in blob)
        else:
            score += (want_carrier.replace("-", "") in blob.replace("-", ""))
        if best is None or score > best[0]:
            best = (score, pn)
    if best and best[0] >= 4:
        return best[1]

    # 2) Si la página de la configuración solo trae un número de parte, úsalo
    parts = sorted(set(PART_RE.findall(html)))
    if len(parts) == 1:
        return parts[0]
    return None


# ---------- Consulta de disponibilidad ----------

def check(part_number):
    """Devuelve lista de (tienda, disponible:bool, texto) para las tiendas de interés."""
    params = {
        "pl": "true",
        "mts.0": "regular",
        "cppart": "UNLOCKED/US",
        "parts.0": part_number,
        "location": ZIP_CODE,
    }
    data = json.loads(http_get(FULFILLMENT_URL, params))
    stores = data.get("body", {}).get("content", {}).get("pickupMessage", {}).get("stores", [])
    results = []
    for s in stores:
        name = s.get("storeName", "")
        if not any(k in name.lower() for k in STORE_KEYWORDS):
            continue
        info = (s.get("partsAvailability") or {}).get(part_number, {})
        display = (info.get("pickupDisplay") or "").lower()
        quote = (info.get("pickupSearchQuote")
                 or info.get("messageTypes", {}).get("regular", {}).get("storePickupQuote")
                 or display or "sin dato")
        quote = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(quote))).strip()
        results.append((name, display == "available", quote))
    return results


# ---------- Alertas ----------

def _play_alarm(times=6):
    system = platform.system()
    for _ in range(times):
        try:
            if system == "Darwin":
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
            elif system == "Windows":
                import winsound
                winsound.Beep(1200, 400); winsound.Beep(900, 400)
            else:
                print("\a", end="", flush=True); time.sleep(0.8)
        except Exception:
            print("\a", end="", flush=True)
        time.sleep(0.3)


def desktop_notify(title, msg):
    system = platform.system()
    try:
        if system == "Darwin":
            safe_t, safe_m = title.replace('"', "'"), msg.replace('"', "'")
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe_m}" with title "{safe_t}" sound name "Glass"'],
                           check=False)
            subprocess.Popen(["say", "iPhone disponible para recoger"])
        elif system == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(30000,'{title.replace(chr(39), '')}','{msg.replace(chr(39), '')}',"
                "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep -s 30;$n.Dispose()"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps])
        else:
            subprocess.run(["notify-send", "-u", "critical", title, msg], check=False)
    except Exception as e:
        print(f"  (no se pudo mostrar notificación de escritorio: {e})")


def push_notify(title, msg, url):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), method="POST",
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "urgent",
                     "Tags": "iphone,rotating_light", "Click": url})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  (falló el push a ntfy: {e})")


def alert(title, msg, url, open_page=True):
    print(f"\n{'=' * 60}\n🚨 {title}\n{msg}\n{url}\n{'=' * 60}\n", flush=True)
    push_notify(title, msg, url)
    if HEADLESS:
        return
    desktop_notify(title, msg)
    if open_page:
        webbrowser.open(url)
    threading.Thread(target=_play_alarm, daemon=True).start()


# ---------- Bucle principal ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="probar alertas y salir")
    ap.add_argument("--once", action="store_true", help="una sola revisión")
    ap.add_argument("--part", help="número de parte manual (ej. MXXXXLL/A) para la variante desbloqueada")
    ap.add_argument("--hours", type=float, help="horas a vigilar (por defecto: hasta mañana 23:59)")
    args = ap.parse_args()

    if DEADLINE:
        try:
            if datetime.now().astimezone() >= datetime.fromisoformat(DEADLINE):
                print(f"[{now()}] Pasó la fecha límite ({DEADLINE}). No reviso nada.")
                return
        except ValueError:
            print(f"[{now()}] DEADLINE inválido: {DEADLINE} (ignorado)")

    if args.test:
        alert("PRUEBA: iPhone 18 Pro Max disponible",
              "Esto es una prueba de la alerta.", list(PRODUCT_PAGES.values())[0], open_page=False)
        if not HEADLESS:
            time.sleep(8)
        return

    stop_at = (datetime.now() + timedelta(hours=args.hours)) if args.hours else \
        (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)

    # Resolver números de parte
    targets = {}
    for label, url in PRODUCT_PAGES.items():
        pn = args.part if (args.part and label == "Desbloqueado") else None
        if not pn:
            try:
                pn = find_part_number(url)
            except Exception as e:
                print(f"[{now()}] No pude abrir {url}: {e}")
        if pn:
            targets[label] = (pn, url)
            print(f"[{now()}] {label}: número de parte {pn}")
        else:
            print(f"[{now()}] {label}: no encontré el número de parte. Ábrelo en Chrome, "
                  "pulsa 'Check availability', y en DevTools > Network busca 'fulfillment-messages': "
                  "el valor de parts.0 es el número. Luego corre:  python3 iphone_watch.py --part ESE_NUMERO")
    if not targets:
        sys.exit(1)

    print(f"[{now()}] Vigilando Millenia y The Florida Mall hasta {stop_at:%a %d %b %H:%M}. "
          f"Ctrl+C para detener.\n")

    was_available = {}
    errors = 0
    ok_checks = 0
    while datetime.now() < stop_at:
        for label, (pn, url) in targets.items():
            try:
                results = check(pn)
                errors = 0
                ok_checks += 1
                if not results:
                    print(f"[{now()}] {label}: Apple no devolvió esas tiendas (¿ZIP o nombres cambiaron?)")
                for store, ok, quote in results:
                    key = (label, store)
                    mark = "✅" if ok else "—"
                    print(f"[{now()}] {mark} {store:<18} {label:<12} {quote}")
                    if ok and not was_available.get(key):
                        alert(f"iPhone 18 Pro Max 256GB Negro en {store}",
                              f"{label}: {quote}. ¡Compra ya para recoger!", url)
                    was_available[key] = ok
            except urllib.error.HTTPError as e:
                errors += 1
                print(f"[{now()}] {label}: HTTP {e.code} (Apple limitando o caído), reintento con más pausa")
            except Exception as e:
                errors += 1
                print(f"[{now()}] {label}: error {e}")
        if args.once:
            break
        backoff = min(errors, 5) * 60
        time.sleep(INTERVAL_SECONDS + random.uniform(0, 5) + backoff)

    print(f"[{now()}] Fin del periodo de vigilancia. Revisiones exitosas: {ok_checks}")
    if ok_checks == 0:
        print("Ninguna revisión funcionó: es probable que Apple esté bloqueando esta conexión.")
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido.")
