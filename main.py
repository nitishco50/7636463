import os
import re
import sys
import json
import time
import random
import hashlib
import ipaddress
import datetime
import urllib.request
import urllib.parse
import threading
from collections import defaultdict
from threading import Lock
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# Load .env file manually
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key, val = key.strip(), val.strip()
                if val and val[0] in ('"', "'") and val[-1] == val[0]:
                    val = val[1:-1]
                if val:
                    os.environ[key] = val
_load_env()

app = Flask(__name__)

# CORS: restrict to frontend domain(s)
_cors_origins = os.environ.get('CORS_ORIGINS', '*')
if _cors_origins != '*':
    _cors_origins = [o.strip() for o in _cors_origins.split(',') if o.strip()]
CORS(app, origins=_cors_origins, methods=['GET', 'POST', 'PUT', 'DELETE'], allow_headers=['Content-Type', 'X-Admin-Token'])

_start_time = time.time()
BACKEND_ADMIN_TOKEN = os.environ.get('BACKEND_ADMIN_TOKEN', '')
if not BACKEND_ADMIN_TOKEN:
    print('WARNING: BACKEND_ADMIN_TOKEN not set!', flush=True)

# ============================================================
# ANTI-DETECTION: User-Agent Rotation Pool
# ============================================================
USER_AGENTS = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5.1 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.186 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.244 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-A536B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.5790.166 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.118 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/124.0.6367.88 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.71 Mobile Safari/537.36',
    'Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; RMX3771) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.113 Mobile Safari/537.36',
]

ACCEPT_LANGUAGES = [
    'en-US,en;q=0.9',
    'en-US,en;q=0.9,hi;q=0.8',
    'en-GB,en;q=0.9',
    'en-US,en;q=0.9,es;q=0.8',
    'hi-IN,hi;q=0.9,en-US;q=0.8,en;q=0.7',
]

GOOGLEBOT_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'

# ============================================================
# GLOBAL REQUEST THROTTLER
# ============================================================
_last_ig_request_time = 0
_ig_throttle_lock = Lock()
MIN_IG_GAP = 1.5

def throttle_ig_request():
    global _last_ig_request_time
    with _ig_throttle_lock:
        now = time.time()
        elapsed = now - _last_ig_request_time
        if elapsed < MIN_IG_GAP:
            wait = MIN_IG_GAP - elapsed + random.uniform(0.2, 0.6)
            time.sleep(wait)
        _last_ig_request_time = time.time()

# ============================================================
# PERSISTENT CACHE
# ============================================================
_cache = {}
_cache_lock = Lock()
CACHE_TTL = 900  # 15 minutes
CACHE_FILE = '/tmp/instasave_cache.json'

def _load_cache():
    global _cache
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
            now = time.time()
            _cache = {k: v for k, v in data.items()
                      if now - v.get('ts', 0) < CACHE_TTL}
            print(f'Loaded {len(_cache)} cache entries', flush=True)
    except Exception as e:
        print(f'Cache load error: {e}', flush=True)

def _save_cache():
    try:
        with _cache_lock:
            data = {k: v for k, v in _cache.items()}
        with open(CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def cache_get(key):
    with _cache_lock:
        if key in _cache:
            entry = _cache[key]
            if time.time() - entry.get('ts', 0) < CACHE_TTL:
                return entry.get('data')
            del _cache[key]
    return None

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    if random.randint(1, 5) == 1:
        threading.Thread(target=_save_cache, daemon=True).start()

_load_cache()

# ============================================================
# PER-IP RATE LIMITER
# ============================================================
_rate_limits = defaultdict(list)
_rate_lock = Lock()
MAX_REQUESTS_PER_MINUTE = 10
MAX_REQUESTS_PER_HOUR = 100

def check_rate_limit(ip):
    now = time.time()
    with _rate_lock:
        _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < 3600]
        recent = [t for t in _rate_limits[ip] if now - t < 60]
        if len(recent) >= MAX_REQUESTS_PER_MINUTE:
            return False, 'Rate limit: max 10 requests per minute.'
        if len(_rate_limits[ip]) >= MAX_REQUESTS_PER_HOUR:
            return False, 'Rate limit: max 100 requests per hour.'
        _rate_limits[ip].append(now)
    return True, ''

