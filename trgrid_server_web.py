# -*- coding: utf-8 -*-
import json
import os
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime

import requests

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
TGT_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr/electricity-service"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "TRGRID_V3.html"

PROGRESS = {}
PROGRESS_LOCK = threading.Lock()


def set_progress(job_id, percent, stage, detail=None):
    with PROGRESS_LOCK:
        PROGRESS[job_id] = {
            "percent": int(max(0, min(100, percent))),
            "stage": stage,
            "detail": detail or stage,
            "updated": time.time(),
        }


def get_progress(job_id):
    with PROGRESS_LOCK:
        return dict(PROGRESS.get(job_id, {
            "percent": 0,
            "stage": "İşlem başlatılıyor...",
            "detail": "EPİAŞ bağlantısı hazırlanıyor...",
        }))


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
        raise RuntimeError(f"EPİAŞ giriş bileti alınamadı. HTTP {r.status_code}: {r.text[:300]}")
    return r.text.strip()


def get_plants(tgt, session):
    r = session.get(
        BASE_URL + "/v1/generation/data/powerplant-list",
        headers={"TGT": tgt, "Accept": "application/json"},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Santral listesi alınamadı. HTTP {r.status_code}: {r.text[:300]}")
    return r.json().get("items", [])


def get_generation(session, tgt, date_str, ids, group_no):
    url = BASE_URL + "/v1/generation/data/realtime-generation-bulk"
    body = {"date": date_str + "T00:00:00+03:00", "powerPlantIds": ids}
    headers = {
        "TGT": tgt,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "SantralMatik-WEB/1.0",
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
        time.sleep(wait)
    raise RuntimeError(f"Üretim grubu {group_no} 5 denemede alınamadı: {last_error}")


def classify(row):
    keys = [
        "wind", "sun", "dammedHydro", "river", "naturalGas", "lignite", "importCoal",
        "geothermal", "biomass", "fueloil", "asphaltiteCoal", "blackCoal",
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
        return {"value": None, "error": str(exc)}


def fetch_epias(username, password, date_str, job_id):
    datetime.strptime(date_str, "%Y-%m-%d")
    session = requests.Session()

    set_progress(job_id, 2, "EPİAŞ sunucusuna bağlanılıyor...", "Giriş bileti alınıyor...")
    tgt = get_tgt(username, password)
    set_progress(job_id, 8, "EPİAŞ bağlantısı kuruldu.", "Kimlik doğrulama tamamlandı.")

    set_progress(job_id, 12, "Santral listesi alınıyor...", "EPİAŞ santral listesi hazırlanıyor...")
    plants = [p for p in get_plants(tgt, session) if p.get("id") is not None]
    set_progress(job_id, 15, "Santral listesi hazır.", f"{len(plants)} santral bulundu.")

    all_rows = []
    batch_size = 50
    total_groups = max(1, (len(plants) + batch_size - 1) // batch_size)
    # Üretim grupları gerçek tamamlanan grup sayısına göre %15 -> %76 arasında ilerler.
    start_pct, end_pct = 15, 76

    for i in range(0, len(plants), batch_size):
        group_no = i // batch_size + 1
        ids = [p["id"] for p in plants[i:i + batch_size]]
        set_progress(
            job_id,
            start_pct + int((group_no - 1) / total_groups * (end_pct - start_pct)),
            "Üretim verileri toplanıyor...",
            f"Santral grubu {group_no}/{total_groups} alınıyor...",
        )
        response = get_generation(session, tgt, date_str, ids, group_no)
        if response.status_code != 200:
            raise RuntimeError(
                f"Üretim verisi alınamadı. Grup {group_no}. HTTP {response.status_code}: {response.text[:500]}"
            )
        all_rows.extend(response.json().get("items", []))
        completed_pct = start_pct + int(group_no / total_groups * (end_pct - start_pct))
        set_progress(
            job_id,
            completed_pct,
            "Üretim verileri toplanıyor...",
            f"{group_no}/{total_groups} santral grubu tamamlandı.",
        )
        time.sleep(0.8)

    set_progress(job_id, 80, "Saatlik üretimler işleniyor...", "Üretim kayıtları santrallere göre birleştiriliyor...")
    aggregate = {}
    source = {}
    for row in all_rows:
        name = str(row.get("powerPlantName") or "").strip()
        if not name:
            continue
        hour = row.get("hour")
        try:
            hs = str(hour).strip()
            hour_num = int(hs.split(":", 1)[0]) if ":" in hs else int(float(hs))
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
        aggregate.setdefault(name, [0.0] * 24)[hour_num] += value
        source.setdefault(name, classify(row))

    plants_out = []
    for name, hours in aggregate.items():
        plants_out.append({
            "name": name,
            "dailyTotal": round(sum(hours), 6),
            "hourly": [round(v, 6) for v in hours],
            "type": source.get(name, "unknown"),
        })

    set_progress(job_id, 87, "Türkiye toplam üretimi hesaplanıyor...", f"{len(plants_out)} üretim kaydı hazırlandı.")
    load = get_national_load(tgt)
    set_progress(job_id, 94, "Harita verileri hazırlanıyor...", "Santral verileri harita ile eşleştiriliyor...")

    result = {
        "date": date_str,
        "plantCount": len(plants_out),
        "plants": plants_out,
        "nationalLoad": load,
    }
    set_progress(job_id, 100, "Veriler hazır.", "EPİAŞ verileri başarıyla işlendi.")
    return result


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/epias":
            return self._json({"error": "Bulunamadı"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            job_id = str(data.get("jobId") or uuid.uuid4())
            set_progress(job_id, 0, "İşlem başlatılıyor...", "EPİAŞ bağlantısı hazırlanıyor...")
            result = fetch_epias(data["username"], data["password"], data["date"], job_id)
            self._json(result)
        except Exception as exc:
            job_id = locals().get("job_id")
            if job_id:
                set_progress(job_id, 0, "Veri alınamadı.", str(exc))
            self._json({"error": str(exc)}, 400)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self._json({"status": "ok", "service": "SantralMatik"})
        if path == "/api/epias-progress":
            job_id = parse_qs(parsed.query).get("jobId", [""])[0]
            if not job_id:
                return self._json({"error": "jobId gerekli"}, 400)
            return self._json(get_progress(job_id))
        if path in ("/", "/TRGRID_V3.html"):
            try:
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        self._json({"error": "Bulunamadı"}, 404)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    print(f"SantralMatik web sunucusu: http://{HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu kapatıldı.")
