#!/usr/bin/env python3.8
"""
Servidor de streaming de video do DVR iMHDX.
- GET /?start=...&end=...&token=...   → stream fMP4 de intervalo explícito
- GET /cupom/NUMBER?token=...          → busca cupom no spy file e stream
- GET /cupom/NUMBER/info?token=...     → retorna JSON com start/end do cupom
Porta: 8765
"""
import os, re, json, datetime, pathlib, subprocess, threading, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HOST      = os.environ.get("IMHDX_HOST",    "")
USER      = os.environ.get("IMHDX_USER",    "")
PASS      = os.environ.get("IMHDX_PASS",    "")
CHANNEL   = os.environ.get("IMHDX_CHANNEL", "1")
TOKEN     = os.environ.get("AUDITORIA_API_TOKEN", "")
PORT      = int(os.environ.get("VIDEO_STREAMER_PORT", "8765"))
PDV_BASE  = os.environ.get("PDV_BASE_DIR", "/home/rpdv/frente")
PDV_STATION = os.environ.get("PDV_STATION", "001")

_sema = threading.Semaphore(1)

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
            }
            if current['numero']:
                by_num[current['numero']] = current
        elif event == 'VIT' and current:
            current['itens'] += 1
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
         "-i", "pipe:0", "-t", "95",
         "-vf", "scale=480:-2", "-c:v", "libx264",
         "-preset", "ultrafast", "-crf", "35",
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
        req_token = params.get("token", [""])[0]
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

        # ── /cupons → lista todos os cupons do dia (do spy file)
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


if __name__ == "__main__":
    print("video_streamer: iniciado na porta %d" % PORT, flush=True)
    HTTPServer(("0.0.0.0", PORT), VideoStreamHandler).serve_forever()
