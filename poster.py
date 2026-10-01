"""Kartenposter als PDF/PNG: Raster (Kacheln, optional Relief) in beliebiger Projektion, Grenzen und Beschriftung als Vektor.

Setup:       py -m pip install pyproj pillow reportlab requests numpy
Browser:     py poster.py --serve      -> http://localhost:8000 öffnen, Button "Mit Python exportieren"
Smoke test:  py poster.py --width-mm 100 --dpi 150 --zoom 6 --out test.pdf
Europa 1 m:  py poster.py --width-mm 1000 --dpi 800 --out europa.pdf   (EPSG:3035; .png = ein Bild, Text/Linien gerastert)
Region:      py poster.py --region -25 34 45 72 --proj rob --hs-str 0.33 --out x.pdf
Kacheln landen in ./tile_cache, ein neuer Lauf setzt fort.
"""
import argparse, json, math, os, re, shutil, struct, subprocess, sys, tempfile, threading, time, zlib
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests
from PIL import Image, ImageDraw, ImageFile, ImageFont
from pyproj import Transformer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

BASE = {
    'sat': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    'topo': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    'osm': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    'none': None,  # weißes Papier
}
DEM = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
NE = 'https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson'
EARTH = 6378137.0  # Kugel wie im Browser
PROJ = {  # Namen wie im Dropdown der Seite; *w = Weltkarte (Region und lon0 kommen dann von der Seite)
    'mercator': '+proj=merc +R={R} +lon_0={lon0}', 'ee': '+proj=eqearth +R={R} +lon_0={lon0}', 'eew': '+proj=eqearth +R={R} +lon_0={lon0}',
    'rob': '+proj=robin +R={R} +lon_0={lon0}', 'robw': '+proj=robin +R={R} +lon_0={lon0}', 'laea': '+proj=laea +R={R} +lat_0={lat0} +lon_0={lon0}',
}
HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument('--out', default='europa.pdf')
ap.add_argument('--base', choices=BASE, default='sat')
ap.add_argument('--extent', nargs=4, type=float, default=[2.5e6, 1.2e6, 7.5e6, 5.7e6], metavar=('XMIN', 'YMIN', 'XMAX', 'YMAX'), help='EPSG:3035 Meter (ohne --region)')
ap.add_argument('--region', nargs=4, type=float, metavar=('W', 'S', 'O', 'N'), help='Grad; ersetzt --extent, Projektion per --proj')
ap.add_argument('--proj', choices=PROJ, default='laea')
ap.add_argument('--lon0', type=float, help='Mittelmeridian (Standard: Mitte der Region)')
ap.add_argument('--lat0', type=float, help='Mittelbreite für laea (Standard: Mitte der Region)')
ap.add_argument('--width-mm', type=float, default=1000)
ap.add_argument('--dpi', type=float, default=800)
ap.add_argument('--zoom', type=int, help='Kachelzoom; Standard: nie hochskalieren')
ap.add_argument('--pt', type=float, default=5, help='Städtenamen in pt (Berge 0.8x)')
ap.add_argument('--country-scale', type=float, default=1.3, help='Ländernamen = pt * Faktor')
ap.add_argument('--label-zoom', type=float, default=5, help='wie Web-Zoom der Vorschau: höher = mehr Orte')
ap.add_argument('--text', choices=['white', 'black'], default='white')
ap.add_argument('--dots', action='store_true', help='Punkt neben Städten und Bergen (Farbe wie die Schrift, ohne Umrandung)')
ap.add_argument('--no-labels', action='store_true', help='keine Grenzen und Namen')
ap.add_argument('--hs-str', type=float, default=0, help='Stärke der Relief-Schattierung 0..1 (0 = aus)')
ap.add_argument('--hs-zf', type=float, default=8, help='Z-Faktor')
ap.add_argument('--hs-az', type=float, default=315); ap.add_argument('--hs-alt', type=float, default=45)
ap.add_argument('--hs-gl', type=float, default=.1, help='Glanzlichter 0..1')
ap.add_argument('--dem-zoom', type=int, help='Zoom der Höhenkacheln (Standard: wie Kachelzoom, max 15)')
ap.add_argument('--quality', type=int, default=90)
ap.add_argument('--strip', type=int, default=2048, help='Zeilen pro JPEG-Streifen im PDF')
ap.add_argument('--tile-mm', type=float, default=0, help='Rechen-Kacheln höchstens so groß; 0 = ein Block')
ap.add_argument('--cache', default='tile_cache')
ap.add_argument('--serve', action='store_true', help='lokaler Server für den Button in index.html')
ap.add_argument('--port', type=int, default=8000)
a = ap.parse_args()


