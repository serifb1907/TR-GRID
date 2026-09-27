# -*- coding: utf-8 -*-
import json, os, time, threading, uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import requests

HOST='0.0.0.0'
PORT=int(os.environ.get('PORT','10000'))
TGT_URL='https://giris.epias.com.tr/cas/v1/tickets'
BASE_URL='https://seffaflik.epias.com.tr/electricity-service'
HERE=Path(__file__).resolve().parent
HTML_FILE=HERE/'TRGRID_V3.html'
ALLOWED_ORIGINS={x.strip().rstrip('/') for x in os.environ.get('ALLOWED_ORIGINS','').split(',') if x.strip()}
MAX_BODY_BYTES=32*1024
MAX_USERNAME_LENGTH=320
MAX_PASSWORD_LENGTH=256
RATE_WINDOW_SECONDS=600
RATE_LIMIT=8
MAX_CONCURRENT_JOBS=2
PROGRESS_TTL_SECONDS=1800
PROGRESS={}; PROGRESS_LOCK=threading.Lock()
RATE_BUCKETS=defaultdict(deque); RATE_LOCK=threading.Lock()
JOB_SEMAPHORE=threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

def cleanup_progress():
    now=time.time()
    with PROGRESS_LOCK:
        for k in [k for k,v in PROGRESS.items() if now-v.get('updated',now)>PROGRESS_TTL_SECONDS]: PROGRESS.pop(k,None)

def set_progress(job_id,percent,stage,detail=None):
    cleanup_progress()
    with PROGRESS_LOCK: PROGRESS[job_id]={'percent':int(max(0,min(100,percent))),'stage':stage,'detail':detail or stage,'updated':time.time()}

def get_progress(job_id):
    cleanup_progress()
    with PROGRESS_LOCK: return dict(PROGRESS.get(job_id,{'percent':0,'stage':'İşlem başlatılıyor...','detail':'EPİAŞ bağlantısı hazırlanıyor...'}))

def check_rate_limit(ip):
    now=time.time(); cutoff=now-RATE_WINDOW_SECONDS
    with RATE_LOCK:
        b=RATE_BUCKETS[ip]
        while b and b[0]<=cutoff: b.popleft()
        if len(b)>=RATE_LIMIT: return False,max(1,int(b[0]+RATE_WINDOW_SECONDS-now))
        b.append(now)
    return True,0

def client_ip(h):
    x=h.headers.get('X-Forwarded-For','')
    return (x.split(',')[0].strip() if x else h.client_address[0])[:100]

def origin_allowed(origin, host=''):
    if not origin:
        return True
    clean = origin.rstrip('/')
    if clean in ALLOWED_ORIGINS:
        return True
    try:
        return urlparse(clean).netloc.lower() == host.lower()
    except Exception:
        return False

def get_tgt(username,password):
    r=requests.post(TGT_URL,data={'username':username,'password':password},headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'text/plain'},timeout=30)
    if r.status_code!=201 or not r.text.strip(): raise RuntimeError('EPİAŞ kimlik doğrulaması başarısız oldu.')
    return r.text.strip()

def get_plants(tgt,session):
    r=session.get(BASE_URL+'/v1/generation/data/powerplant-list',headers={'TGT':tgt,'Accept':'application/json'},timeout=60)
    if r.status_code!=200: raise RuntimeError('EPİAŞ santral listesi alınamadı.')
    return r.json().get('items',[])

def get_generation(session,tgt,date_str,ids,group_no):
    url=BASE_URL+'/v1/generation/data/realtime-generation-bulk'; body={'date':date_str+'T00:00:00+03:00','powerPlantIds':ids}
    headers={'TGT':tgt,'Content-Type':'application/json','Accept':'application/json','User-Agent':'SantralMatik-WEB/1.0'}
    last=None
    for attempt in range(1,6):
        try:
            r=session.post(url,json=body,headers=headers,timeout=(15,120))
            if r.status_code==200: return r
            last=f'HTTP {r.status_code}'
            if r.status_code not in (403,429,500,502,503,504): return r
        except (requests.exceptions.ConnectionError,requests.exceptions.Timeout): last='Bağlantı veya zaman aşımı'
        time.sleep(min(2*attempt,10))
    raise RuntimeError(f'Üretim grubu {group_no} alınamadı.')