# ============================================================
# PROXY POOL (for production)
# ============================================================
_proxy_list = []
_proxy_lock = Lock()
_proxy_index = 0

def load_proxies():
    """Load proxies from environment or file."""
    global _proxy_list
    # From environment variable: PROXIES=socks5://user:pass@host:port,...
    env = os.environ.get('PROXIES', '')
    if env:
        _proxy_list = [p.strip() for p in env.split(',') if p.strip()]
    # From file
    proxy_file = os.path.join(os.path.dirname(__file__), 'proxies.txt')
    if os.path.exists(proxy_file):
        with open(proxy_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    _proxy_list.append(line)
    print(f'Loaded {len(_proxy_list)} proxies', flush=True)

def get_next_proxy():
    global _proxy_index
    with _proxy_lock:
        if not _proxy_list:
            return None
        proxy = _proxy_list[_proxy_index % len(_proxy_list)]
        _proxy_index += 1
        return proxy

load_proxies()

# ============================================================
# HTTP REQUEST BUILDER
# ============================================================
def make_request(url, timeout=15, use_proxy=False, ua=None):
    """Make HTTP request with optional proxy and custom UA."""
    headers = {
        'User-Agent': ua or random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': random.choice(ACCEPT_LANGUAGES),
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive',
        'Referer': 'https://www.instagram.com/',
    }

    req = urllib.request.Request(url, headers=headers)

    # Proxy support
    if use_proxy:
        proxy = get_next_proxy()
        if proxy:
            handler = urllib.request.ProxyHandler({
                'http': proxy,
                'https': proxy,
            })
            opener = urllib.request.build_opener(handler)
            return opener.open(req, timeout=timeout)

    return urllib.request.urlopen(req, timeout=timeout)

INSTAGRAM_RE = re.compile(r'instagram\.com/(?:p|reel|tv|reels)/([A-Za-z0-9_-]+)')

def get_shortcode(url):
    m = INSTAGRAM_RE.search(url)
    return m.group(1) if m else None

# SSRF Protection: block private/internal IPs
BLOCKED_NETWORKS = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('0.0.0.0/8'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
]

ALLOWED_DOMAINS = ['instagram.com', 'www.instagram.com', 'cdninstagram.com', 'scontent']

def is_safe_url(url):
    """Validate URL is not targeting internal/private IPs."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Check domain whitelist for Instagram endpoints
        if not any(d in hostname for d in ALLOWED_DOMAINS):
            return False
        # Resolve and check for private IPs
        import socket
        try:
            addr = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
            for family, _, _, _, sockaddr in addr:
                ip = ipaddress.ip_address(sockaddr[0])
                for net in BLOCKED_NETWORKS:
                    if ip in net:
                        return False
        except (socket.gaierror, ValueError):
            return False
        return True
    except Exception:
        return False

# ============================================================
# STRATEGY 1: Instagram oEmbed API (Official, 1000 req/hr)
# ============================================================
def extract_oembed(shortcode):
    """Use Instagram's official oEmbed API."""
    try:
        url = f'https://www.instagram.com/api/v1/oembed/?url=https://www.instagram.com/p/{shortcode}/'
        throttle_ig_request()
        resp = make_request(url, timeout=10)
        data = json.loads(resp.read().decode('utf-8'))

        thumbnail = data.get('thumbnail_url', '')
        author = data.get('author_name', '')
        title = data.get('title', '')

        return {
            'post_type': 'GraphVideo' if '/reel/' in url or '/tv/' in url else 'GraphSidecar',
            'images': [thumbnail] if thumbnail else [],
            'videos': [],
            'caption': title[:100] if title else '',
            'author': author,
            'strategy': 'oembed',
        }
    except Exception as e:
        print(f'  oembed failed for {shortcode}: {e}', flush=True)
        return None

# ============================================================
# STRATEGY 2: Googlebot UA (Instagram doesn't block Google)
# ============================================================
def extract_googlebot(shortcode):
    """Use Googlebot UA - Instagram never blocks Google's crawler."""
    html = None
    for prefix in ['p', 'reel']:
        try:
            throttle_ig_request()
            url = f'https://www.instagram.com/{prefix}/{shortcode}/embed/?cr=1&v=14'
            resp = make_request(url, timeout=15, ua=GOOGLEBOT_UA)
            html = resp.read().decode('utf-8', errors='ignore')
            if 'gql_data' in html or 'display_url' in html:
                break
            html = None
        except:
            continue

    if not html:
        return None

    return _parse_embed(html, shortcode, 'googlebot')

# ============================================================
# STRATEGY 3: Mobile Embed (current method)
# ============================================================
def extract_mobile_embed(shortcode, max_attempts=3):
    """Standard mobile embed with retry."""
    html = None
    for attempt in range(max_attempts):
        for prefix in ['p', 'reel']:
            try:
                throttle_ig_request()
                if attempt > 0:
                    delay = 1.0 * (1.5 ** attempt) + random.uniform(0.3, 0.8)
                    time.sleep(delay)

                url = f'https://www.instagram.com/{prefix}/{shortcode}/embed/?cr=1&v=14'
                resp = make_request(url, timeout=15)
                html = resp.read().decode('utf-8', errors='ignore')

                if 'Please wait a few minutes' in html:
                    html = None
                    continue
                if len(html) < 500:
                    html = None
                    continue
                if 'gql_data' in html or 'display_url' in html:
                    break
                html = None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                continue
            except:
                continue
        if html:
            break

    if not html:
        return None

    return _parse_embed(html, shortcode, 'mobile_embed')

# ============================================================
# STRATEGY 4: Proxy Embed (for production)
# ============================================================
def extract_proxy_embed(shortcode):
    """Use proxy rotation for extraction."""
    if not _proxy_list:
        return None

    for prefix in ['p', 'reel']:
        try:
            throttle_ig_request()
            url = f'https://www.instagram.com/{prefix}/{shortcode}/embed/?cr=1&v=14'
            resp = make_request(url, timeout=15, use_proxy=True)
            html = resp.read().decode('utf-8', errors='ignore')
            if 'gql_data' in html or 'display_url' in html:
                return _parse_embed(html, shortcode, 'proxy')
        except:
            continue
    return None

# ============================================================
# SHARED EMBED PARSER
# ============================================================
def _parse_embed(html, shortcode, strategy):
    """Parse Instagram embed page HTML into result dict."""
    chunk = (html
             .replace('\\\\/', '/')
             .replace('\\"', '"')
             .replace('\\/', '/')
             .replace('\\u0025', '%')
             .replace('\\u0026', '&'))

    images = []
    videos = []
    seen = set()

    # Image URLs
    for m in re.finditer(r'"display_url"\s*:\s*"(https?://[^"]+)"', chunk):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            images.append(u)

    # Video URLs — multiple patterns
    for pattern in [
        r'"video_url"\s*:\s*"(https?://[^"]+)"',
        r'video_url\s*:\s*"(https?://[^"]+)"',
        r'<video[^>]+src="([^"]+)"',
        r'"url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'url\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
    ]:
        for m in re.finditer(pattern, chunk):
            u = m.group(1)
            if u not in seen and ('.mp4' in u or 'video' in u):
                seen.add(u)
                videos.append(u)

    # Fallback: raw HTML
    if not videos:
        for pattern in [
            r'"video_url"\s*:\s*"(https?://[^"]+)"',
            r'video_url\s*:\s*"(https?://[^"]+)"',
            r'<video[^>]+src="([^"]+)"',
        ]:
            for m in re.finditer(pattern, html):
                u = m.group(1)
                if u not in seen and ('.mp4' in u or 'video' in u):
                    seen.add(u)
                    videos.append(u)

    types = re.findall(r'"__typename"\s*:\s*"(Graph\w+)"', chunk)
    post_type = types[0] if types else 'GraphImage'

    caption = ''
    cap_m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
    if cap_m:
        caption = cap_m.group(1).replace('\\n', '\n').replace('\\u0040', '@')
        caption = re.sub(r'\\ud[0-9a-f]{3}\\\\?d[0-9a-f]{3}', '', caption)
        caption = caption[:100]

    author_m = re.search(r'"username"\s*:\s*"([^"]+)"', chunk)
    author = author_m.group(1) if author_m else ''

    print(f'  [{strategy}] {shortcode}: imgs={len(images)} vids={len(videos)} type={post_type}', flush=True)

    return {
        'post_type': post_type,
        'images': images,
        'videos': videos,
        'caption': caption,
        'author': author,
        'strategy': strategy,
    }

# ============================================================
# SMART EXTRACTION — tries all strategies in order
# ============================================================
def smart_extract(shortcode):
    """Try multiple extraction strategies with smart fallback.
    Order: googlebot (fast, reliable) → mobile_embed (no proxy needed) → oembed (thumbnail only) → proxy
    """
    cached = cache_get(f'embed:{shortcode}')
    if cached:
        cached['strategy'] = 'cache'
        return cached

    strategies = [
        ('googlebot', extract_googlebot),
        ('mobile_embed', extract_mobile_embed),
    ]

    if _proxy_list:
        strategies.append(('proxy', extract_proxy_embed))

    # oEmbed last — it only gives thumbnails, not actual video URLs
    strategies.append(('oembed', extract_oembed))

    for name, func in strategies:
        try:
            print(f'  trying {name} for {shortcode}...', flush=True)
            result = func(shortcode)
            if result and result.get('videos'):
                result['strategy'] = name
                cache_set(f'embed:{shortcode}', result)
                return result
            # For image posts, check if we got real media URLs (not just oembed thumbnails)
            if result and result.get('images') and name != 'oembed':
                result['strategy'] = name
                cache_set(f'embed:{shortcode}', result)
                return result
        except Exception as e:
            print(f'  {name} error: {e}', flush=True)
            continue

    return None


# ============================================================
# API Routes
# ============================================================
@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'service': 'instasave-unified',
        'strategies': ['oembed', 'googlebot', 'mobile_embed'] + (['proxy'] if _proxy_list else []),
        'proxies': len(_proxy_list),
        'cache_entries': len(_cache),
    })