# --- Server: index.html + Export-Aufträge, jeder Auftrag ist ein eigener Aufruf dieses Skripts ---
def serve(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    outdir, cache, jobs = os.path.join(HERE, 'poster_out'), os.path.join(HERE, 'tile_cache'), {}
    os.makedirs(outdir, exist_ok=True)

    def num(j, k, lo, hi):
        v = float(j[k])
        if not lo <= v <= hi: raise ValueError(f'{k} außerhalb {lo}..{hi}')
        return v

    def command(j):  # geprüfte Zahlen und Namen, nie freie Strings, in die Kommandozeile
        proj, base, fmt = j['proj'], j['base'], j['fmt']
        if proj not in PROJ or base not in BASE or fmt not in ('pdf', 'png'): raise ValueError('proj/base/fmt unbekannt')
        w, s, e, n = (num({'r': v}, 'r', -360, 360) for v in j['region'])
        if not (w < e and s < n): raise ValueError('Region leer')
        hs = j['hs']; dpi = num(j, 'dpi', 50, 1200); cm = num(j, 'cm', 5, 1000); tile_mm = num(j, 'tileMm', 0, 1000)
        tag = '_punkte' if j.get('dots') else ''  # Einstellung im Dateinamen sichtbar
        out = os.path.join(outdir, f'topo_{proj}_{base}_{cm:g}cm_{dpi:g}dpi{tag}_{time.strftime("%H%M%S")}.{fmt}')
        c = [sys.executable, os.path.abspath(__file__), '--proj', proj, '--region', str(w), str(s), str(e), str(n), '--base', base, '--width-mm', str(cm * 10), '--dpi', str(dpi),
             '--pt', str(num(j, 'pt', 1, 200)), '--label-zoom', str(num(j, 'labelZoom', 0, 22)), '--text', 'black' if j['text'] == 'black' else 'white', '--out', out, '--cache', cache,
             '--hs-str', str(num(hs, 'str', 0, 1)), '--hs-zf', str(num(hs, 'zf', .01, 1000)), '--hs-az', str(num(hs, 'az', 0, 360)), '--hs-alt', str(num(hs, 'alt', 1, 90)), '--hs-gl', str(num(hs, 'gl', 0, 1)), '--tile-mm', str(tile_mm)]
        for k in ('lon0', 'lat0'):
            if j.get(k) is not None: c += [f'--{k}', str(num(j, k, -360, 360))]
        if not j['labels']: c.append('--no-labels')
        if j.get('dots'): c.append('--dots')
        return c, out

    def run(job, cmd):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        for line in p.stdout:
            line = line.strip(); job['log'] = line
            m = re.match(r'Kacheln (\d+)/(\d+)', line)
            if m: job['pct'] = .4 * int(m[1]) / int(m[2])
            m = re.match(r'Zeilen (\d+)/(\d+)', line)
            if m: job['pct'] = .4 + .6 * int(m[1]) / int(m[2])
        job['state'] = 'done' if p.wait() == 0 else 'error'

    class Handler(BaseHTTPRequestHandler):
        def send(self, body, ctype='application/json', code=200, extra=None):
            self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)
        def json(self, obj, code=200): self.send(json.dumps(obj).encode(), code=code)
        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ('/', '/index.html'):
                with open(os.path.join(HERE, 'index.html'), 'rb') as f: self.send(f.read(), 'text/html; charset=utf-8')
            elif u.path == '/status':
                j = jobs.get(parse_qs(u.query).get('id', [''])[0])
                self.json({'state': j['state'], 'pct': j['pct'], 'log': j['log'], 'file': j['file']} if j else {'error': 'unbekannter Auftrag'}, 200 if j else 404)
            elif u.path.startswith('/out/'):
                p = os.path.join(outdir, os.path.basename(u.path))
                if not os.path.isfile(p): return self.json({'error': 'nicht gefunden'}, 404)
                with open(p, 'rb') as f: self.send(f.read(), 'application/octet-stream', extra={'Content-Disposition': f'attachment; filename="{os.path.basename(p)}"'})
            else: self.json({'error': 'nicht gefunden'}, 404)
        def do_POST(self):
            # nur Aufrufe von der eigenen Seite: fremde Webseiten können localhost sonst anstoßen
            if self.path != '/render' or self.headers.get('Origin') not in (None, f'http://localhost:{port}', f'http://127.0.0.1:{port}', f'http://[::1]:{port}'): return self.json({'error': 'nicht erlaubt'}, 403)
            try:
                cmd, out = command(json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))))
            except (ValueError, KeyError, TypeError) as e: return self.json({'error': f'ungültige Eingabe: {e}'}, 400)
            jid = str(int(time.time() * 1000)); jobs[jid] = {'state': 'run', 'pct': 0, 'log': 'startet…', 'file': os.path.basename(out)}
            threading.Thread(target=run, args=(jobs[jid], cmd), daemon=True).start()
            self.json({'id': jid})
        def log_message(self, *args): pass

    import socket
    servers = []  # "localhost" löst je nach Browser/System zu IPv4 oder IPv6 auf → beide Loopback-Adressen bedienen, nie das Netzwerk
    for fam, host in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        class Srv(ThreadingHTTPServer): address_family = fam; allow_reuse_address = False  # Windows: sonst dürfen mehrere Prozesse denselben Port belegen
        try: servers.append(Srv((host, port), Handler))
        except OSError as e:
            if fam == socket.AF_INET: sys.exit(f'Port {port} nicht verfügbar ({e}). Anderen Port wählen: py poster.py --serve --port 8765')
    for s in servers[1:]: threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f'Server läuft: http://127.0.0.1:{port}  (Strg+C beendet; fertige Dateien in {outdir})', flush=True)
    servers[0].serve_forever()

