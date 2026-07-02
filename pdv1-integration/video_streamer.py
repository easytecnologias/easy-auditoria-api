#!/usr/bin/env python3.8
"""
Servidor de streaming de video do DVR iMHDX.
- GET /?start=...&end=...&token=...   → stream fMP4 de intervalo explícito
- GET /cupom/NUMBER?token=...          → busca cupom no spy file e stream
- GET /cupom/NUMBER/info?token=...     → retorna JSON com start/end do cupom
Porta: 8765
"""
import os, re, json, datetime, pathlib, subprocess, threading, urllib.parse, tempfile, hashlib, time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Cache de clips gerados: {token: (filepath, expires_ts)}
_clip_cache = {}
_clip_lock  = threading.Lock()
CLIP_TTL    = 300  # 5 minutos

def _clip_cleanup():
    now = time.time()
    with _clip_lock:
        expired = [k for k, (f, e) in _clip_cache.items() if now > e]
        for k in expired:
            try: os.unlink(_clip_cache[k][0])
            except: pass
            del _clip_cache[k]

HOST      = os.environ.get("IMHDX_HOST",    "")
USER      = os.environ.get("IMHDX_USER",    "")
PASS      = os.environ.get("IMHDX_PASS",    "")
CHANNEL   = os.environ.get("IMHDX_CHANNEL", "1")
TOKEN     = os.environ.get("AUDITORIA_API_TOKEN", "")
PORT      = int(os.environ.get("VIDEO_STREAMER_PORT", "8765"))
PDV_BASE  = os.environ.get("PDV_BASE_DIR", "/home/rpdv/frente")
PDV_STATION = os.environ.get("PDV_STATION", "001")

# ── Relay de câmera ao vivo (conexão persistente com o DVR) ──────────────────
# O endpoint MJPEG do DVR leva ~15-20s pra "esquentar" a primeira conexão.
# Por isso mantemos UMA conexão de fundo aberta enquanto houver viewers,
# assim quem entra depois do primeiro recebe o frame na hora.
_live_lock        = threading.Lock()
_live_last_frame  = None     # bytes do último JPEG recebido
_live_last_ts     = 0.0      # quando o último frame chegou
_live_viewers     = 0        # quantos clientes HTTP estão assistindo agora
_live_thread      = None     # thread de relay ativa
_live_stop_flag   = False    # sinaliza pra thread encerrar
_live_idle_since  = None     # timestamp de quando o último viewer saiu
_LIVE_IDLE_TIMEOUT = 30      # segundos sem viewers até a thread encerrar sozinha

def _live_should_stop():
    """Decide se a thread de relay deve encerrar (sem chamadas há mais de IDLE_TIMEOUT)."""
    with _live_lock:
        if _live_stop_flag:
            return True
        if _live_viewers > 0:
            return False
        if _live_idle_since is None:
            return False
        return (time.time() - _live_idle_since) > _LIVE_IDLE_TIMEOUT

def _live_relay_loop():
    global _live_last_frame, _live_last_ts
    url = "http://%s/cgi-bin/mjpg/video.cgi?channel=%s&subtype=1" % (HOST, CHANNEL)
    print("live-relay: iniciando conexao com a camera", flush=True)
    while True:
        if _live_should_stop():
            print("live-relay: encerrando (idle)", flush=True)
            return
        curl = subprocess.Popen(
            ["curl", "-s", "--no-buffer", "--digest", "-u", "%s:%s" % (USER, PASS), url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
        try:
            buf = b""
            while True:
                if _live_should_stop():
                    curl.kill()
                    return
                chunk = curl.stdout.read(4096)
                if not chunk:
                    break  # conexão caiu, sai do while interno pra reconectar
                buf += chunk
                # Procura cabeçalho Content-Length de cada parte multipart
                while True:
                    m = re.search(br"Content-Length:\s*(\d+)\r\n\r\n", buf)
                    if not m:
                        break
                    frame_len = int(m.group(1))
                    start = m.end()
                    if len(buf) < start + frame_len:
                        break  # ainda não chegou o frame inteiro
                    frame = buf[start:start + frame_len]
                    if frame[:2] == b'\xff\xd8':
                        with _live_lock:
                            _live_last_frame = frame
                            _live_last_ts = time.time()
                    buf = buf[start + frame_len:]
                if len(buf) > 2_000_000:
                    buf = buf[-200_000:]  # proteção contra buffer crescer infinito
        except Exception as e:
            print("live-relay erro: %s" % e, flush=True)
        finally:
            try: curl.kill()
            except Exception: pass
        time.sleep(1)  # backoff antes de reconectar

def _live_acquire_viewer():
    global _live_thread, _live_viewers, _live_stop_flag, _live_idle_since
    with _live_lock:
        _live_viewers += 1
        _live_stop_flag = False
        _live_idle_since = None
        if _live_thread is None or not _live_thread.is_alive():
            _live_thread = threading.Thread(target=_live_relay_loop, daemon=True)
            _live_thread.start()

def _live_release_viewer():
    global _live_viewers, _live_idle_since
    with _live_lock:
        _live_viewers = max(0, _live_viewers - 1)
        if _live_viewers == 0:
            _live_idle_since = time.time()

_sema = threading.Semaphore(3)

LINE_RE = re.compile(r'^(\d{2}:\d{2}:\d{2}):(\S+)\s*\|?(.*)')

def _parse_fields(body):
    fields = {}
    for part in (body or '').split('|'):
        part = part.strip()
        if ':' in part:
            k, _, v = part.partition(':')
            fields[k.strip()] = v.strip()
    return fields

def _spy_path(dt):
    name = "Espiao%s.%s" % (dt.strftime("%d%m%y"), PDV_STATION)
    return pathlib.Path(PDV_BASE) / "Cm" / name

def _buscar_itens_cupom(cupom_num):
    """Retorna (date_str, lista_itens) lendo VIT do spy file."""
    hoje = datetime.date.today()
    for dias in range(7):
        dt = hoje - datetime.timedelta(days=dias)
        path = _spy_path(dt)
        if not path.exists():
            continue
        date_str = dt.strftime('%Y-%m-%d')
        dentro, itens = False, []
        for raw in path.read_text(errors='replace').splitlines():
            raw = raw.strip().rstrip('\r')
            m = LINE_RE.match(raw)
            if not m:
                continue
            ts, event, body = m.group(1), m.group(2), m.group(3)
            fields = _parse_fields(body)
            if event == 'ABRECUPOM' and fields.get('Cod') == str(cupom_num):
                dentro, itens = True, []
            elif event == 'FECHACUPOM' and fields.get('Cod') == str(cupom_num):
                return date_str, itens
            elif dentro and event == 'VIT':
                try:
                    valor = float(fields.get('VlTotal', '0').replace(',', '.'))
                    qty   = float(fields.get('Quant',  '1').replace(',', '.'))
                except Exception:
                    valor, qty = 0.0, 1.0
                itens.append({
                    "timestamp": "%s %s" % (date_str, ts),
                    "time":      ts,
                    "desc":      fields.get('Descricao', ''),
                    "qty":       qty,
                    "value":     valor,
                })
    return None, []


def _buscar_cupom(cupom_num):
    """Procura o cupom nos spy files dos últimos 7 dias.
    Usa timestamps dos itens (VIT) para janela precisa — igual ao _item_timestamps do worker.
    Retorna (date, start_ts, end_ts) ou None."""
    hoje = datetime.date.today()
    for dias in range(7):
        dt = hoje - datetime.timedelta(days=dias)
        path = _spy_path(dt)
        if not path.exists():
            continue
        date_str = dt.strftime('%Y-%m-%d')
        fmt = '%Y-%m-%d %H:%M:%S'
        dentro, item_times = False, []
        for raw in path.read_text(errors='replace').splitlines():
            raw = raw.strip().rstrip('\r')
            m = LINE_RE.match(raw)
            if not m:
                continue
            ts, event, body = m.group(1), m.group(2), m.group(3)
            fields = _parse_fields(body)
            if event == 'ABRECUPOM' and fields.get('Cod') == str(cupom_num):
                dentro, item_times = True, []
            elif event == 'VIT' and dentro:
                item_times.append(ts)
            elif event == 'FECHACUPOM' and fields.get('Cod') == str(cupom_num):
                if not item_times:
                    # sem itens: usar ABRECUPOM→FECHACUPOM como fallback
                    item_times = [ts]
                item_times.sort()
                try:
                    first = datetime.datetime.strptime('%s %s' % (date_str, item_times[0]),  fmt)
                    last  = datetime.datetime.strptime('%s %s' % (date_str, item_times[-1]), fmt)
                except ValueError:
                    return None
                # Sem cap: streaming transmite em tempo real, sem custo de upload
                start = first - datetime.timedelta(seconds=5)
                end   = last  + datetime.timedelta(seconds=25)
                return (date_str, start.strftime(fmt), end.strftime(fmt))
    return None


def _buscar_receipt(cupom_num):
    """Retorna o cupom completo (itens + pagamentos) do spy file."""
    hoje = datetime.date.today()
    for dias in range(7):
        dt = hoje - datetime.timedelta(days=dias)
        path = _spy_path(dt)
        if not path.exists():
            continue
        date_str = dt.strftime('%Y-%m-%d')
        dentro = False
        receipt = None
        for raw in path.read_text(errors='replace').splitlines():
            raw = raw.strip().rstrip('\r')
            m = LINE_RE.match(raw)
            if not m:
                continue
            ts, event, body = m.group(1), m.group(2), m.group(3)
            fields = _parse_fields(body)
            if event == 'ABRECUPOM' and fields.get('Cod') == str(cupom_num):
                receipt = {
                    'numero': cupom_num,
                    'operador': fields.get('Descricao', ''),
                    'data': date_str,
                    'abriu': ts,
                    'fechou': None,
                    'itens': [],
                    'pagamentos': [],
                    'subtotal': 0.0,
                    'total': 0.0,
                }
                dentro = True
            elif dentro:
                if event == 'VIT':
                    try:
                        qty   = float(fields.get('Quant', '1').replace(',', '.'))
                        vunit = float(fields.get('VlUnit', '0').replace(',', '.'))
                        vtot  = float(fields.get('VlTotal', '0').replace(',', '.'))
                    except Exception:
                        qty, vunit, vtot = 1.0, 0.0, 0.0
                    receipt['itens'].append({
                        'time': ts, 'cod': fields.get('Cod', ''),
                        'desc': fields.get('Descricao', ''),
                        'qty': qty, 'unit': fields.get('Und', 'Un'),
                        'vunit': vunit, 'vtotal': vtot,
                    })
                elif event == 'SBT':
                    try: receipt['subtotal'] = float(fields.get('VlTotal','0').replace(',','.'))
                    except: pass
                elif event == 'FIN':
                    try: vfin = float(fields.get('VlTotal','0').replace(',','.'))
                    except: vfin = 0.0
                    receipt['pagamentos'].append({
                        'forma': fields.get('Descricao', ''), 'valor': vfin
                    })
                elif event == 'FECHACUPOM' and fields.get('Cod') == str(cupom_num):
                    receipt['fechou'] = ts
                    try: receipt['total'] = float(fields.get('VlTotal','0').replace(',','.'))
                    except: pass
                    dentro = False
                    return receipt
    return None


def _teclas_path(dt):
    name = "Teclas%s.%s" % (dt.strftime("%d%m%y"), PDV_STATION)
    return pathlib.Path(PDV_BASE) / "Cm" / name


TECLAS_RE = re.compile(r'^(\d{2}:\d{2}:\d{2}[.\d]*)\|K:([^|]+)\|A:([^|]+)\|R:([^|]*)\|')

def _ts_to_secs(ts):
    """HH:MM:SS ou HH:MM:SS.mmm → segundos desde meia-noite."""
    p = ts[:8].split(':')
    return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])