@app.route('/extract', methods=['POST'])
def home_extract():
    ip = request.remote_addr or '0.0.0.0'
    ok, msg = check_rate_limit(ip)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 429

    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    if not url or 'instagram.com' not in url:
        return jsonify({'success': False, 'error': 'Invalid Instagram URL'}), 400

    shortcode = get_shortcode(url)
    if not shortcode:
        return jsonify({'success': False, 'error': 'Could not extract shortcode'}), 400

    result = smart_extract(shortcode)
    if not result:
        log_download(ip, shortcode, 'video', success=False, error='extraction_failed')
        return jsonify({'success': False, 'error': 'Could not extract. Post may be private or temporarily unavailable.'}), 400

    if result['videos']:
        log_download(ip, shortcode, 'video', media_url=result['videos'][0])
        return jsonify({
            'success': True,
            'video_url': result['videos'][0],
            'thumbnail': result['images'][0] if result['images'] else '',
            'title': result['caption'] or 'Instagram Video',
            'uploader': result['author'],
            'strategy': result.get('strategy', 'unknown'),
        })
    elif result['images']:
        log_download(ip, shortcode, 'image', media_url=result['images'][0])
        return jsonify({
            'success': True,
            'video_url': None,
            'image_url': result['images'][0],
            'thumbnail': result['images'][0],
            'title': result['caption'] or 'Instagram Image',
            'uploader': result['author'],
            'strategy': result.get('strategy', 'unknown'),
        })
    return jsonify({'success': False, 'error': 'No media found'}), 400