if a.serve: serve(a.port); sys.exit()


# --- Geometrie ---
if a.region:
    w0, s0, e0, n0 = a.region
    lon0 = a.lon0 if a.lon0 is not None else (w0 + e0) / 2; lat0 = a.lat0 if a.lat0 is not None else (s0 + n0) / 2
    crs, geo = PROJ[a.proj].format(R=EARTH, lon0=lon0, lat0=lat0) + ' +no_defs', f'+proj=longlat +R={EARTH} +no_defs'
    fwd, inv = Transformer.from_crs(geo, crs, always_xy=True), Transformer.from_crs(crs, geo, always_xy=True)
    gl, gb = np.meshgrid(np.linspace(w0, e0, 41), np.linspace(s0, n0, 41))  # Bounding-Box der Region in Projektionskoordinaten
    gx, gy = fwd.transform(gl.ravel(), gb.ravel()); ok = np.isfinite(gx) & np.isfinite(gy)
    x0, x1, y0, y1 = gx[ok].min(), gx[ok].max(), gy[ok].min(), gy[ok].max()
else:
    fwd, inv = Transformer.from_crs(4326, 3035, always_xy=True), Transformer.from_crs(3035, 4326, always_xy=True)
    x0, y0, x1, y1 = a.extent
W = round(a.width_mm / 25.4 * a.dpi); H = round(W * (y1 - y0) / (x1 - x0))
dx, dy = (x1 - x0) / W, (y1 - y0) / H                     # Projektionseinheiten pro Ausgabepixel
pw = a.width_mm / 25.4 * 72; ph = pw * H / W               # Seite in pt
S = requests.Session(); S.headers['User-Agent'] = 'topography-map-poster/1.0'

def merc(lon, lat, z):  # Web-Mercator-Pixel auf Zoom z
    n = 256 * 2 ** z
    return (lon + 180) / 360 * n, (1 - np.arcsinh(np.tan(np.radians(np.clip(lat, -85.0511287798, 85.0511287798)))) / np.pi) / 2 * n

