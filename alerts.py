#!/usr/bin/env python3
"""Alertas por Telegram:
  1) Scrub caps nuevas en FIGS (colores/modelos que no estaban antes).
  2) Bajadas de precio de RTX 5060 Ti 8GB y RTX 5060 (datos de pctechsniper.com).

Solo usa la libreria estandar. Variables de entorno:
  TELEGRAM_TOKEN    token del bot (BotFather)
  TELEGRAM_CHAT_ID  tu chat id
Sin token imprime los avisos en pantalla (sirve para probar).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Avisa cuando el mejor precio (nuevo, en stock) sea MENOR a estos valores (USD).
GPU_TARGETS = {
    "RTX 5060 Ti 8GB": 499.99,
    "RTX 5060": 459.99,
}
GPU_API = "https://us.pctechsniper.com/api/catalog"
FIGS_URL = "https://www.wearfigs.com/collections/scrub-caps"
FAIL_NOTIFY_AT = 6  # avisa si algo falla 6 corridas seguidas (~1 hora)

RETAILERS = {"amzn": "Amazon", "negg": "Newegg", "bby": "Best Buy", "wmt": "Walmart"}
EXCLUDED_RETAILERS = {"bby"}  # tiendas que no quiero (bby = Best Buy)
AMAZON_OWN_SELLER = "ATVPDKIKX0DER"  # evita vendedores terceros en Amazon
NO_PRICE = 9999999  # valor que usa la API cuando no hay precio

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ----------------------------------------------------------------- red
def _request(url, data=None, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fallo al pedir {url}: {last}")


def http_text(url, headers=None):
    return _request(url, headers=headers)


def http_json(url, data=None, headers=None):
    return json.loads(_request(url, data=data, headers=headers))


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[SIN TELEGRAM] " + text + "\n")
        return
    _request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
    )


# ----------------------------------------------------------------- GPUs
def best_listings(products):
    """Mejor listado nuevo y en stock por modelo objetivo."""
    best = {}
    for p in products:
        cls = p.get("product_class")
        if cls not in GPU_TARGETS:
            continue
        if not p.get("in_stock_new") or p.get("seller_blacklisted"):
            continue
        if p.get("retailer") in EXCLUDED_RETAILERS:
            continue
        if p.get("retailer") == "amzn" and p.get("seller_id") != AMAZON_OWN_SELLER:
            continue
        try:
            price = float(p.get("price_new"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or price >= NO_PRICE:
            continue
        if cls not in best or price < best[cls][0]:
            best[cls] = (price, p)
    return best


def check_gpus(state):
    data = http_json(
        GPU_API,
        data={"userId": None, "region": "us", "type": "gpu"},
        headers={"Origin": "https://pctechsniper.com", "Referer": "https://pctechsniper.com/"},
    )
    best = best_listings(data["products"].values())
    alerted = state.setdefault("gpu_alerted", {})
    first_run = not state.get("gpu_seen")
    state["gpu_seen"] = True

    for cls, limit in GPU_TARGETS.items():
        found = best.get(cls)
        if found and found[0] < limit:
            price, p = found
            last = alerted.get(cls)
            if last is None or price < last - 0.005:
                msrp = (p.get("metadata") or {}).get("msrp")
                extra = f" ({price / msrp * 100 - 100:+.0f}% vs MSRP ${msrp})" if msrp else ""
                store = RETAILERS.get(p.get("retailer"), p.get("retailer"))
                send(
                    f"🟢 {cls} bajó de ${limit:.2f}\n"
                    f"${price:.2f} en {store}{extra}\n"
                    f"{p.get('short_name')}\n{p.get('link')}"
                )
                alerted[cls] = price
        else:
            alerted.pop(cls, None)  # se reinicia: avisará de nuevo en la próxima bajada

    return {c: (b[0], RETAILERS.get(b[1].get("retailer"), b[1].get("retailer"))) for c, b in best.items()}, first_run


# ----------------------------------------------------------------- FIGS
def parse_figs(html):
    """Devuelve {clave: info} con cada gorra (modelo + color) de la coleccion."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("FIGS: no encontre __NEXT_DATA__ (¿bloqueo o cambio de pagina?)")
    caches = json.loads(m.group(1))["props"]["apolloCaches"]
    items = {}
    for cache in caches.values():
        for key, vg in cache.items():
            if not key.startswith("ProductCatalogVariantGroup:"):
                continue
            handle = (vg.get("product") or {}).get("handle") or ""
            cat = (vg.get("primaryCategory") or {}).get("__ref", "")
            if "Cap" not in cat and "cap" not in handle:
                continue  # descarta accesorios (PIPS, etc.)
            color = vg.get("color") or {}
            price = (((vg.get("priceRange") or {}).get("min")) or {}).get("amount")
            path = (vg.get("urlPath") or "").replace("/collections/scrub-caps", "", 1)
            items[f"{handle}:{color.get('id')}"] = {
                "title": vg.get("title"),
                "color": color.get("displayValue") or color.get("id"),
                "price": price,
                "stock": vg.get("stockState"),
                "url": "https://www.wearfigs.com" + path,
            }
    return items


def check_figs(state):
    items = parse_figs(http_text(FIGS_URL))
    if not items:
        raise RuntimeError("FIGS: 0 gorras encontradas, no actualizo el estado")
    known = set(state.get("figs_known") or [])
    first_run = not known
    new = {k: v for k, v in items.items() if k not in known}
    state["figs_known"] = sorted(known | set(items))
    if not first_run:
        for v in new.values():
            price = f" · ${v['price']}" if v.get("price") is not None else ""
            send(f"🧢 Nueva scrub cap en FIGS\n{v['title']} — {v['color']}{price}\n{v['url']}")
    return len(items), first_run


# ----------------------------------------------------------------- main
def run_check(name, fn, state):
    fails = state.setdefault("fails", {})
    try:
        res = fn(state)
        fails[name] = 0
        return res
    except Exception as e:  # noqa: BLE001
        fails[name] = fails.get(name, 0) + 1
        print(f"ERROR {name} ({fails[name]}): {e}", file=sys.stderr)
        if fails[name] == FAIL_NOTIFY_AT:
            send(f"⚠️ El chequeo de {name} lleva {FAIL_NOTIFY_AT} fallos seguidos: {e}")
        return None


def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    gpu = run_check("gpus", check_gpus, state)
    figs = run_check("figs", check_figs, state)

    # Mensaje de bienvenida en la primera corrida (sirve de prueba de Telegram)
    if (gpu and gpu[1]) or (figs and figs[1]):
        lines = ["✅ Alertas activadas"]
        if figs:
            lines.append(f"FIGS: {figs[0]} gorras registradas; te aviso de las nuevas.")
        if gpu:
            for cls, limit in GPU_TARGETS.items():
                now = gpu[0].get(cls)
                now_txt = f"hoy ${now[0]:.2f} ({now[1]})" if now else "hoy sin stock"
                lines.append(f"{cls}: aviso si baja de ${limit:.2f} ({now_txt})")
        send("\n".join(lines))

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    if gpu is None and figs is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