@app.route('/reel/extract', methods=['POST'])
def reel_extract():
    ip = request.remote_addr or '0.0.0.0'
    ok, msg = check_rate_limit(ip)
    if not ok:
        return jsonify({'error': msg}), 429

    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    if not url or 'instagram.com' not in url:
        return jsonify({'error': 'Invalid Instagram URL'}), 400

    shortcode = get_shortcode(url)
    if not shortcode:
        return jsonify({'error': 'Could not extract shortcode'}), 400

    result = smart_extract(shortcode)
    if not result:
        log_download(ip, shortcode, 'reel', success=False, error='extraction_failed')
        return jsonify({'error': 'Could not extract. Post may be private or temporarily unavailable.'}), 400

    images = result['images']
    videos = result['videos']

    if result['post_type'] == 'GraphVideo' or videos:
        video_url = videos[0] if videos else (images[0] if images else '')
        log_download(ip, shortcode, 'reel', media_url=video_url)
        return jsonify({
            'type': 'video',
            'video_url': video_url,
            'thumbnail': images[0] if images else '',
            'title': result['caption'] or 'Instagram Reel',
            'author': result['author'],
            'strategy': result.get('strategy', 'unknown'),
        })

    media = [{'type': 'image', 'url': u} for u in images] + [{'type': 'video', 'url': u} for u in videos]
    if media:
        log_download(ip, shortcode, 'reel', media_url=media[0]['url'])
        return jsonify({
            'type': 'sidecar',
            'video_url': videos[0] if videos else (images[0] if images else ''),
            'media': media,
            'media_count': len(media),
            'title': result['caption'] or 'Instagram Post',
            'author': result['author'],
            'strategy': result.get('strategy', 'unknown'),
        })

    log_download(ip, shortcode, 'reel', success=False, error='no_media')
    return jsonify({'error': 'No media found'}), 400