CHECK = bool(a.region) and a.proj in ('ee', 'eew', 'rob', 'robw')  # deren Umkehrung liefert außerhalb des Umrisses Unsinn statt eines Fehlers
def lonlat(xs, ys):
    lon, lat = inv.transform(xs.ravel(), ys.ravel())
    if CHECK:  # Rundreise prüfen, außerhalb des Umrisses bleibt es weiß
        bx, by = fwd.transform(lon, lat); bad = ~((np.abs(bx - xs.ravel()) < 2 * dx) & (np.abs(by - ys.ravel()) < 2 * dy))
        lon, lat = np.where(bad, np.nan, lon), np.where(bad, np.nan, lat)
    return np.reshape(lon, xs.shape), np.reshape(lat, xs.shape)

# --- Kacheln ---
if a.zoom is None:
    clon, clat = inv.transform((x0 + x1) / 2, (y0 + y1) / 2)
    e = 1e-3; xa, ya = fwd.transform(clon + e, clat); xc, yc = fwd.transform(clon, clat)
    kscale = math.hypot(xa - xc, ya - yc) / (EARTH * math.cos(math.radians(clat)) * math.radians(e))  # Maßstabsfaktor entlang des Breitenkreises in der Mitte
    a.zoom = min(19, max(0, math.ceil(math.log2(2 * math.pi * EARTH * math.cos(math.radians(clat)) / 256 / (dx / kscale)))))
z = a.zoom; zd = min(a.dem_zoom or z, 15)
URL = {a.base: BASE[a.base], 'dem': DEM}
tpath = lambda kind, z, x, y: os.path.join(a.cache, kind, str(z), str(x), f'{y}.tile')