def _parse_ts_float(s):
    """HH:MM:SS.mmm → float (segundos)."""
    p = s.split(':')
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])


def _listar_consultas(dt):
    """Detecta itens consultados/selecionados manualmente no caixa no dia dt."""
    esp_path = _spy_path(dt)
    tec_path = _teclas_path(dt)
    if not esp_path.exists() or not tec_path.exists():
        return []

    date_str = dt.strftime('%Y-%m-%d')

    # ── 1. Parse Espiao: cupons e VITs ──────────────────────────────────────
    cupom_ativo = None
    vits = []
    for raw in esp_path.read_text(errors='replace').splitlines():
        raw = raw.strip().rstrip('\r')
        m = LINE_RE.match(raw)
        if not m:
            continue
        ts, event, body = m.group(1), m.group(2), m.group(3)
        fields = _parse_fields(body)
        if event == 'ABRECUPOM':
            cupom_ativo = {'num': fields.get('Cod',''), 'operador': fields.get('Descricao','')}
        elif event == 'FECHACUPOM':
            num = fields.get('Cod', '')
            if cupom_ativo and cupom_ativo['num'] == num:
                cupom_ativo = None
        elif event == 'VIT' and cupom_ativo:
            try:
                qty   = float(fields.get('Quant',  '1').replace(',', '.'))
                vunit = float(fields.get('VlUnit',  '0').replace(',', '.'))
                vtot  = float(fields.get('VlTotal', '0').replace(',', '.'))
            except Exception:
                qty, vunit, vtot = 1.0, 0.0, 0.0
            vits.append({
                'timestamp': '%s %s' % (date_str, ts),
                'time': ts, 'secs': _ts_to_secs(ts),
                'cupom': cupom_ativo['num'], 'operador': cupom_ativo['operador'],
                'cod': fields.get('Cod', ''), 'desc': fields.get('Descricao', ''),
                'desc2': fields.get('Desc2', ''), 'qty': qty,
                'unit': fields.get('Und', 'Un'), 'vunit': vunit, 'vtotal': vtot,
                'used': False,
            })

    if not vits:
        return []

    # ── 2. Parse Teclas ──────────────────────────────────────────────────────
    teclas = []
    for raw in tec_path.read_text(errors='replace').splitlines():
        raw = raw.strip().rstrip('\r')
        m = TECLAS_RE.match(raw)
        if m:
            teclas.append({
                'ts_f': m.group(1), 'ts_s': m.group(1)[:8],
                'key': m.group(2), 'action': m.group(3), 'r': m.group(4),
            })

    def _find_vit(code, at_secs, max_delta=10):
        """Encontra VIT mais próximo em time e código."""
        best, best_d = None, max_delta + 1
        code_n = code.lstrip('0') or '0'
        for v in vits:
            if v['used']:
                continue
            d = abs(v['secs'] - at_secs)
            if d > max_delta:
                continue
            cn = v['cod'].lstrip('0') or '0'
            d2n = v['desc2'].lstrip('0') if v['desc2'] else ''
            match = (cn == code_n or v['cod'] == code or
                     (d2n and d2n.startswith(code_n)))
            if match and d < best_d:
                best, best_d = v, d
        if best:
            return best
        # fallback: qualquer VIT mais próximo
        best, best_d = None, max_delta + 1
        for v in vits:
            if v['used']:
                continue
            d = abs(v['secs'] - at_secs)
            if d < best_d:
                best, best_d = v, d
        return best

    # ── 3. Detectar consultas ────────────────────────────────────────────────
    consultas, seen = [], set()

    i = 0
    while i < len(teclas):
        ev = teclas[i]

        # Tipo 1: K:MENU + A:VIT
        if ev['key'] == 'MENU' and ev['action'] == 'VIT':
            codigo = ev['r'].strip()
            at = _ts_to_secs(ev['ts_s'])
            vit = _find_vit(codigo, at)
            if vit:
                vit['used'] = True
                key = (vit['cupom'], vit['cod'], vit['time'][:5])
                if key not in seen:
                    seen.add(key)
                    consultas.append({
                        'date': date_str, 'pdv': PDV_STATION,
                        'time': vit['time'], 'timestamp': vit['timestamp'],
                        'cupom': vit['cupom'], 'operador': vit['operador'],
                        'acao': 'VIT', 'acao_label': 'Item selecionado no menu',
                        'codigo_consultado': codigo,
                        'cod': vit['cod'], 'desc': vit['desc'],
                        'qty': vit['qty'], 'unit': vit['unit'],
                        'vunit': vit['vunit'], 'vtotal': vit['vtotal'],
                    })
            i += 1
            continue

        # Tipo 2: sequência A:NUM (dígitos simples) + A:CDP
        if ev['action'] == 'NUM':
            nums = []
            j = i
            while j < len(teclas) and teclas[j]['action'] == 'NUM':
                nums.append(teclas[j])
                j += 1
            if j < len(teclas) and teclas[j]['action'] == 'CDP' and nums:
                # Só aceita se R: são dígitos simples (teclado manual, não scanner)
                is_manual = all(len(e['r']) <= 1 and (e['r'] == '' or e['r'].isdigit())
                                for e in nums)
                if is_manual:
                    code = ''.join(e['r'] for e in nums)
                    duration = 0.0
                    try:
                        duration = _parse_ts_float(nums[-1]['ts_f']) - _parse_ts_float(nums[0]['ts_f'])
                    except Exception:
                        pass
                    if len(code) <= 6 or duration >= 0.7:
                        at = _ts_to_secs(teclas[j]['ts_s'])
                        vit = _find_vit(code, at)
                        if vit:
                            vit['used'] = True
                            key = (vit['cupom'], vit['cod'], vit['time'][:5])
                            if key not in seen:
                                seen.add(key)
                                consultas.append({
                                    'date': date_str, 'pdv': PDV_STATION,
                                    'time': vit['time'], 'timestamp': vit['timestamp'],
                                    'cupom': vit['cupom'], 'operador': vit['operador'],
                                    'acao': 'CDP', 'acao_label': 'Código digitado no caixa',
                                    'codigo_consultado': code,
                                    'cod': vit['cod'], 'desc': vit['desc'],
                                    'qty': vit['qty'], 'unit': vit['unit'],
                                    'vunit': vit['vunit'], 'vtotal': vit['vtotal'],
                                })
                i = j + 1
                continue

        i += 1

    return sorted(consultas, key=lambda x: x['time'])