@app.route('/post/extract', methods=['POST'])
def post_extract():
    ip = request.remote_addr or '0.0.0.0'
    ok, msg = check_rate_limit(ip)
    if not ok:
        return jsonify({'error': msg}), 429

    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    if not url or 'instagram.com' not in url:
        return jsonify({'error': 'Invalid Instagram URL'}), 400

    shortcode = get_shortcode(url)
    if not shortcode:
        return jsonify({'error': 'Could not extract shortcode'}), 400

    result = smart_extract(shortcode)
    if not result:
        return jsonify({'error': 'Could not extract. Post may be private or temporarily unavailable.'}), 400

    media = [{'type': 'image', 'url': u} for u in result['images']] + [{'type': 'video', 'url': u} for u in result['videos']]
    if media:
        return jsonify({
            'media': media,
            'title': result['caption'] or 'Instagram Post',
            'author': result['author'],
            'media_count': len(media),
            'strategy': result.get('strategy', 'unknown'),
        })

    return jsonify({'error': 'No media found'}), 400


def log_download(ip, shortcode, dtype, media_url=None, success=True, error=None):
    entry = {
        'ip': ip, 'shortcode': shortcode, 'type': dtype,
        'media_url': media_url, 'success': success, 'error': error,
        'time': time.time(),
    }
    _download_log.append(entry)
    if len(_download_log) > 2000:
        del _download_log[:500]

_download_log = []
_download_log_lock = Lock()


@app.route('/carousel/extract', methods=['POST'])
def carousel_extract():
    return post_extract()