def classify(row):
    keys=['wind','sun','dammedHydro','river','naturalGas','lignite','importCoal','geothermal','biomass','fueloil','asphaltiteCoal','blackCoal','naphta','lng','wasteheat']
    vals={}
    for k in keys:
        try: vals[k]=float(row.get(k) or 0)
        except Exception: vals[k]=0.0
    best=max(keys,key=lambda k:vals[k]); return best if vals[best]>0 else 'unknown'

def get_national_load(tgt):
    try:
        r=requests.get(BASE_URL+'/v1/dashboard/realtime-consumption',headers={'TGT':tgt,'Accept':'application/json'},timeout=30)
        if r.status_code!=200: return {'value':None,'error':'Veri alınamadı'}
        data=r.json(); items=data.get('items') or []
        if not items: return {'value':None,'latestUpdateTime':data.get('latestUpdateTime')}
        latest=items[-1]; value=latest.get('consumption') if latest.get('consumption') is not None else latest.get('value')
        return {'value':value,'date':latest.get('date'),'time':latest.get('time'),'latestUpdateTime':data.get('latestUpdateTime')}
    except Exception: return {'value':None,'error':'Veri alınamadı'}

def fetch_epias(username,password,date_str,job_id):
    datetime.strptime(date_str,'%Y-%m-%d'); session=requests.Session(); tgt=None
    try:
        set_progress(job_id,2,'EPİAŞ sunucusuna bağlanılıyor...','Giriş bileti alınıyor...')
        tgt=get_tgt(username,password)
        set_progress(job_id,8,'EPİAŞ bağlantısı kuruldu.','Kimlik doğrulama tamamlandı.')
        set_progress(job_id,12,'Santral listesi alınıyor...','EPİAŞ santral listesi hazırlanıyor...')
        plants=[p for p in get_plants(tgt,session) if p.get('id') is not None]
        set_progress(job_id,15,'Santral listesi hazır.',f'{len(plants)} santral bulundu.')
        all_rows=[]; batch_size=50; total_groups=max(1,(len(plants)+batch_size-1)//batch_size); start_pct,end_pct=15,76
        for i in range(0,len(plants),batch_size):
            group_no=i//batch_size+1; ids=[p['id'] for p in plants[i:i+batch_size]]
            set_progress(job_id,start_pct+int((group_no-1)/total_groups*(end_pct-start_pct)),'Üretim verileri toplanıyor...',f'Santral grubu {group_no}/{total_groups} alınıyor...')
            response=get_generation(session,tgt,date_str,ids,group_no)
            if response.status_code!=200: raise RuntimeError(f'Üretim verisi alınamadı. Grup {group_no}.')
            all_rows.extend(response.json().get('items',[]))
            set_progress(job_id,start_pct+int(group_no/total_groups*(end_pct-start_pct)),'Üretim verileri toplanıyor...',f'{group_no}/{total_groups} santral grubu tamamlandı.')
            time.sleep(.8)
        set_progress(job_id,80,'Saatlik üretimler işleniyor...','Üretim kayıtları santrallere göre birleştiriliyor...')
        aggregate={}; source={}
        for row in all_rows:
            name=str(row.get('powerPlantName') or '').strip()
            if not name: continue
            try:
                hs=str(row.get('hour')).strip(); hour_num=int(hs.split(':',1)[0]) if ':' in hs else int(float(hs)); hour_num=23 if hour_num==24 else hour_num
            except Exception: continue
            if not 0<=hour_num<=23: continue
            try: value=float(row.get('total') or 0)
            except Exception: value=0.0
            aggregate.setdefault(name,[0.0]*24)[hour_num]+=value; source.setdefault(name,classify(row))
        plants_out=[{'name':name,'dailyTotal':round(sum(hours),6),'hourly':[round(v,6) for v in hours],'type':source.get(name,'unknown')} for name,hours in aggregate.items()]
        set_progress(job_id,87,'Türkiye toplam üretimi hesaplanıyor...',f'{len(plants_out)} üretim kaydı hazırlandı.')
        load=get_national_load(tgt)
        set_progress(job_id,94,'Harita verileri hazırlanıyor...','Santral verileri harita ile eşleştiriliyor...')
        result={'date':date_str,'plantCount':len(plants_out),'plants':plants_out,'nationalLoad':load}
        set_progress(job_id,100,'Veriler hazır.','EPİAŞ verileri başarıyla işlendi.')
        return result
    finally:
        tgt=None; password=None; username=None; session.close()

class Handler(BaseHTTPRequestHandler):
    def _json(self,obj,status=200,extra_headers=None):
        body=json.dumps(obj,ensure_ascii=False).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body)))
        origin=self.headers.get('Origin','')
        if origin and origin_allowed(origin, self.headers.get('Host', '')): self.send_header('Access-Control-Allow-Origin',origin); self.send_header('Vary','Origin')
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer')
        if extra_headers:
            for k,v in extra_headers.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self):
        origin=self.headers.get('Origin','')
        if not origin_allowed(origin, self.headers.get('Host', '')): self.send_response(403); self.send_header('Content-Length','0'); self.end_headers(); return
        self.send_response(204)
        if origin: self.send_header('Access-Control-Allow-Origin',origin); self.send_header('Vary','Origin')
        self.send_header('Access-Control-Allow-Headers','Content-Type'); self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS'); self.send_header('Access-Control-Max-Age','600'); self.end_headers()
    def do_POST(self):
        if urlparse(self.path).path!='/api/epias': return self._json({'error':'Bulunamadı'},404)
        origin=self.headers.get('Origin','')
        if not origin_allowed(origin, self.headers.get('Host', '')): return self._json({'error':'Yetkisiz kaynak'},403)
        if self.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json': return self._json({'error':'Geçersiz istek türü.'},415)
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: return self._json({'error':'Geçersiz istek.'},400)
        if length<=0 or length>MAX_BODY_BYTES: return self._json({'error':'İstek boyutu geçersiz.'},413)
        allowed,retry=check_rate_limit(client_ip(self))
        if not allowed: return self._json({'error':'Çok fazla istek gönderildi. Lütfen daha sonra tekrar deneyin.'},429,{'Retry-After':str(retry)})
        try:
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError
            username=str(data.get('username') or '').strip(); password=str(data.get('password') or ''); date_str=str(data.get('date') or '').strip(); client_job_id=str(data.get('jobId') or '').strip()
            if not username or not password or not date_str: return self._json({'error':'Kullanıcı adı, şifre ve tarih gereklidir.'},400)
            if len(username)>MAX_USERNAME_LENGTH or len(password)>MAX_PASSWORD_LENGTH: return self._json({'error':'Giriş bilgileri geçersiz.'},400)
            datetime.strptime(date_str,'%Y-%m-%d')
            job_id=str(uuid.UUID(client_job_id)) if client_job_id else str(uuid.uuid4())
        except (json.JSONDecodeError,ValueError,TypeError): return self._json({'error':'Geçersiz istek verisi.'},400)
        if not JOB_SEMAPHORE.acquire(timeout=1): return self._json({'error':'Sunucu şu anda yoğun. Lütfen kısa süre sonra tekrar deneyin.'},503,{'Retry-After':'15'})
        try:
            set_progress(job_id,0,'İşlem başlatılıyor...','EPİAŞ bağlantısı hazırlanıyor...')
            result=fetch_epias(username,password,date_str,job_id); return self._json(result,200)
        except Exception:
            set_progress(job_id,0,'Veri alınamadı.','EPİAŞ verileri alınamadı. Bilgilerinizi kontrol edip tekrar deneyin.')
            return self._json({'error':'EPİAŞ verileri alınamadı. Giriş bilgilerinizi ve tarihi kontrol edip tekrar deneyin.'},400)
        finally:
            username=None; password=None; data=None; JOB_SEMAPHORE.release()
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/health': return self._json({'status':'ok','service':'SantralMatik'})
        if path=='/api/epias-progress':
            job_id=parse_qs(urlparse(self.path).query).get('jobId',[''])[0]
            try: job_id=str(uuid.UUID(job_id))
            except ValueError: return self._json({'error':'Geçersiz işlem kimliği.'},400)
            return self._json(get_progress(job_id))
        if path in ('/','/TRGRID_V3.html'):
            try:
                body=HTML_FILE.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.end_headers(); self.wfile.write(body); return
            except Exception: return self._json({'error':'Sayfa yüklenemedi.'},500)
        return self._json({'error':'Bulunamadı'},404)
    def log_message(self,*_args): pass

if __name__=='__main__':
    print(f'SantralMatik web sunucusu: http://{HOST}:{PORT}')
    if not ALLOWED_ORIGINS: print('UYARI: ALLOWED_ORIGINS ayarlanmamış. Render Environment Variables içine blog adresini ekleyin.')
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nSunucu kapatıldı.')
    finally: server.server_close()