def _listar_cupons(dt):
    """Retorna lista de todos os cupons fechados do dia dt (do spy file)."""
    path = _spy_path(dt)
    if not path.exists():
        return []
    cupons = []
    by_num = {}
    current = None
    for raw in path.read_text(errors='replace').splitlines():
        raw = raw.strip().rstrip('\r')
        m = LINE_RE.match(raw)
        if not m:
            continue
        ts, event, body = m.group(1), m.group(2), m.group(3)
        fields = _parse_fields(body)
        if event == 'ABRECUPOM':
            current = {
                'numero': fields.get('Cod', ''),
                'operador': fields.get('Descricao', ''),
                'abriu': ts,
                'fechou': None,
                'total': 0.0,
                'itens': 0,
                'item_top': None,   # item de maior valor
                'item_top_valor': 0.0,
            }
            if current['numero']:
                by_num[current['numero']] = current
        elif event == 'VIT' and current:
            current['itens'] += 1
            try:
                vl = float(fields.get('VlTotal', '0').replace(',', '.'))
                if vl > current['item_top_valor']:
                    current['item_top_valor'] = vl
                    current['item_top'] = fields.get('Descricao', '')
            except Exception:
                pass
        elif event == 'FECHACUPOM':
            num = fields.get('Cod', '')
            cup = by_num.get(num, current)
            if cup:
                cup['fechou'] = ts
                try:
                    v = fields.get('VlTotal', '0')
                    if ',' in v and '.' in v:
                        cup['total'] = float(v.replace('.', '').replace(',', '.'))
                    elif ',' in v:
                        cup['total'] = float(v.replace(',', '.'))
                    else:
                        cup['total'] = float(v)
                except Exception:
                    pass
                if cup not in cupons:
                    cupons.append(cup)
                current = None
    return cupons