@app.route('/proxy-download')
def proxy_download():
    media_url = request.args.get('url', '').strip()
    if not media_url or not media_url.startswith('http'):
        return jsonify({'error': 'Invalid URL'}), 400

    # SSRF protection: validate URL
    if not is_safe_url(media_url):
        return jsonify({'error': 'URL not allowed'}), 400

    # Rate limit proxy downloads
    ip = request.remote_addr or '0.0.0.0'
    ok, msg = check_rate_limit(ip)
    if not ok:
        return jsonify({'error': msg}), 429

    try:
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Referer': 'https://www.instagram.com/',
            'Accept': '*/*',
        }
        req = urllib.request.Request(media_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30)
        ct = resp.headers.get('Content-Type', 'application/octet-stream')
        if '.mp4' in media_url or 'video' in ct:
            ct = 'video/mp4'
            fn = 'instasave_video.mp4'
        else:
            ct = 'image/jpeg'
            fn = 'instasave_image.jpg'
        cl = resp.headers.get('Content-Length')
        hdrs = {'Content-Type': ct, 'Content-Disposition': f'attachment; filename="{fn}"', 'Cache-Control': 'no-cache'}
        if cl:
            hdrs['Content-Length'] = cl
        return Response(iter(lambda: resp.read(8192), b''), headers=hdrs)
    except Exception as e:
        print(f'proxy-download error: {e}', flush=True)
        return jsonify({'error': 'Download failed'}), 500


# ============================================================
# ADMIN ROUTES
# ============================================================
def check_admin():
    token = request.headers.get('X-Admin-Token', '')
    if token != BACKEND_ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401
    return None

@app.route('/admin/health')
def admin_health():
    err = check_admin()
    if err: return err
    return jsonify({
        'status': 'healthy',
        'uptime': time.time() - _start_time,
        'cache_entries': len(_cache),
        'rate_limit_ips': len(_rate_limits),
        'strategies': ['oembed', 'googlebot', 'mobile_embed'] + (['proxy'] if _proxy_list else []),
        'proxies': len(_proxy_list),
    })

@app.route('/admin/rate-limit-config', methods=['GET', 'PUT'])
def admin_rate_limit():
    err = check_admin()
    if err: return err
    global MAX_REQUESTS_PER_MINUTE, MAX_REQUESTS_PER_HOUR
    if request.method == 'PUT':
        data = request.get_json(force=True)
        if 'max_per_minute' in data:
            MAX_REQUESTS_PER_MINUTE = int(data['max_per_minute'])
        if 'max_per_hour' in data:
            MAX_REQUESTS_PER_HOUR = int(data['max_per_hour'])
        return jsonify({'success': True})
    return jsonify({'max_per_minute': MAX_REQUESTS_PER_MINUTE, 'max_per_hour': MAX_REQUESTS_PER_HOUR})

@app.route('/admin/blocked-ips', methods=['GET'])
def admin_blocked_ips():
    err = check_admin()
    if err: return err
    return jsonify({'blocked': []})

@app.route('/admin/cache-stats')
def admin_cache_stats():
    err = check_admin()
    if err: return err
    return jsonify({'entries': len(_cache), 'ttl': CACHE_TTL})

@app.route('/admin/clear-cache', methods=['POST'])
def admin_clear_cache():
    err = check_admin()
    if err: return err
    with _cache_lock:
        _cache.clear()
    _save_cache()
    return jsonify({'success': True, 'message': 'Cache cleared'})

@app.route('/admin/health-details')
def admin_health_details():
    err = check_admin()
    if err: return err
    return jsonify({
        'status': 'healthy',
        'python_version': sys.version,
        'uptime': time.time() - _start_time,
        'cache_entries': len(_cache),
        'rate_limit_ips': len(_rate_limits),
        'strategies': ['oembed', 'googlebot', 'mobile_embed'] + (['proxy'] if _proxy_list else []),
        'proxies': len(_proxy_list),
        'ig_min_gap': MIN_IG_GAP,
    })