def needed(zz):  # exakt die Kacheln, die Ausgabepixel berühren (grobes Raster + 1 Kachel Rand), nicht die Bounding-Box
    gx, gy = np.meshgrid(np.linspace(x0, x1, W // 32 + 2), np.linspace(y0, y1, H // 32 + 2))
    lon, lat = lonlat(gx, gy); px, py = merc(lon, lat, zz); n = 2 ** zz
    ok = np.isfinite(lon) & np.isfinite(lat); tx, ty = (px[ok] // 256).astype(np.int64), (py[ok] // 256).astype(np.int64)
    code = np.unique(np.concatenate([np.clip(tx + i, 0, n - 1) * n + np.clip(ty + j, 0, n - 1) for i in (-1, 0, 1) for j in (-1, 0, 1)]))
    return [(zz, int(c // n), int(c % n)) for c in code]

def fetch(t):
    kind, z, x, y = t; p = tpath(*t)
    if os.path.exists(p): return 0
    for _ in range(3):
        try:
            r = S.get(URL[kind].format(z=z, x=x, y=y), timeout=30)
            if r.status_code == 404: return 1
            r.raise_for_status()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + '.part', 'wb') as f: f.write(r.content)
            os.replace(p + '.part', p); return 0
        except requests.RequestException: pass
    return 1  # ponytail: fehlende Kachel bleibt weiß bzw. flach; erneuter Lauf versucht sie wieder

def load(kind, z, x, y):
    try: t = np.asarray(Image.open(tpath(kind, z, x, y)).convert('RGB'))
    except OSError: return None
    return t[..., 0] * 256. + t[..., 1] + t[..., 2] / 256 - 32768 if kind == 'dem' else t  # dem: Terrarium -> Meter

hs = a.hs_str > 0
jobs = ([(a.base, *t) for t in needed(z)] if a.base != 'none' else []) + ([('dem', *t) for t in needed(zd)] if hs else [])
print(f'{W}x{H} px ({W * H / 1e6:.0f} Mpx), Zoom {z}{f" (Relief {zd})" if hs else ""}, {len(jobs)} Kacheln (~{len(jobs) * 35 / 1e6:.1f} GB)', flush=True)
with ThreadPoolExecutor(16) as ex:
    miss = 0
    for i, m in enumerate(ex.map(fetch, jobs)):
        miss += m
        if i % 1000 == 999: print(f'Kacheln {i + 1}/{len(jobs)}', flush=True)
if miss: print(f'WARNUNG: {miss} Kacheln fehlen (weiße Flecken)')

# --- Raster: Ausgabezeilen-Band -> Web-Mercator-Mosaik -> bilinear (+ Relief) ---
MAX_BAND_PIXELS = 4_000_000
MAX_MOSAIC_BYTES = 96 * 1024 ** 2
MAX_JPEG_PIXELS = 8 * 1024 ** 2
MAX_JPEG_DIMENSION = 65_000
class MosaicTooLarge(Exception): pass

def mosaic(kind, zz, px, py, ok):
    tx0, tx1, ty0, ty1 = int(px[ok].min() // 256), int(px[ok].max() // 256), int(py[ok].min() // 256), int(py[ok].max() // 256)
    shape = ((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256)
    if shape[0] * shape[1] * (4 if kind == 'dem' else 3) > MAX_MOSAIC_BYTES: raise MosaicTooLarge
    m = np.zeros(shape, np.float32) if kind == 'dem' else np.full(shape + (3,), 255, np.uint8)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = load(kind, zz, tx, ty)
            if t is not None: m[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t
    return m, tx0, ty0

def bil(m, u, v):
    i = np.clip(np.floor(u).astype(np.int32), 0, m.shape[1] - 2); j = np.clip(np.floor(v).astype(np.int32), 0, m.shape[0] - 2)
    fu, fv = np.clip(u - i, 0, 1), np.clip(v - j, 0, 1)
    if m.ndim == 3: fu, fv = fu[..., None], fv[..., None]
    return m[j, i] * (1 - fu) * (1 - fv) + m[j, i + 1] * fu * (1 - fv) + m[j + 1, i] * (1 - fu) * fv + m[j + 1, i + 1] * fu * fv

def shade(h, ty0, zz):  # Höhenmosaik (m) -> Schattierung -1..1 (>0 hell), gleiche Formeln wie im Browser, aber über Kachelgrenzen hinweg
    zen, azm = math.radians(90 - a.hs_alt), math.radians((360 - a.hs_az + 90) % 360)
    cz, sz, ca, sa = math.cos(zen), math.sin(zen), math.cos(azm), math.sin(azm)
    sh = np.empty_like(h); n = 256 * 2 ** zz
    for r0 in range(0, h.shape[0], 128):
        r1 = min(h.shape[0], r0 + 128)
        c = np.pad(h[np.clip(np.arange(r0 - 1, r1 + 1), 0, h.shape[0] - 1)], ((0, 0), (1, 1)), mode='edge')
        A, B, C, D, F, G, Hh, I = c[:-2, :-2], c[:-2, 1:-1], c[:-2, 2:], c[1:-1, :-2], c[1:-1, 2:], c[2:, :-2], c[2:, 1:-1], c[2:, 2:]
        lat = np.arctan(np.sinh(np.pi * (1 - 2 * (ty0 * 256 + np.arange(r0, r1) + .5) / n)))
        q = (n / (8 * 40075016.686 * np.cos(lat)))[:, None].astype(np.float32)  # 1 / (8 * Zellgröße in m)
        dx_, dy_ = ((C + 2 * F + I) - (A + 2 * D + G)) * q, ((G + 2 * Hh + I) - (A + 2 * B + C)) * q
        s = (cz + sz * a.hs_zf * (dy_ * sa - dx_ * ca)) / np.sqrt(1 + a.hs_zf ** 2 * (dx_ * dx_ + dy_ * dy_))
        lit = s > cz
        al = np.where(lit, (s - cz) / (1 - cz) * a.hs_gl, (cz - s) / cz)
        sh[r0:r1] = np.clip(al, 0, 1) * a.hs_str * np.where(lit, 1, -1)
    return sh

def band(r0, r1, c0=0, c1=None):
    c1 = W if c1 is None else c1
    def split():
        cm = (c0 + c1) // 2
        if cm == c0: raise RuntimeError('Kachel kann nicht weiter geteilt werden')
        return np.concatenate((band(r0, r1, c0, cm), band(r0, r1, cm, c1)), axis=1)
    if (r1 - r0) * (c1 - c0) > MAX_BAND_PIXELS: return split()
    gx, gy = np.meshgrid(x0 + (np.arange(c0, c1) + .5) * dx, y1 - (np.arange(r0, r1) + .5) * dy)
    lon, lat = lonlat(gx, gy); ok = np.isfinite(lon) & np.isfinite(lat)
    if not ok.any(): return np.full((r1 - r0, c1 - c0, 3), 255, np.uint8)
    def sample(kind, zz):
        px, py = merc(lon, lat, zz); m, tx0, ty0 = mosaic(kind, zz, px, py, ok)
        return m, np.where(ok, px - tx0 * 256 - .5, 0).astype(np.float32), np.where(ok, py - ty0 * 256 - .5, 0).astype(np.float32), ty0
    try:
        if a.base == 'none': out = np.full((r1 - r0, c1 - c0, 3), 255, np.float32)
        else: m, u, v, _ = sample(a.base, z); out = bil(m, u, v)
        if hs:
            m, u, v, ty0 = sample('dem', zd); sh = bil(shade(m, ty0, zd), u, v)[..., None]
            out = np.where(sh > 0, out + (255 - out) * sh, out * (1 + sh))  # weiß bzw. schwarz mit Deckkraft |sh| darüber
    except MosaicTooLarge:
        return split()
    out[~ok] = 255
    return np.clip(out + .5, 0, 255).astype(np.uint8)

# --- Vektordaten: Grenzen + Beschriftung (in pt auf der Seite; wird für PDF als Vektor, für PNG per Pillow gezeichnet) ---
def ne(name):
    p = os.path.join(a.cache, 'ne', name + '.geojson')
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        r = S.get(f'{NE}/{name}.geojson', timeout=120); r.raise_for_status()
        with open(p, 'wb') as f: f.write(r.content)
    with open(p, encoding='utf-8') as f: return json.load(f)['features']

def topt(lon, lat):
    x, y = fwd.transform(lon, lat)
    return (np.asarray(x) - x0) / (x1 - x0) * pw, (np.asarray(y) - y0) / (y1 - y0) * ph

FONT, TTF = 'Helvetica-Bold', None
for p in (r'C:\Windows\Fonts\arialbd.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
    if os.path.exists(p): pdfmetrics.registerFont(TTFont('F', p)); FONT, TTF = 'F', p; break

lines, placed, items = [], [], []  # items: (text, Textanfang tx/ty (Grundlinie), Größe, Buchstabenabstand, Punkt (x, y, r) oder None)
fg, halo = ((1, 1, 1), (0, 0, 0)) if a.text == 'white' else ((0, 0, 0), (1, 1, 1))
def put(s, x, y, size, cs=0, dot=None):  # Name zentriert auf den Ort; dot='r'/'b' = mit Punkt, Name rechts davon bzw. darunter. False bei Kollision/Rand
    if not (math.isfinite(x) and math.isfinite(y)): return False
    w = pdfmetrics.stringWidth(s, FONT, size) + cs * (len(s) - 1)
    r = size * (.25 if dot == 'r' else .2)
    tx, ty = (x + r + .35 * size if dot == 'r' else x - w / 2), (y - 1.2 * size if dot == 'b' else y - .3 * size)
    box = (tx, ty - .2 * size, tx + w, ty + .8 * size)
    if dot: box = (min(box[0], x - r), min(box[1], y - r), max(box[2], x + r), max(box[3], y + r))
    if box[0] < 0 or box[1] < 0 or box[2] > pw or box[3] > ph or any(box[0] < q[2] and box[2] > q[0] and box[1] < q[3] and box[3] > q[1] for q in placed): return False
    placed.append(box); items.append((s, tx, ty, size, cs, (x, y, r) if dot else None))
    return True

if not a.no_labels:
    for f in ne('ne_10m_admin_0_boundary_lines_land'):
        g = f['geometry']
        for ln in g['coordinates'] if g['type'] == 'MultiLineString' else [g['coordinates']]:
            xs, ys = topt(*np.array(ln)[:, :2].T)
            if np.isfinite(xs).all() and np.isfinite(ys).all() and xs.max() >= 0 and xs.min() <= pw and ys.max() >= 0 and ys.min() <= ph: lines.append((xs, ys))
    n = [0, 0, 0]
    for f in ne('ne_110m_admin_0_countries'):
        p = f['properties']; x, y = topt(p['LABEL_X'], p['LABEL_Y'])
        n[0] += put(p['NAME'].upper(), float(x), float(y), a.pt * a.country_scale, a.pt * .35)
    Z = a.label_zoom
    for f in sorted((f for f in ne('ne_10m_populated_places_simple') if f['properties']['min_zoom'] <= Z), key=lambda f: (f['properties']['scalerank'], -(f['properties']['pop_max'] or 0))):
        x, y = topt(*f['geometry']['coordinates'][:2]); n[1] += put(f['properties']['name'], float(x), float(y), a.pt, 0, 'r' if a.dots else None)
    for f in sorted((f for f in ne('ne_10m_geography_regions_elevation_points') if f['properties']['featurecla'] == 'mountain' and f['properties']['min_zoom'] <= Z), key=lambda f: -(f['properties']['elevation'] or 0)):
        p = f['properties']; x, y = topt(*f['geometry']['coordinates'][:2])
        n[2] += put(f"{p.get('name_de') or p['name']} ({int(p['elevation'])} m)", float(x), float(y), a.pt * .8, 0, 'b' if a.dots else None)
    print(f'{len(lines)} Grenzlinien; Beschriftung: {n[0]} Länder, {n[1]} Städte, {n[2]} Berge')

def draw_pdf(c):
    c.saveState(); clip = c.beginPath(); clip.rect(0, 0, pw, ph); c.clipPath(clip, stroke=0, fill=0)
    c.setStrokeColorRGB(0, 0, 0); c.setLineWidth(a.pt * .12)
    for xs, ys in lines:
        p = c.beginPath(); p.moveTo(xs[0], ys[0])
        for xx, yy in zip(xs[1:], ys[1:]): p.lineTo(xx, yy)
        c.drawPath(p, stroke=1, fill=0)
    for s, tx, ty, size, cs, dot in items:
        c.setStrokeColorRGB(*halo); c.setFillColorRGB(*fg); c.setLineWidth(size * .15); c.setLineJoin(1)
        if dot: c.circle(*dot, stroke=0, fill=1)  # nur in Schriftfarbe, keine Umrandung
        for tr in (1, 0):  # erst Halo (Kontur), dann Füllung; reportlab schreibt '0 Tr' nie selbst, der Modus bleibt sonst hängen
            t = c.beginText(tx, ty); t.setFont(FONT, size); t.setCharSpace(cs); t.setTextRenderMode(tr) if tr else t._code.append('0 Tr'); t.setFillColorRGB(*fg); t.setStrokeColorRGB(*halo); t.textOut(s); c.drawText(t)
    c.restoreState()

pxk = W / pw  # Pixel pro pt
rgb = lambda col: tuple(int(v * 255) for v in col)
fonts = {}
def pil_font(px):
    px = max(1, round(px))
    if px not in fonts: fonts[px] = ImageFont.truetype(TTF, px) if TTF else ImageFont.load_default(size=px)
    return fonts[px]

def overlay(strip, s0):  # dieselben Vektordaten in ein Pixel-Streifen-Array zeichnen (Zeilen s0..s0+h)
    im = Image.fromarray(strip); d = ImageDraw.Draw(im); h = strip.shape[0]
    Y = lambda y: (ph - y) * pxk - s0
    for xs, ys in lines:
        if Y(ys.min()) < -50 or Y(ys.max()) > h + 50: continue  # Linie liegt ganz außerhalb des Streifens
        d.line(np.c_[xs * pxk, Y(ys)].ravel().tolist(), fill=(0, 0, 0), width=max(1, round(a.pt * .12 * pxk)), joint='curve')
    for s, tx, ty, size, cs, dot in items:
        if not -3 * size * pxk < Y(ty) < h + 3 * size * pxk: continue
        sw, f = max(1, round(size * .075 * pxk)), pil_font(size * pxk)
        if dot: d.ellipse(((dot[0] - dot[2]) * pxk, Y(dot[1]) - dot[2] * pxk, (dot[0] + dot[2]) * pxk, Y(dot[1]) + dot[2] * pxk), fill=rgb(fg))
        if cs:  # Pillow kennt keinen Buchstabenabstand -> Zeichen einzeln
            px = tx * pxk
            for ch in s: d.text((px, Y(ty)), ch, font=f, fill=rgb(fg), stroke_width=sw, stroke_fill=rgb(halo), anchor='ls'); px += f.getlength(ch) + cs * pxk
        else: d.text((tx * pxk, Y(ty)), s, font=f, fill=rgb(fg), stroke_width=sw, stroke_fill=rgb(halo), anchor='ls')
    return np.asarray(im)

class PNG:  # streamt zeilenweise, hält nie das ganze Bild im RAM (Pillow kann das nicht)
    def __init__(self, path, dpi):
        self.f = open(path, 'wb'); self.f.write(b'\x89PNG\r\n\x1a\n')
        self.chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
        ppm = round(dpi / 0.0254); self.chunk(b'pHYs', struct.pack('>IIB', ppm, ppm, 1))
        self.z = zlib.compressobj(4)
    def chunk(self, t, d): self.f.write(struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d)))
    def write(self, rows):
        flat = rows.reshape(len(rows), -1); buf = np.empty((len(rows), flat.shape[1] + 1), np.uint8)
        buf[:, 0] = 1; buf[:, 1:] = flat; buf[:, 4:] -= flat[:, :-3]  # PNG-Filter "Sub" (Differenz zum linken Pixel): deutlich kleinere Datei
        d = self.z.compress(buf.tobytes())
        if d: self.chunk(b'IDAT', d)
    def close(self): self.chunk(b'IDAT', self.z.flush()); self.chunk(b'IEND', b''); self.f.close()

# --- Ausgabe ---
png = a.out.lower().endswith('.png')
if png and a.tile_mm: raise ValueError('--tile-mm ist nur für PDF verfügbar')
tmp = tempfile.mkdtemp(prefix='poster_')
c = None if png else canvas.Canvas(a.out, pagesize=(pw, ph), pageCompression=1)
out = PNG(a.out, a.dpi) if png else None
k = pw / W  # pt pro Pixel
tile = max(1, round(a.tile_mm / 25.4 * a.dpi)) if a.tile_mm else max(W, H)
if not png: tile = min(tile, MAX_JPEG_DIMENSION)  # JPEG akzeptiert höchstens 65.500 px je Kante
tiles = [(r0, min(r0 + tile, H), c0, min(c0 + tile, W)) for r0 in range(0, H, tile) for c0 in range(0, W, tile)]
for page, (r0, r1, c0, c1) in enumerate(tiles, 1):
    strip_rows = min(a.strip, max(1, MAX_JPEG_PIXELS // (c1 - c0)))
    for s0 in range(r0, r1, strip_rows):
        s1 = min(s0 + strip_rows, r1); strip = np.empty((s1 - s0, c1 - c0, 3), np.uint8)
        for r in range(s0, s1, 256):
            strip[r - s0:min(r + 256, s1) - s0] = band(r, min(r + 256, s1), c0, c1); print(f'Zeilen {min(r + 256, s1)}/{H}', flush=True)
        if png: out.write(overlay(strip, s0))
        else:
            f = os.path.join(tmp, f'{page}_{s0}.jpg'); ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, strip.nbytes)
            try: Image.fromarray(strip).save(f, quality=a.quality)
            except OSError:
                f = os.path.join(tmp, f'{page}_{s0}.png'); Image.fromarray(strip).save(f, format='PNG')
            c.drawImage(f, c0 * k, ph - s1 * k, width=(c1 - c0) * k, height=(s1 - s0) * k)
        print(f'Streifen {s1}/{H} geschrieben', flush=True)
    if not png: print(f'Kacheln {page}/{len(tiles)} geschrieben', flush=True)
if png: out.close()
else: print('Beschriftung + PDF speichern…', flush=True); draw_pdf(c); c.showPage(); c.save()
shutil.rmtree(tmp, ignore_errors=True)
print(f'Fertig: {a.out} ({os.path.getsize(a.out) / 1e6:.1f} MB, {a.width_mm:.0f}x{a.width_mm * H / W:.0f} mm)')