def _extrair_snapshot_jpeg(ts_str):
    """Extrai 1 frame JPEG do DVR para o timestamp dado. Retorna bytes ou None."""
    try:
        ts_dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    start = (ts_dt - datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    end   = (ts_dt + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    dvr_url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
    ) % (HOST, CHANNEL, urllib.parse.quote(start), urllib.parse.quote(end))
    try:
        curl = subprocess.Popen(
            ["curl", "-s", "--digest", "-u", "%s:%s" % (USER, PASS), dvr_url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        ffmpeg = subprocess.Popen(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ss", "1", "-vframes", "1",
             "-q:v", "3", "-f", "image2", "pipe:1"],
            stdin=curl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        curl.stdout.close()
        jpeg, _ = ffmpeg.communicate(timeout=20)
        curl.wait(timeout=3)
        if ffmpeg.returncode != 0 or not jpeg or jpeg[:2] != b'\xff\xd8':
            return None
        return jpeg
    except Exception as e:
        print("video_streamer: _extrair_snapshot_jpeg erro: %s" % e, flush=True)
        try: curl.kill()
        except: pass
        try: ffmpeg.kill()
        except: pass
        return None


def _compra_muito_recente(end_time):
    """Retorna True se a compra terminou há menos de 3 minutos (segmento DVR ainda aberto)."""
    try:
        fmt = '%Y-%m-%d %H:%M:%S'
        end_dt = datetime.datetime.strptime(end_time, fmt)
        return (datetime.datetime.now() - end_dt).total_seconds() < 180
    except Exception:
        return False


def _ajustar_inicio_dhav(start_time, end_time):
    """Tenta encontrar um start_time que retorne DHAV.
    O DVR grava em segmentos por minuto — recua até o minuto anterior se necessário.
    Retorna o start_time ajustado ou None se não encontrou."""
    fmt = '%Y-%m-%d %H:%M:%S'
    candidates = [start_time]
    try:
        dt = datetime.datetime.strptime(start_time, fmt)
        dt0 = dt.replace(second=0)
        for mins in [0, 1, 2, 3, 4, 5]:
            candidates.append((dt0 - datetime.timedelta(minutes=mins)).strftime(fmt))
    except Exception:
        pass

    seen = []
    for s in candidates:
        if s in seen:
            continue
        seen.append(s)
        if _verificar_dhav(s, end_time):
            if s != start_time:
                print("video_streamer: ajustou inicio %s → %s" % (start_time, s), flush=True)
            return s
    return None


def _verificar_dhav(start_time, end_time):
    """Lê apenas os primeiros 4 bytes do DVR via requests stream. Retorna True/False."""
    import requests
    from requests.auth import HTTPDigestAuth
    dvr_url = (
        "http://%s/cgi-bin/loadfile.cgi"
        "?action=startLoad&channel=%s&startTime=%s&endTime=%s"
    ) % (HOST, CHANNEL,
         urllib.parse.quote(start_time),
         urllib.parse.quote(end_time))
    try:
        r = requests.get(dvr_url, auth=HTTPDigestAuth(USER, PASS),
                         stream=True, timeout=8)
        first = b''
        for chunk in r.iter_content(chunk_size=4):
            first += chunk
            if len(first) >= 4:
                break
        r.close()
        return first[:4] == b'DHAV'
    except Exception:
        return False


def _stream_dvr(start_time, end_time, wfile):
    """Baixa do DVR e faz pipe ffmpeg → wfile. Retorna False se DVR sem gravação."""
    dvr_url = (
        "http://%s/cgi-bin/loadfile.cgi"
        "?action=startLoad&channel=%s&startTime=%s&endTime=%s"
    ) % (HOST, CHANNEL,
         urllib.parse.quote(start_time),
         urllib.parse.quote(end_time))

    curl = subprocess.Popen(
        ["curl", "-s", "--digest", "-u", "%s:%s" % (USER, PASS), dvr_url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    # Verificar se os primeiros bytes são DHAV antes de responder 200
    header = curl.stdout.read(4)
    if header != b'DHAV':
        curl.terminate()
        try: curl.wait(timeout=3)
        except: curl.kill()
        return False

    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y",
         "-i", "pipe:0",
         "-vf", "scale=720:-2",
         "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
         "-preset", "fast", "-crf", "23",
         "-an",
         "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
         "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    # Thread para alimentar ffmpeg: header + resto do curl
    def _feed():
        try:
            ffmpeg.stdin.write(header)
            while True:
                chunk = curl.stdout.read(65536)
                if not chunk:
                    break
                ffmpeg.stdin.write(chunk)
        except Exception:
            pass
        finally:
            try: ffmpeg.stdin.close()
            except: pass
            curl.terminate()
            try: curl.wait(timeout=3)
            except: curl.kill()

    import threading as _thr
    _thr.Thread(target=_feed, daemon=True).start()

    try:
        while True:
            chunk = ffmpeg.stdout.read(65536)
            if not chunk:
                break
            wfile.write(chunk)
    except BrokenPipeError:
        pass
    finally:
        ffmpeg.terminate()
        try: ffmpeg.wait(timeout=3)
        except: ffmpeg.kill()
    return True


class VideoStreamHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print("video_streamer: " + fmt % args, flush=True)

    def _check_token(self, params):
        auth = self.headers.get("Authorization", "")
        bearer = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        req_token = bearer or params.get("token", [""])[0]
        if TOKEN and req_token != TOKEN:
            self.send_error(403, "Token invalido")
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path   = parsed.path.rstrip('/')

        # ── /cupom/NUMBER/receipt → cupom completo com itens e pagamentos
        if path.startswith('/cupom/') and path.endswith('/receipt'):
            if not self._check_token(params): return
            num = path.split('/')[2]
            receipt = _buscar_receipt(num)
            if receipt is None:
                self.send_error(404, "Cupom nao encontrado")
                return
            body = json.dumps(receipt, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /live-stream → MJPEG ao vivo servido a partir do relay persistente
        # GET /live-stream?token=...
        if path == '/live-stream':
            if not self._check_token(params): return
            _live_acquire_viewer()
            last_sent_ts = 0.0
            try:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=myboundary")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    with _live_lock:
                        frame = _live_last_frame
                        frame_ts = _live_last_ts
                    if frame and frame_ts != last_sent_ts:
                        last_sent_ts = frame_ts
                        part = (b"--myboundary\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Content-Length: %d\r\n\r\n" % len(frame)) + frame + b"\r\n"
                        self.wfile.write(part)
                    time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError):
                pass  # cliente fechou o modal — normal
            except Exception as e:
                print("live-stream erro: %s" % e, flush=True)
            finally:
                _live_release_viewer()
            return

        # ── /live-snapshot → frame ATUAL da câmera (fallback sem stream contínuo)
        # GET /live-snapshot?token=...
        if path == '/live-snapshot':
            if not self._check_token(params): return
            try:
                url = "http://%s/cgi-bin/snapshot.cgi?channel=%s" % (HOST, CHANNEL)
                r = subprocess.run(
                    ["curl", "-s", "--digest", "-u", "%s:%s" % (USER, PASS), "--max-time", "8", url],
                    capture_output=True, timeout=10
                )
                jpeg = r.stdout
                if not jpeg or jpeg[:2] != b'\xff\xd8':
                    self.send_error(502, "Câmera indisponível")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            except Exception as e:
                self.send_error(500, str(e))
            return

        # ── /self-restart → encerra o processo para systemd (Restart=always) reiniciar com novo código
        if path == '/self-restart':
            if not self._check_token(params): return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            import threading as _rt
            def _exit():
                import time as _t; _t.sleep(0.5); os._exit(0)
            _rt.Thread(target=_exit, daemon=False).start()
            return

        # ── /gemini-stats → estatísticas de uso e custo do Gemini (hoje)
        if path == '/gemini-stats':
            if not self._check_token(params): return
            body = json.dumps(_gemini_stats(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /gemini-stats-total → crédito restante e gasto acumulado (todos os dias)
        if path == '/gemini-stats-total':
            if not self._check_token(params): return
            body = json.dumps(_gemini_stats_total(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /best-snapshot → extrai janela de vídeo e retorna melhor frame do ROI
        # GET /best-snapshot?ts=...&before=3&after=1&token=...
        if path == '/best-snapshot':
            if not self._check_token(params): return
            ts_str = params.get("ts", [""])[0]
            before = min(int(params.get("before", ["3"])[0]), 6)
            after  = min(int(params.get("after",  ["1"])[0]), 3)
            if not ts_str:
                self.send_error(400, "ts obrigatorio")
                return
            best = _extrair_melhor_frame(ts_str, before, after)
            if best is None:
                self.send_error(404, "Sem frame adequado na janela")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(best)))
            self.end_headers()
            self.wfile.write(best)
            return

        # ── /snapshot → extrai 1 frame JPEG do DVR em determinado timestamp
        # GET /snapshot?ts=YYYY-MM-DD+HH:MM:SS&token=...
        if path == '/snapshot':
            if not self._check_token(params): return
            ts_str = params.get("ts", [""])[0]
            if not ts_str:
                self.send_error(400, "ts obrigatorio (YYYY-MM-DD HH:MM:SS)")
                return
            try:
                ts_dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                self.send_error(400, "ts invalido")
                return
            # Baixar 1s antes do timestamp para garantir segmento DHAV disponível
            # O -ss 1 no ffmpeg pula 1s e extrai o frame no timestamp exato
            start = (ts_dt - datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            end   = (ts_dt + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            dvr_url = (
                "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
            ) % (HOST, CHANNEL, urllib.parse.quote(start), urllib.parse.quote(end))
            jpeg = _extrair_snapshot_jpeg(ts_str)
            if jpeg is None:
                self.send_error(404, "Sem gravacao para este instante")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)
            return

        # ── /clip → gera MP4 completo (arquivo, não stream) — funciona em iOS
        # GET /clip?start=...&end=...&token=...
        # GET /clip/{token} → serve o arquivo gerado
        if path == '/clip' or path.startswith('/clip/'):

            if not self._check_token(params): return

            # Servir arquivo já gerado: /clip/TOKEN
            if path.startswith('/clip/'):
                clip_tok = path[6:]
                _clip_cleanup()
                with _clip_lock:
                    entry = _clip_cache.get(clip_tok)
                if not entry:
                    self.send_error(404, "Clip expirado ou nao encontrado")
                    return
                fpath, _ = entry
                try:
                    data = open(fpath, 'rb').read()
                except Exception:
                    self.send_error(500, "Erro ao ler clip")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return

            # Gerar novo clip: /clip?start=...&end=...
            start_c = params.get("start", [""])[0]
            end_c   = params.get("end",   [""])[0]
            if not start_c or not end_c:
                self.send_error(400, "start e end obrigatorios")
                return

            # Token baseado nos parâmetros
            tok = hashlib.md5(("%s|%s" % (start_c, end_c)).encode()).hexdigest()[:16]
            _clip_cleanup()
            with _clip_lock:
                if tok in _clip_cache:
                    body = json.dumps({"token": tok}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                    return

            # Baixar DVR e gerar MP4 completo via ffmpeg
            dvr_url = (
                "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
            ) % (HOST, CHANNEL, urllib.parse.quote(start_c), urllib.parse.quote(end_c))
            try:
                tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
                tmp_path = tmp.name
                tmp.close()

                # Calcular duração real (start→end), máx 300s
                try:
                    fmt = '%Y-%m-%d %H:%M:%S'
                    dt_s = datetime.datetime.strptime(start_c, fmt)
                    dt_e = datetime.datetime.strptime(end_c,   fmt)
                    dur  = max(10, min(1800, int((dt_e - dt_s).total_seconds()) + 5))
                except Exception:
                    dur = 120

                # curl DVR → ffmpeg → arquivo mp4
                curl = subprocess.Popen(
                    ["curl", "-s", "--digest", "-u", "%s:%s" % (USER, PASS), dvr_url],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                ffmpeg = subprocess.Popen(
                    ["ffmpeg", "-y", "-i", "pipe:0",
                     "-t", str(dur),
                     "-vf", "scale=480:-2",
                     "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
                     "-preset", "fast", "-crf", "26",
                     "-an",
                     "-movflags", "+faststart",
                     tmp_path],
                    stdin=curl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                curl.stdout.close()
                ffmpeg.wait(timeout=dur + 120)
                curl.wait(timeout=5)

                if ffmpeg.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                    try: os.unlink(tmp_path)
                    except: pass
                    self.send_error(500, "Falha ao gerar clip")
                    return

                with _clip_lock:
                    _clip_cache[tok] = (tmp_path, time.time() + CLIP_TTL)

                body = json.dumps({"token": tok}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                print("video_streamer: erro clip: %s" % e, flush=True)
                self.send_error(500, "Erro interno")
            return

        # ── /consultas → itens consultados/selecionados manualmente no caixa
        if path == '/consultas':
            if not self._check_token(params): return
            date_str = params.get("date", [datetime.date.today().strftime('%Y-%m-%d')])[0]
            try:
                dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.send_error(400, "Data invalida")
                return
            consultas = _listar_consultas(dt)
            body = json.dumps({"date": date_str, "consultas": consultas},
                               ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /cupons → lista todos os cupons do dia (do spy file)
        # ── /vlm-stats → estatísticas da IA SmolVLM (lê arquivo compartilhado)
        if path == '/audit-evidence':
            if not self._check_token(params): return
            rel = params.get("file", [""])[0]
            root = pathlib.Path("/var/lib/pdv-visual-auditor/evidence").resolve()
            target = (root / rel).resolve()
            if not str(target).startswith(str(root)) or not target.exists() or target.suffix.lower() not in (".jpg", ".jpeg"):
                self.send_error(404, "Evidencia nao encontrada")
                return
            try:
                data = target.read_bytes()
            except Exception:
                self.send_error(500, "Erro ao ler evidencia")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == '/audit-decisions':
            if not self._check_token(params): return
            date_str = params.get("date", [datetime.date.today().strftime('%Y-%m-%d')])[0]
            result_filter = params.get("result", [""])[0].upper()
            try:
                limit = max(1, min(1000, int(params.get("limit", ["300"])[0])))
            except Exception:
                limit = 300
            log_file = pathlib.Path("/var/lib/pdv-visual-auditor/audit_decisions_%s.jsonl" % date_str)
            rows = []
            counts = {}
            if log_file.exists():
                try:
                    for raw in log_file.read_text(errors='replace').splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        res = str(rec.get("resultado", "")).upper()
                        counts[res] = counts.get(res, 0) + 1
                        if result_filter and res != result_filter:
                            continue
                        rows.append(rec)
                    rows = rows[-limit:]
                    rows.reverse()
                except Exception:
                    rows = []
            body = json.dumps({
                "date": date_str,
                "result": result_filter or "ALL",
                "limit": limit,
                "counts": counts,
                "items": rows,
            }, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/vlm-stats':
            if not self._check_token(params): return
            date_str = params.get("date", [datetime.date.today().strftime('%Y-%m-%d')])[0]
            stats_file = "/tmp/vlm_stats_%s.json" % date_str
            try:
                import pathlib as _pl
                s = json.loads(_pl.Path(stats_file).read_text()) if _pl.Path(stats_file).exists() else {}
                total = s.get("ok", 0) + s.get("suspeito", 0) + s.get("inconclusivo", 0)
                pulados = s.get("pulados", 0)
                sem_dvr = s.get("sem_dvr", 0)
                descartado = s.get("descartado", 0)
                processados = total + sem_dvr + descartado + pulados
                fila_interna = s.get("fila", 0)
                fila_conta = fila_interna
                cupom_summary = {}
                medida = {}
                try:
                    marker_file = pathlib.Path("/var/lib/pdv-visual-auditor/measurement_marker.json")
                    legacy_file = pathlib.Path("/var/lib/pdv-visual-auditor/auditoria_medicao_atual.json")
                    if marker_file.exists():
                        medida = json.loads(marker_file.read_text())
                    elif legacy_file.exists():
                        medida = json.loads(legacy_file.read_text())
                    dt_stats = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                    spy = _spy_path(dt_stats)
                    total_itens = 0
                    if spy.exists():
                        for raw in spy.read_text(errors='replace').splitlines():
                            raw = raw.strip()
                            m = LINE_RE.match(raw)
                            if m and m.group(2) == 'VIT':
                                total_itens += 1
                    baseline = int(medida.get("baseline_total_itens") or 0) if medida.get("date") == date_str else 0
                    itens_novos = max(0, total_itens - baseline)
                    fila_conta = max(0, itens_novos - processados)
                    audit_mode = "cupom" if "cupom" in str(medida.get("note", "")).lower() else "item"
                    if audit_mode == "cupom":
                        fila_conta = fila_interna
                        rows = list((s.get("cupoms") or {}).values())
                        enfileirados = len(rows)
                        auditados = sum(1 for r in rows if int(r.get("total") or 0) > 0 and int(r.get("done") or 0) >= int(r.get("total") or 0))
                        em_analise_cupom = sum(1 for r in rows if 0 < int(r.get("done") or 0) < int(r.get("total") or 0))
                        aprovados_cupom = sum(1 for r in rows if r.get("status") == "OK")
                        suspeitos_cupom = sum(1 for r in rows if r.get("status") == "SUSPEITO")
                        inconclusivos_cupom = sum(1 for r in rows if r.get("status") == "INCONCLUSIVO")
                        incompletos_cupom = sum(1 for r in rows if r.get("status") == "INCOMPLETO")
                        fila_cupom = sum(1 for r in rows if int(r.get("done") or 0) == 0)
                        fila_conta = fila_cupom
                        cupom_summary = {
                            "cupoms_enfileirados": enfileirados,
                            "cupoms_auditados": auditados,
                            "cupoms_em_analise": em_analise_cupom,
                            "cupoms_aprovados": aprovados_cupom,
                            "cupoms_suspeitos": suspeitos_cupom,
                            "cupoms_inconclusivos": inconclusivos_cupom,
                            "cupoms_incompletos": incompletos_cupom,
                            "cupoms_fila": fila_cupom,
                        }
                        fila_interna = fila_cupom
                    medida.update({
                        "date": date_str,
                        "baseline_total_itens": baseline,
                        "total_itens": total_itens,
                        "itens_novos": itens_novos,
                        "audit_mode": audit_mode,
                        "pendencia_pela_conta": fila_conta,
                    })
                except Exception:
                    medida = {}
                total_ms = s.get("total_ms", 0)
                tempos = s.get("tempos", [])
                media_ms = round(total_ms / total) if total > 0 else 0
                # Histórico permanente
                _hf = pathlib.Path("/opt/pdv-visual-auditor/historico_ia.json")
                h = json.loads(_hf.read_text()) if _hf.exists() else {}
                stats = {
                    "date": date_str,
                    "aprovados": s.get("ok", 0),
                    "suspeitos": s.get("suspeito", 0),
                    "inconclusivos": s.get("inconclusivo", 0),
                    "sem_dvr": sem_dvr,
                    "descartado": descartado,
                    "pulados": pulados,
                    "total": total,
                    "ia_total": total,
                    "processados": processados,
                    "taxa_aprovacao": round(s.get("ok", 0) / total * 100, 1) if total > 0 else 0,
                    "media_s": round(media_ms / 1000, 1) if media_ms else 0,
                    "min_s": round(min(tempos) / 1000, 1) if tempos else 0,
                    "max_s": round(max(tempos) / 1000, 1) if tempos else 0,
                    "ultimo_s": round(tempos[-1] / 1000, 1) if tempos else 0,
                    "tempos_recentes": [round(t / 1000, 1) for t in tempos[-10:]],
                    "fila": fila_conta,
                    "fila_conta": fila_conta,
                    "fila_interna": fila_interna,
                    "medicao": medida,
                    "historico_total": h.get("total", 0),
                    "historico_ok": h.get("ok", 0),
                    "historico_suspeito": h.get("suspeito", 0),
                    "historico_inconclusivo": h.get("inconclusivo", 0),
                    **cupom_summary,
                }
            except Exception as e:
                stats = {"date": date_str, "aprovados": 0, "suspeitos": 0, "inconclusivos": 0, "total": 0, "taxa_aprovacao": 0, "media_s": 0, "min_s": 0, "max_s": 0, "ultimo_s": 0, "tempos_recentes": []}
            body = json.dumps(stats, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /stats → total de itens passados no caixa no dia
        if path == '/stats':
            if not self._check_token(params): return
            date_str = params.get("date", [datetime.date.today().strftime('%Y-%m-%d')])[0]
            try:
                dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.send_error(400, "Data invalida")
                return
            # Conta todos os VIT (bips) do dia, incluindo cupons abertos/cancelados
            spy = _spy_path(dt)
            total_itens = 0
            total_cupons = 0
            if spy.exists():
                for raw in spy.read_text(errors='replace').splitlines():
                    raw = raw.strip()
                    m = LINE_RE.match(raw)
                    if not m:
                        continue
                    ev = m.group(2)
                    if ev == 'VIT':
                        total_itens += 1
                    elif ev == 'FECHACUPOM':
                        total_cupons += 1
            stats = {
                "date": date_str,
                "total_itens": total_itens,
                "total_cupons": total_cupons,
            }
            body = json.dumps(stats, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/cupons':
            if not self._check_token(params): return
            date_str = params.get("date", [datetime.date.today().strftime('%Y-%m-%d')])[0]
            try:
                dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.send_error(400, "Data invalida")
                return
            cupons = _listar_cupons(dt)
            body = json.dumps({"date": date_str, "cupons": cupons},
                               ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /probe → verifica janela e retorna start_time real (após ajuste DHAV)
        if path == '/probe':
            if not self._check_token(params): return
            start_time = params.get("start", [""])[0]
            end_time   = params.get("end",   [""])[0]
            if not start_time or not end_time:
                self.send_error(400, "start e end obrigatorios")
                return
            real_start = _ajustar_inicio_dhav(start_time, end_time)
            if real_start is None:
                self.send_error(404, "DVR sem gravacao para este periodo")
                return
            body = json.dumps({"start_time": real_start, "end_time": end_time,
                                "adjusted": real_start != start_time}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /cupom/NUMBER/items → itens do spy file
        if path.startswith('/cupom/') and path.endswith('/items'):
            if not self._check_token(params): return
            num = path.split('/')[2]
            date_str, itens = _buscar_itens_cupom(num)
            if date_str is None:
                self.send_error(404, "Cupom nao encontrado")
                return
            body = json.dumps({"cupom": num, "date": date_str, "itens": itens},
                               ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /cupom/NUMBER/info → JSON com start/end REAL (após ajuste DHAV)
        if path.startswith('/cupom/') and path.endswith('/info'):
            if not self._check_token(params): return
            num = path.split('/')[2]
            result = _buscar_cupom(num)
            if not result:
                self.send_error(404, "Cupom nao encontrado nos ultimos 7 dias")
                return
            date_str, start, end = result
            if _compra_muito_recente(end):
                self.send_error(425, "Gravacao ainda sendo escrita pelo DVR aguarde 2-3 min")
                return
            # Ajustar start para o início real do segmento DHAV
            real_start = _ajustar_inicio_dhav(start, end)
            if real_start is None:
                self.send_error(404, "DVR sem gravacao para este periodo")
                return
            body = json.dumps({"cupom": num, "date": date_str,
                                "start_time": real_start, "end_time": end,
                                "adjusted": real_start != start}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /cupom/NUMBER → stream direto pelo spy file
        if path.startswith('/cupom/'):
            if not self._check_token(params): return
            num = path.split('/')[2]
            result = _buscar_cupom(num)
            if not result:
                self.send_error(404, "Cupom nao encontrado nos ultimos 7 dias")
                return
            _, start, end = result
            if not _sema.acquire(blocking=False):
                self.send_error(503, "Servidor ocupado")
                return
            try:
                if _compra_muito_recente(end):
                    self.send_error(425, "Gravacao ainda sendo escrita pelo DVR aguarde 2-3 min")
                    return
                # Enviar 200 OK IMEDIATAMENTE — browser não faz timeout esperando headers
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Accept-Ranges", "none")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                # DHAV check + stream acontecem DEPOIS dos headers (browser já recebeu 200)
                start = _ajustar_inicio_dhav(start, end)
                if start:
                    _stream_dvr(start, end, self.wfile)
            except Exception as e:
                print("video_streamer: erro cupom %s: %s" % (num, e), flush=True)
            finally:
                _sema.release()
            return

        # ── / → stream de intervalo explícito (?start=...&end=...)
        start_time = params.get("start", [""])[0]
        end_time   = params.get("end",   [""])[0]
        if not start_time or not end_time:
            self.send_error(400, "start e end obrigatorios")
            return
        if not self._check_token(params): return
        if not _sema.acquire(blocking=False):
            self.send_error(503, "Servidor ocupado")
            return
        skip_dhav = params.get("skip_dhav", [""])[0] == "1"
        try:
            if not skip_dhav and _compra_muito_recente(end_time):
                _sema.release()
                self.send_error(425, "Gravacao ainda sendo escrita pelo DVR aguarde 2-3 min")
                return
            # Enviar 200 OK imediatamente — evita timeout do browser
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # skip_dhav=1: /info ou /probe já verificaram DHAV, não precisa verificar de novo
            if skip_dhav:
                _stream_dvr(start_time, end_time, self.wfile)
            else:
                start_time = _ajustar_inicio_dhav(start_time, end_time)
                if start_time:
                    _stream_dvr(start_time, end_time, self.wfile)
        except Exception as e:
            print("video_streamer: erro stream: %s" % e, flush=True)
        finally:
            if _sema._value == 0:
                _sema.release()

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path   = parsed.path.rstrip('/')

        # ── POST /gemini-analyze?cupom=XXXXX&token=... → analisa cupom com Gemini Vision
        if path == '/gemini-analyze':
            if not self._check_token(params): return
            cupom_num = params.get("cupom", [""])[0]
            if not cupom_num:
                self.send_error(400, "cupom obrigatorio")
                return
            try:
                result = _gemini_analyze_cupom(cupom_num)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return

        self.send_error(404, "Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


def _postar_evento_fisico(cupom_num, ts, produto, valor, qty,
                           resultado_str, motivo, confianca, img_url,
                           api_url, api_token, pdv_station):
    """Posta evento na API de auditoria."""
    import urllib.request as _ur
    if not api_url or not api_token:
        return
    api_resultado = resultado_str
    if api_resultado not in ("CONFERE","INCONCLUSIVO","DIVERGENCIA_CATEGORIA","NAO_ANALISADO","CONFERE_POR_REGRA_DE_VALOR","NAO_CONFERE"):
        api_resultado = "INCONCLUSIVO"
    acao = "revisar" if api_resultado == "DIVERGENCIA_CATEGORIA" else "liberar"
    payload = json.dumps({
        "timestamp": ts, "pdv": pdv_station, "cupom": str(cupom_num),
        "produto": produto, "valor_unitario": round(valor, 2), "quantidade": qty,
        "modo": "gemini",
        "resultado": {
            "resultado": api_resultado, "confianca": confianca,
            "comparacao_pdv": motivo[:300],
            "possivel_divergencia": motivo if api_resultado == "DIVERGENCIA_CATEGORIA" else "",
            "acao_recomendada": acao,
        },
        "imagem": img_url,
    }).encode()
    try:
        req = _ur.Request("%s/api/v1/events" % api_url, data=payload,
                          headers={"Authorization": "Bearer %s" % api_token,
                                   "Content-Type": "application/json"}, method="POST")
        _ur.urlopen(req, timeout=10)
    except Exception as e:
        print("postar_evento erro: %s" % e, flush=True)


def _melhor_frame_janela(ts_str, janela_s=3, before=1, after=4):
    """Extrai frames na janela [-before, +after] em torno do VIT e retorna (jpeg, ts_melhor, metricas).

    Prioriza frames NO MOMENTO OU APÓS o VIT: o registro do item acontece quando/depois
    do scan físico, então o produto tende a ainda estar visível logo após — enquanto frames
    muito antes do VIT podem mostrar o item anterior já retirado ou a operadora sem nada
    no scanner ainda. A pontuação de ocupação por pixel escuro também é enganada por
    cabelo/corpo da operadora no ROI, então aplicamos um bônus de recência para compensar.
    """
    try:
        ts_dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None, ts_str, None

    melhor = (-999.0, None, ts_str, None)
    # ROI centralizado na área do scanner (centro da imagem, abaixo do teclado)
    ROI_X, ROI_Y, ROI_W, ROI_H = 0.20, 0.28, 0.55, 0.55

    for delta in range(-before, after + 1):
        ts_c = (ts_dt + datetime.timedelta(seconds=delta)).strftime("%Y-%m-%d %H:%M:%S")
        jpeg = _extrair_snapshot_jpeg(ts_c)
        if not jpeg:
            continue
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-y", "-i", "pipe:0",
                 "-vf", "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=120:120,format=gray" % (ROI_W, ROI_H, ROI_X, ROI_Y),
                 "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw, _ = proc.communicate(input=jpeg, timeout=8)
            if not raw or len(raw) < 200:
                continue
            px    = list(raw)
            n     = len(px)
            mean  = sum(px) / n
            var   = sum((p - mean) ** 2 for p in px) / n
            sp    = sorted(px)
            bg    = sp[int(n * 0.90)]
            occ   = sum(1 for p in px if p < max(bg - 25, 60)) / n
            # Bônus de recência: frames em/após o VIT (delta>=0) são fisicamente mais confiáveis;
            # frames bem antes do VIT (delta muito negativo) são penalizados.
            recencia = 0.20 if delta >= 0 else -0.05 * abs(delta)
            score = occ * 0.55 + min(var / 3000.0, 0.30) + recencia
            if score > melhor[0]:
                melhor = (score, jpeg, ts_c, {"occupancy": occ, "sharpness": var, "mean": mean, "delta": delta})
        except Exception:
            pass

    score_melhor, jpeg, ts_best, metricas = melhor
    # Score mínimo: frame muito vazio/escuro não vale mandar ao Gemini
    if score_melhor < 0.28:
        return None, ts_str, metricas
    return jpeg, ts_best, metricas


# ── Gemini Vision Analysis ────────────────────────────────────────────────────

_GEMINI_USAGE_DIR = "/tmp"
_GEMINI_MODEL     = "gemini-2.5-flash"
_gemini_hora_calls = []  # timestamps de cupons analisados na última hora (controle de limite)
# Preços gemini-2.5-flash (USD por 1M tokens)
_GEMINI_PRICE_IN  = 0.075   # input
_GEMINI_PRICE_OUT = 0.30    # output
_USD_TO_BRL       = 5.70


def _gemini_usage_file():
    return os.path.join(_GEMINI_USAGE_DIR, "gemini_usage_%s.json" % datetime.date.today().isoformat())


def _gemini_track_usage(input_tokens, output_tokens, cupom, n_itens):
    """Acumula tokens/custo no arquivo diário."""
    fpath = _gemini_usage_file()
    try:
        data = json.load(open(fpath)) if os.path.exists(fpath) else {
            "date": datetime.date.today().isoformat(),
            "calls": 0, "itens": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "cost_brl": 0.0,
            "cupons": []
        }
    except Exception:
        data = {"date": datetime.date.today().isoformat(),
                "calls": 0, "itens": 0,
                "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "cost_brl": 0.0, "cupons": []}

    data["calls"]        += 1
    data["itens"]        += n_itens
    data["input_tokens"] += input_tokens
    data["output_tokens"] += output_tokens
    cost_usd              = (input_tokens * _GEMINI_PRICE_IN + output_tokens * _GEMINI_PRICE_OUT) / 1_000_000
    data["cost_usd"]     += cost_usd
    data["cost_brl"]     += cost_usd * _USD_TO_BRL
    if str(cupom) not in data["cupons"]:
        data["cupons"].append(str(cupom))

    try:
        json.dump(data, open(fpath, "w"))
    except Exception as e:
        print("gemini_track erro: %s" % e, flush=True)


_GEMINI_CREDITO_INICIAL_BRL = float(os.environ.get("GEMINI_CREDITO_INICIAL_BRL", "60.0"))


def _gemini_stats_total():
    """Soma o uso de todos os dias (todos os arquivos gemini_usage_*.json) — gasto acumulado real."""
    import glob as _glob
    total_calls = total_itens = 0
    total_cost_usd = total_cost_brl = 0.0
    dias = []
    for fpath in sorted(_glob.glob(os.path.join(_GEMINI_USAGE_DIR, "gemini_usage_*.json"))):
        try:
            d = json.load(open(fpath))
            total_calls    += d.get("calls", 0)
            total_itens    += d.get("itens", 0)
            total_cost_usd += d.get("cost_usd", 0.0)
            total_cost_brl += d.get("cost_brl", 0.0)
            dias.append({"date": d.get("date"), "calls": d.get("calls", 0), "cost_brl": round(d.get("cost_brl", 0.0), 4)})
        except Exception:
            continue
    credito_restante = max(_GEMINI_CREDITO_INICIAL_BRL - total_cost_brl, 0.0)
    return {
        "credito_inicial_brl": _GEMINI_CREDITO_INICIAL_BRL,
        "gasto_total_brl": round(total_cost_brl, 4),
        "gasto_total_usd": round(total_cost_usd, 6),
        "credito_restante_brl": round(credito_restante, 4),
        "fotos_analisadas": total_calls,
        "itens_analisados": total_itens,
        "dias": dias,
    }


def _gemini_stats():
    """Retorna estatísticas de uso do dia atual."""
    fpath = _gemini_usage_file()
    if not os.path.exists(fpath):
        return {"date": datetime.date.today().isoformat(), "calls": 0, "itens": 0,
                "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_brl": 0.0, "cupons": []}
    try:
        return json.load(open(fpath))
    except Exception:
        return {}


def _gemini_call_vision(img_bytes, produto, valor):
    """Chama Gemini 2.5 Flash com imagem ROI + produto. Retorna (texto, suspeito, in_tok, out_tok, ms)."""
    import base64, urllib.request as _ur
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY nao configurada")

    b64 = base64.b64encode(img_bytes).decode()
    prompt = (
        "Câmera de teto de supermercado mostrando o caixa. "
        "IMPORTANTE: Responda apenas com base no que está REALMENTE visível. "
        "Não invente produto — se não houver nada identificável, diga honestamente.\n\n"
        "PASSO 1: Identifique o produto que está sendo escaneado pelo operador. "
        "O produto pode estar na mão do caixa diretamente acima/sobre o scanner, ou deslizando pela esteira. "
        "NÃO ignore objetos nas mãos do operador se estiverem sendo claramente manipulados/escaneados. "
        "Ignore APENAS produtos parados em expositores ao fundo ou sacolas plásticas vazias. "
        "Há produto identificável sendo passado no scanner? (sim/não)\n\n"
        "PASSO 2: Se SIM, descreva o produto que o operador está segurando ou acabou de escanear: "
        "tipo de produto, embalagem, cor, marca ou texto visível na embalagem. "
        "Foque no objeto que está sendo ativamente manipulado, não no que está ao fundo. "
        "Se NÃO, diga apenas isso.\n\n"
        "PASSO 3: O produto registrado no sistema é: '%s' (R$ %.2f). "
        "Compare com o que você identificou no Passo 2:\n"
        "   - OK: o produto visível é fisicamente compatível com o registrado.\n"
        "   - SUSPEITO: o produto visível é claramente um produto diferente do registrado.\n"
        "   - INCONCLUSIVO: produto não identificável, imagem fora de foco, ou área obstruída.\n"
        "Nunca responda OK se não viu nenhum produto no Passo 1.\n"
        "Responda em no máximo 3 linhas curtas."
    ) % (produto, valor)

    payload = json.dumps({
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            {"text": prompt}
        ]}],
        "generationConfig": {
            "maxOutputTokens": 300,
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }).encode()

    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (
        _GEMINI_MODEL, GEMINI_KEY)
    req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")

    t0 = time.time()
    resp = json.loads(_ur.urlopen(req, timeout=30).read())
    ms = int((time.time() - t0) * 1000)

    texto      = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    usage      = resp.get("usageMetadata", {})
    in_tok     = usage.get("promptTokenCount", 0)
    out_tok    = usage.get("candidatesTokenCount", 0)
    suspeito   = "SUSPEITO" in texto.upper() and "INCONCLUSIVO" not in texto.upper()
    inconcl    = "INCONCLUSIVO" in texto.upper()
    return texto, suspeito, inconcl, in_tok, out_tok, ms


def _gemini_analyze_cupom(cupom_num):
    """Analisa itens do cupom com Gemini Vision, respeitando config remota."""
    API_URL       = os.environ.get("AUDITORIA_API_URL", "http://201.182.184.80:8098")
    API_TOKEN_PDV = os.environ.get("AUDITORIA_API_TOKEN", "")
    PDV_STATION   = os.environ.get("PDV_STATION", "001")

    # Carregar config remota (valor_minimo, max itens por cupom)
    try:
        import pdv_config_loader as _cfg
        cfg = _cfg.fetch()
    except Exception:
        cfg = {}
    valor_minimo = float(cfg.get("auditoria_valor_minimo") or 0)
    max_itens    = int(cfg.get("auditoria_fotos_por_cupom") or 0)  # 0 = sem limite
    max_por_hora = int(cfg.get("auditoria_max_por_hora") or 0)     # 0 = sem limite

    # Verificar limite de análises por hora
    if max_por_hora > 0:
        agora_h = time.time()
        _gemini_hora_calls[:] = [t for t in _gemini_hora_calls if agora_h - t < 3600]
        if len(_gemini_hora_calls) >= max_por_hora:
            print("gemini: limite %d/hora atingido (%d feitos) — pulando cupom %s" % (
                max_por_hora, len(_gemini_hora_calls), cupom_num), flush=True)
            return {"cupom": cupom_num, "itens_analisados": 0,
                    "ok": 0, "alertas": 0, "inconclusivos": 0,
                    "resultados": [], "custo_brl": 0.0, "tempo_ms": 0,
                    "pulado": True, "motivo": "limite_hora"}

    _, itens = _buscar_itens_cupom(cupom_num)
    if not itens:
        return {"cupom": cupom_num, "itens_analisados": 0,
                "ok": 0, "alertas": 0, "inconclusivos": 0,
                "resultados": [], "custo_brl": 0.0, "tempo_ms": 0}

    # Filtrar por valor mínimo
    if valor_minimo > 0:
        itens = [it for it in itens if float(it.get("value", 0)) >= valor_minimo]

    # Ordenar do mais caro para o mais barato e limitar quantidade
    itens = sorted(itens, key=lambda x: float(x.get("value", 0)), reverse=True)
    if max_itens > 0:
        itens = itens[:max_itens]

    if not itens:
        return {"cupom": cupom_num, "itens_analisados": 0,
                "ok": 0, "alertas": 0, "inconclusivos": 0,
                "resultados": [], "custo_brl": 0.0, "tempo_ms": 0}

    resultados  = []
    t0_total    = time.time()
    total_in    = total_out = 0

    for it in itens:
        ts      = it["timestamp"]
        produto = it["desc"]
        valor   = float(it.get("value", 0))
        qty     = float(it.get("qty", 1))

        # Melhor frame ±3s
        jpeg, ts_best, _ = _melhor_frame_janela(ts, janela_s=3)

        if jpeg is None:
            resultado_str = "INCONCLUSIVO"
            analise       = "Sem footage DVR"
            ms_item = 0
        else:
            try:
                analise, sus, inc, in_tok, out_tok, ms_item = _gemini_call_vision(jpeg, produto, valor)
                total_in  += in_tok
                total_out += out_tok
                if sus:      resultado_str = "DIVERGENCIA_CATEGORIA"
                elif inc:    resultado_str = "INCONCLUSIVO"
                else:        resultado_str = "CONFERE"
            except Exception as e:
                resultado_str = "INCONCLUSIVO"
                analise       = "Erro Gemini: %s" % e
                ms_item       = 0

        # Postar na API
        img_url = "/streamer/snapshot?ts=%s&token=%s" % (urllib.parse.quote(ts_best), TOKEN)
        _postar_evento_fisico(cupom_num, ts_best, produto, valor, qty,
                              resultado_str, analise, 85 if resultado_str != "INCONCLUSIVO" else 30,
                              img_url, API_URL, API_TOKEN_PDV, PDV_STATION)

        resultados.append({
            "produto": produto, "resultado": resultado_str,
            "analise": analise[:100], "ms": ms_item
        })
        print("gemini: %-28s %s %dms — %s" % (
            produto[:26], resultado_str, ms_item, analise[:50]), flush=True)

    total_ms  = int((time.time() - t0_total) * 1000)
    custo_usd = (total_in * _GEMINI_PRICE_IN + total_out * _GEMINI_PRICE_OUT) / 1_000_000
    custo_brl = custo_usd * _USD_TO_BRL

    _gemini_track_usage(total_in, total_out, cupom_num, len(itens))
    _gemini_hora_calls.append(time.time())

    ok  = sum(1 for r in resultados if r["resultado"] == "CONFERE")
    sus = sum(1 for r in resultados if r["resultado"] == "DIVERGENCIA_CATEGORIA")
    inc = sum(1 for r in resultados if r["resultado"] == "INCONCLUSIVO")

    print("gemini: cupom %s done — %d itens %dms | in=%d out=%d | R$%.4f" % (
        cupom_num, len(itens), total_ms, total_in, total_out, custo_brl), flush=True)

    return {
        "cupom": cupom_num, "itens_analisados": len(itens),
        "ok": ok, "alertas": sus, "inconclusivos": inc,
        "resultados": resultados,
        "tokens_input": total_in, "tokens_output": total_out,
        "custo_usd": round(custo_usd, 6), "custo_brl": round(custo_brl, 4),
        "tempo_ms": total_ms
    }


def _extrair_melhor_frame(ts_str, before=3, after=1):
    """Baixa janela de vídeo do DVR, extrai frames a 5fps e retorna o ROI com melhor objeto."""
    import tempfile, shutil, glob as _glob
    ROI_X, ROI_Y, ROI_W, ROI_H = 0.48, 0.03, 0.31, 0.65

    try:
        ts_dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

    start = (ts_dt - datetime.timedelta(seconds=before)).strftime("%Y-%m-%d %H:%M:%S")
    end   = (ts_dt + datetime.timedelta(seconds=after + 1)).strftime("%Y-%m-%d %H:%M:%S")

    dvr_url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
    ) % (HOST, CHANNEL, urllib.parse.quote(start), urllib.parse.quote(end))

    tmpdir = tempfile.mkdtemp(prefix="bsf_")
    try:
        curl = subprocess.Popen(
            ["curl", "-s", "--digest", "-u", "%s:%s" % (USER, PASS), dvr_url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # Extrai 5fps com ROI já recortado — 4s × 5fps = ~20 frames
        vf = "fps=5,crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=640:-2" % (ROI_W, ROI_H, ROI_X, ROI_Y)
        pattern = os.path.join(tmpdir, "f%04d.jpg")
        ffmpeg = subprocess.Popen(
            ["ffmpeg", "-y", "-i", "pipe:0", "-vf", vf, "-q:v", "2", pattern],
            stdin=curl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        curl.stdout.close()
        try:
            ffmpeg.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            ffmpeg.kill()
        try:
            curl.wait(timeout=3)
        except Exception:
            curl.kill()

        frames = sorted(_glob.glob(os.path.join(tmpdir, "f*.jpg")))
        if not frames:
            return None

        best_score = -1.0
        best_jpeg  = None

        for fpath in frames:
            try:
                jpeg = open(fpath, "rb").read()
                if len(jpeg) < 2000:
                    continue
                # Converter para gray 120px para análise rápida
                proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-i", "pipe:0",
                     "-vf", "scale=120:-2,format=gray",
                     "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                raw, _ = proc.communicate(input=jpeg, timeout=5)
                if not raw or len(raw) < 100:
                    continue
                px   = list(raw)
                n    = len(px)
                mean = sum(px) / n
                var  = sum((p - mean) ** 2 for p in px) / n
                # Ocupação dinâmica (percentil 90 = fundo)
                sp        = sorted(px)
                bg_ref    = sp[int(n * 0.90)]
                occ_thr   = max(bg_ref - 25, 60)
                occ       = sum(1 for p in px if p < occ_thr) / n
                # Score: prioriza ocupação (objeto presente) e nitidez (objeto definido)
                score = occ * 0.55 + min(var / 4000.0, 0.45)
                if score > best_score:
                    best_score = score
                    best_jpeg  = jpeg
            except Exception:
                pass

        print("best-snapshot: %s janela=%ds+%ds frames=%d melhor=%.3f" % (
            ts_str, before, after, len(frames), best_score), flush=True)
        return best_jpeg

    except Exception as e:
        print("_extrair_melhor_frame erro: %s" % e, flush=True)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    print("video_streamer: iniciado na porta %d (threaded)" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), VideoStreamHandler).serve_forever()