@app.route('/admin/server-metrics')
def admin_server_metrics():
    err = check_admin()
    if err: return err
    cpu = ram_total = ram_used = ram_percent = disk_total = disk_used = disk_percent = 0
    load_avg = [0, 0, 0]
    process_count = 0
    uptime_val = time.time() - _start_time
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
            cpu = float(parts[1]) + float(parts[2]) + float(parts[3])
            cpu_total = sum(float(x) for x in parts[1:])
            cpu = round((cpu / cpu_total) * 100, 1) if cpu_total else 0
    except: pass
    try:
        with open('/proc/meminfo') as f:
            info = {}
            for line in f:
                parts = line.split()
                info[parts[0].rstrip(':')] = int(parts[1]) * 1024
            ram_total = info.get('MemTotal', 0)
            ram_free = info.get('MemAvailable', info.get('MemFree', 0))
            ram_used = ram_total - ram_free
            ram_percent = round((ram_used / ram_total) * 100, 1) if ram_total else 0
    except: pass
    try:
        st = os.statvfs('/')
        disk_total = st.f_blocks * st.f_frsize
        disk_used = (st.f_blocks - st.f_bfree) * st.f_frsize
        disk_percent = round((disk_used / disk_total) * 100, 1) if disk_total else 0
    except: pass
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
            load_avg = [float(parts[0]), float(parts[1]), float(parts[2])]
    except: pass
    try:
        process_count = len(os.listdir('/proc'))
    except: pass
    try:
        with open('/proc/uptime') as f:
            uptime_val = float(f.read().split()[0])
    except: pass
    return jsonify({
        'cpu_percent': cpu, 'ram_total': ram_total, 'ram_used': ram_used,
        'ram_percent': ram_percent, 'disk_total': disk_total, 'disk_used': disk_used,
        'disk_percent': disk_percent, 'uptime': uptime_val, 'boot_time': 0,
        'load_avg': load_avg, 'process_count': process_count,
    })

@app.route('/admin/service-status')
def admin_service_status():
    err = check_admin()
    if err: return err
    services = {
        'backend': {'status': 'online', 'uptime': time.time() - _start_time},
        'cache': {'status': 'online', 'entries': len(_cache)},
        'rate_limiter': {'status': 'online', 'tracked_ips': len(_rate_limits)},
        'strategies': {'status': 'online', 'count': len(['oembed', 'googlebot', 'mobile_embed'])},
    }
    try:
        import pymysql
        conn = pymysql.connect(host='127.0.0.1', port=3306, user=os.environ.get('DB_USER', 'instasave_admin'),
                              password=os.environ.get('DB_PASS', ''), database=os.environ.get('DB_NAME', 'instasave'), connect_timeout=2)
        conn.ping(reconnect=False)
        services['database'] = {'status': 'online', 'type': 'MariaDB'}
        conn.close()
    except:
        services['database'] = {'status': 'offline', 'type': 'MariaDB'}
    return jsonify(services)

@app.route('/admin/live-activity')
def admin_live_activity():
    err = check_admin()
    if err: return err
    with _download_log_lock:
        recent = list(_download_log[-50:])
    return jsonify({'activity': recent, 'total_today': len(_download_log)})

@app.route('/admin/download-by-type')
def admin_download_by_type():
    err = check_admin()
    if err: return err
    counts = {'video': 0, 'reel': 0, 'post': 0, 'carousel': 0, 'image': 0}
    with _download_log_lock:
        for entry in _download_log:
            t = entry.get('type', 'unknown')
            if t in counts:
                counts[t] += 1
    return jsonify(counts)

@app.route('/admin/download-timeline')
def admin_download_timeline():
    err = check_admin()
    if err: return err
    hours = {}
    with _download_log_lock:
        for entry in _download_log:
            ts = entry.get('time', 0)
            h = datetime.datetime.fromtimestamp(ts).strftime('%H:00') if ts else 'unknown'
            hours[h] = hours.get(h, 0) + 1
    return jsonify(hours)

