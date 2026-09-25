# -*- coding: utf-8 -*-
import json
import time
import webbrowser
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

import requests

HOST = "127.0.0.1"
PORT = 8765
TGT_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr/electricity-service"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "TRGRID_V3.html"

def get_tgt(username, password):
    r = requests.post(
        TGT_URL,
        data={"username": username, "password": password},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
        },
        timeout=30,
    )
    if r.status_code != 201:
        raise RuntimeError(
            f"EPİAŞ giriş bileti alınamadı. HTTP {r.status_code}: {r.text[:300]}"
        )
    return r.text.strip()

def get_plants(tgt, session):
    r = session.get(
        BASE_URL + "/v1/generation/data/powerplant-list",
        headers={"TGT": tgt, "Accept": "application/json"},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Santral listesi alınamadı. HTTP {r.status_code}: {r.text[:300]}"
        )
    return r.json().get("items", [])

def get_generation(session, tgt, date_str, ids, group_no):
    url = BASE_URL + "/v1/generation/data/realtime-generation-bulk"
    body = {
        "date": date_str + "T00:00:00+03:00",
        "powerPlantIds": ids,
    }
    headers = {
        "TGT": tgt,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "TR-GRID-TEMIZ/1.0",
    }

    last_error = None
    for attempt in range(1, 6):
        try:
            r = session.post(url, json=body, headers=headers, timeout=(15, 120))
            if r.status_code == 200:
                return r

            last_error = f"HTTP {r.status_code}: {r.text[:250]}"
            if r.status_code not in (403, 429, 500, 502, 503, 504):
                return r

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = repr(exc)

        wait = min(2 * attempt, 10)
        print(
            f"[UYARI] Üretim grubu {group_no}, deneme {attempt}/5 başarısız: "
            f"{last_error}. {wait} sn sonra tekrar..."
        )
        time.sleep(wait)

    raise RuntimeError(
        f"Üretim grubu {group_no} 5 denemede alınamadı: {last_error}"
    )

def classify(row):
    keys = [
        "wind", "sun", "dammedHydro", "river",
        "naturalGas", "lignite", "importCoal",
        "geothermal", "biomass",
        "fueloil", "asphaltiteCoal", "blackCoal",
        "naphta", "lng", "wasteheat"
    ]
    values = {}
    for k in keys:
        try:
            values[k] = float(row.get(k) or 0)
        except Exception:
            values[k] = 0.0

    best = max(keys, key=lambda k: values[k])
    return best if values[best] > 0 else "unknown"

def get_national_load(tgt):
    try:
        r = requests.get(
            BASE_URL + "/v1/dashboard/realtime-consumption",
            headers={"TGT": tgt, "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            return {"value": None, "error": f"HTTP {r.status_code}"}

        data = r.json()
        items = data.get("items") or []
        if not items:
            return {"value": None, "latestUpdateTime": data.get("latestUpdateTime")}

        latest = items[-1]
        value = latest.get("consumption")
        if value is None:
            value = latest.get("value")

        return {
            "value": value,
            "date": latest.get("date"),
            "time": latest.get("time"),
            "latestUpdateTime": data.get("latestUpdateTime"),
        }
    except Exception as exc:
        print("[UYARI] Ulusal anlık yük alınamadı:", exc)
        return {"value": None, "error": str(exc)}

def get_realtime_generation(tgt):
    """EPİAŞ ana sayfasındaki gerçek zamanlı toplam üretim verisini alır."""
    try:
        r = requests.get(
            BASE_URL + "/v1/dashboard/realtime-generation",
            headers={"TGT": tgt, "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            return {"value": None, "error": f"HTTP {r.status_code}"}

        data = r.json()
        items = data.get("items") or []
        if not items:
            return {"value": None, "latestUpdateTime": data.get("latestUpdateTime")}

        latest = items[-1]
        value = latest.get("total")
        if value is None:
            value = latest.get("value")

        return {
            "value": value,
            "date": latest.get("date"),
            "time": latest.get("hour"),
            "latestUpdateTime": data.get("latestUpdateTime"),
            "source": "EPİAŞ dashboard realtime-generation",
        }
    except Exception as exc:
        print("[UYARI] Ulusal anlık üretim alınamadı:", exc)
        return {"value": None, "error": str(exc)}

def fetch_epias(username, password, date_str):
    datetime.strptime(date_str, "%Y-%m-%d")

    session = requests.Session()

    print("[1/3] EPİAŞ giriş bileti alınıyor...")
    tgt = get_tgt(username, password)
    print("[OK] EPİAŞ girişi başarılı.")

    print("[2/3] Santral listesi alınıyor...")
    plants = [p for p in get_plants(tgt, session) if p.get("id") is not None]
    print(f"[OK] {len(plants)} santral bulundu.")

    all_rows = []
    batch_size = 50
    total_groups = (len(plants) + batch_size - 1) // batch_size

    for i in range(0, len(plants), batch_size):
        group_no = i // batch_size + 1
        ids = [p["id"] for p in plants[i:i + batch_size]]

        print(f"[ÜRETİM] Grup {group_no}/{total_groups} ({len(ids)} santral)...")
        response = get_generation(session, tgt, date_str, ids, group_no)

        if response.status_code != 200:
            raise RuntimeError(
                f"Üretim verisi alınamadı. Grup {group_no}. "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        all_rows.extend(response.json().get("items", []))
        time.sleep(1.0)

    aggregate = {}
    source = {}

    for row in all_rows:
        name = str(row.get("powerPlantName") or "").strip()
        if not name:
            continue

        hour = row.get("hour")
        try:
            hs = str(hour).strip()
            if ":" in hs:
                hour_num = int(hs.split(":", 1)[0])
            else:
                hour_num = int(float(hs))
            if hour_num == 24:
                hour_num = 23
        except Exception:
            continue

        if not 0 <= hour_num <= 23:
            continue

        try:
            value = float(row.get("total") or 0)
        except Exception:
            value = 0.0

        if name not in aggregate:
            aggregate[name] = [0.0] * 24

        aggregate[name][hour_num] += value
        source.setdefault(name, classify(row))

    plants_out = []
    for name, hours in aggregate.items():
        plants_out.append({
            "name": name,
            "dailyTotal": round(sum(hours), 6),
            "hourly": [round(v, 6) for v in hours],
            "type": source.get(name, "unknown"),
        })

    print(f"[OK] {len(plants_out)} santral için üretim kaydı hazır.")
    print("[3/3] Ulusal anlık yük deneniyor...")
    load = get_national_load(tgt)
    realtime_generation = get_realtime_generation(tgt)

    return {
        "date": date_str,
        "plantCount": len(plants_out),
        "plants": plants_out,
        "nationalLoad": load,
        "nationalGeneration": realtime_generation,
    }

class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/epias":
            return self.send_json({"error": "Bulunamadı"}, 404)

        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))

            result = fetch_epias(
                data["username"],
                data["password"],
                data["date"],
            )
            self.send_json(result)
        except Exception as exc:
            print("[HATA]", repr(exc))
            self.send_json({"error": str(exc)}, 400)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/TRGRID_V3.html"):
            try:
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 500)

        self.send_json({"error": "Bulunamadı"}, 404)

    def log_message(self, *_args):
        pass

if __name__ == "__main__":
    print("TR-GRID temiz sunucusu: http://127.0.0.1:8765")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Timer(
        1.0,
        lambda: webbrowser.open(f"http://{HOST}:{PORT}/")
    ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu kapatıldı.")