@app.route('/admin/test-api', methods=['POST'])
def admin_test_api():
    err = check_admin()
    if err: return err
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    shortcode = get_shortcode(url)
    if not shortcode:
        return jsonify({'error': 'Invalid URL'}), 400
    with _cache_lock:
        _cache.pop(f'embed:{shortcode}', None)
    result = smart_extract(shortcode)
    if not result:
        return jsonify({'error': 'Extraction failed'}), 400
    media = [{'type': 'image', 'url': u} for u in result['images']] + [{'type': 'video', 'url': u} for u in result['videos']]
    return jsonify({
        'success': True, 'media': media, 'author': result['author'],
        'post_type': result['post_type'], 'strategy': result.get('strategy', 'unknown'),
    })

@app.route('/debug/extract', methods=['POST'])
def debug_extract():
    err = check_admin()
    if err: return err
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    shortcode = get_shortcode(url)
    if not shortcode:
        return jsonify({'error': 'no shortcode'}), 400
    with _cache_lock:
        _cache.pop(f'embed:{shortcode}', None)
    result = smart_extract(shortcode)
    if not result:
        return jsonify({'error': 'no result', 'shortcode': shortcode}), 400
    return jsonify({
        'shortcode': shortcode, 'post_type': result['post_type'],
        'images_count': len(result['images']), 'videos_count': len(result['videos']),
        'first_video': result['videos'][0] if result['videos'] else None,
        'first_image': result['images'][0] if result['images'] else None,
        'author': result['author'], 'strategy': result.get('strategy', 'unknown'),
    })

@app.route('/admin/proxy-list', methods=['GET', 'POST'])
def admin_proxy_list():
    err = check_admin()
    if err: return err
    global _proxy_list
    if request.method == 'POST':
        data = request.get_json(force=True)
        if 'proxies' in data:
            _proxy_list = data['proxies']
        elif 'add' in data:
            _proxy_list.append(data['add'])
        return jsonify({'success': True, 'count': len(_proxy_list)})
    return jsonify({'proxies': _proxy_list, 'count': len(_proxy_list)})


# ============================================================
# DIRECT DOWNLOAD - Simple file serve (no CORS issues)
# ============================================================
@app.route('/download')
def direct_download():
    media_url = request.args.get('url', '').strip()
    if not media_url or not media_url.startswith('http'):
        return 'Invalid URL', 400

    # SSRF protection: only allow instagram CDN
    ALLOWED_DL = ['instagram.com', 'cdninstagram.com', 'fbcdn.net', 'scontent']
    try:
        hostname = urllib.parse.urlparse(media_url).hostname or ''
        if not any(d in hostname for d in ALLOWED_DL):
            return 'URL not allowed', 403
    except:
        return 'Invalid URL', 400

    try:
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Referer': 'https://www.instagram.com/',
            'Accept': '*/*',
        }
        req = urllib.request.Request(media_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30)

        ct = resp.headers.get('Content-Type', 'application/octet-stream')
        if '.mp4' in media_url or 'video' in ct:
            ct = 'video/mp4'
            fn = 'instasave_video.mp4'
        else:
            ct = 'image/jpeg'
            fn = 'instasave_image.jpg'

        cl = resp.headers.get('Content-Length')

        def generate():
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk

        headers_resp = {
            'Content-Type': ct,
            'Content-Disposition': f'attachment; filename="{fn}"',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Access-Control-Allow-Origin': '*',
        }
        if cl:
            headers_resp['Content-Length'] = cl

        return Response(generate(), headers=headers_resp)

    except Exception as e:
        print(f'Direct download error: {e}', flush=True)
        return 'Download failed', 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print(f'InstaSave Backend v3 (multi-strategy) running on port {port}...', flush=True)
    print(f'  Strategies: oembed, googlebot, mobile_embed' + (', proxy' if _proxy_list else ''), flush=True)
    print(f'  Proxies: {len(_proxy_list)}', flush=True)
    print(f'  Cache TTL: {CACHE_TTL}s', flush=True)
    print(f'  Min IG gap: {MIN_IG_GAP}s', flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
