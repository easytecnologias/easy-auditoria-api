import json
import os
import subprocess
import time
try:
    import requests as _requests
    _USE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    _USE_REQUESTS = False

API_URL = os.environ.get("AUDITORIA_API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("AUDITORIA_API_TOKEN", "")
PDV_STATION = os.environ.get("PDV_STATION", "1")
IMHDX_HOST = os.environ.get("IMHDX_HOST", "")
IMHDX_USER = os.environ.get("IMHDX_USER", "")
IMHDX_PASS = os.environ.get("IMHDX_PASS", "")
IMHDX_CHANNEL = os.environ.get("IMHDX_CHANNEL", "1")


def _post(path, payload):
    headers = {
        "Authorization": "Bearer %s" % API_TOKEN,
        "Content-Type": "application/json",
    }
    if _USE_REQUESTS:
        r = _requests.post("%s%s" % (API_URL, path), json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            print("dashboard_reporter: %s retornou %s: %s" % (path, r.status_code, r.text[:200]), flush=True)
    else:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            "%s%s" % (API_URL, path),
            data=body,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)


def _servico_ativo(nome):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", nome],
            capture_output=True, text=True, timeout=5,
        )
        return "online" if r.stdout.strip() == "active" else "offline"
    except Exception:
        return "warning"


def postar_health(imhdx_ativo=True):
    if not API_URL or not API_TOKEN:
        return
    try:
        bridge = _servico_ativo("pdv-intelbras-bridge")
        imhdx = "online" if imhdx_ativo else "offline"
        payload = [{"pdv": str(PDV_STATION), "bridge": bridge, "imhdx": imhdx, "audit": "online"}]
        _post("/api/v1/health", payload)
    except Exception as exc:
        print("dashboard_reporter: erro health: %s" % exc, flush=True)


def postar_vendas(cups):
    if not API_URL or not API_TOKEN:
        return
    try:
        import datetime as _dt
        hoje = _dt.date.today().isoformat()  # data local do PDV (ex: "2026-06-18")

        def _cup_data(c):
            """Retorna a data do cupom (YYYY-MM-DD) ou None se não determinável."""
            ts = c.get("open_time") or c.get("close_time") or c.get("timestamp") or ""
            if ts and len(ts) >= 10:
                return ts[:10]
            return None

        closed = [
            c for c in (cups or [])
            if c.get("closed") and (_cup_data(c) in (hoje, None))
        ]
        total = sum(
            float(c.get("total") or c.get("subtotal") or
                  sum(float(i.get("value") or 0) for i in c.get("items", [])))
            for c in closed
        )
        payload = {
            "pdv": str(PDV_STATION),
            "total": round(total, 2),
            "cupons": len(closed),
            "data": hoje,
        }
        _post("/api/v1/sales", payload)
    except Exception as exc:
        print("dashboard_reporter: erro vendas: %s" % exc, flush=True)


def _item_timestamps(cup):
    """Retorna (start_dt, end_dt) calculados a partir dos horários reais dos itens."""
    import datetime as _dt
    items = cup.get("items", [])
    times = sorted(i.get("time", "") for i in items if i.get("time"))
    if not times:
        return None, None
    today = _dt.date.today().strftime("%Y-%m-%d")
    try:
        first = _dt.datetime.strptime("%s %s" % (today, times[0]), "%Y-%m-%d %H:%M:%S")
        last = _dt.datetime.strptime("%s %s" % (today, times[-1]), "%Y-%m-%d %H:%M:%S")
        # Virada do dia: se last < first, last pertence ao dia seguinte
        if last < first:
            last += _dt.timedelta(days=1)
    except ValueError:
        return None, None
    # 5s antes do primeiro item, 25s depois do último (inclui pagamento)
    start = first - _dt.timedelta(seconds=5)
    end   = last  + _dt.timedelta(seconds=25)
    # Cap de 5 minutos para o worker (arquivo gerado = upload controlado)
    MAX_SECS = 300
    if (end - start).total_seconds() > MAX_SECS:
        mid   = start + (end - start) / 2
        start = mid - _dt.timedelta(seconds=MAX_SECS // 2)
        end   = mid + _dt.timedelta(seconds=MAX_SECS // 2)
    return start, end


def _dvr_para_mp4(start_dt, end_dt, cupom_num):
    """Baixa clipe do DVR iMHDX e envia para a API. Retorna True se enviou."""
    import subprocess
    import tempfile
    from urllib.parse import quote as _q
    from requests.auth import HTTPDigestAuth

    duration = int((end_dt - start_dt).total_seconds())
    if duration <= 0 or duration > 3600:
        return False

    url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
        % (IMHDX_HOST, IMHDX_CHANNEL,
           _q(start_dt.strftime("%Y-%m-%d %H:%M:%S")),
           _q(end_dt.strftime("%Y-%m-%d %H:%M:%S")))
    )
    r = _requests.get(url, auth=HTTPDigestAuth(IMHDX_USER, IMHDX_PASS), timeout=120)
    if r.status_code != 200 or len(r.content) < 2048 or not r.content.startswith(b"DHAV"):
        print("dashboard_reporter: video %s: DVR sem gravacao" % cupom_num, flush=True)
        return False

    with tempfile.TemporaryDirectory(prefix="dr_video_") as tmp:
        dav = os.path.join(tmp, "clip.dav")
        mp4 = os.path.join(tmp, "clip.mp4")
        with open(dav, "wb") as f:
            f.write(r.content)
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", dav, "-t", str(duration + 5),
             "-vf", "scale=480:-2", "-c:v", "libx264", "-preset", "fast",
             "-crf", "32", "-movflags", "+faststart", mp4],
            capture_output=True, timeout=300,
        )
        if res.returncode != 0 or not os.path.exists(mp4):
            return False
        with open(mp4, "rb") as f:
            _requests.post(
                "%s/api/v1/cupom_video?cupom=%s&pdv=%s" % (API_URL, cupom_num, PDV_STATION),
                files={"file": ("video.mp4", f, "video/mp4")},
                headers={"Authorization": "Bearer %s" % API_TOKEN},
                timeout=120,
            )
        print("dashboard_reporter: video %s enviado" % cupom_num, flush=True)
        return True


def postar_video_cupom(cup):
    """Captura vídeo de um cupom usando os timestamps dos seus itens."""
    if not all([API_URL, API_TOKEN, IMHDX_HOST, IMHDX_USER, IMHDX_PASS]):
        return
    if not _USE_REQUESTS:
        return
    try:
        start_dt, end_dt = _item_timestamps(cup)
        if not start_dt or not end_dt:
            return
        _dvr_para_mp4(start_dt, end_dt, str(cup.get("number", "")))
    except Exception as exc:
        print("dashboard_reporter: erro video cupom: %s" % exc, flush=True)


def _marcar_request_failed(cupom, pdv):
    """Marca request de vídeo como failed na API para que o dashboard pare de esperar."""
    try:
        _requests.post(
            "%s/api/v1/cupom_video/request/failed?cupom=%s&pdv=%s" % (API_URL, cupom, pdv),
            headers={"Authorization": "Bearer %s" % API_TOKEN},
            timeout=5,
        )
    except Exception:
        pass


def poll_video_pendentes():
    """Busca solicitações de vídeo do dashboard e processa uma por vez."""
    if not all([API_URL, API_TOKEN, IMHDX_HOST, IMHDX_USER, IMHDX_PASS]):
        return []
    if not _USE_REQUESTS:
        return []
    try:
        r = _requests.get(
            "%s/api/v1/cupom_video/pending" % API_URL,
            headers={"Authorization": "Bearer %s" % API_TOKEN},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        reqs = r.json() or []
        if reqs:
            print("dashboard_reporter: %d request(s) pendente(s): %s" % (
                len(reqs), [x.get("cupom") for x in reqs]), flush=True)
        return reqs
    except Exception as exc:
        print("dashboard_reporter: erro poll pendentes: %s" % exc, flush=True)
        return []


def processar_video_request(req):
    """Processa uma solicitação explícita de vídeo (start_time/end_time do dashboard)."""
    if not all([API_URL, API_TOKEN, IMHDX_HOST, IMHDX_USER, IMHDX_PASS]):
        return
    if not _USE_REQUESTS:
        return
    cupom = str(req.get("cupom", ""))
    pdv = str(req.get("pdv", ""))
    try:
        import datetime as _dt
        start_dt = _dt.datetime.strptime(req["start_time"], "%Y-%m-%d %H:%M:%S")
        end_dt = _dt.datetime.strptime(req["end_time"], "%Y-%m-%d %H:%M:%S")
        ok = _dvr_para_mp4(start_dt, end_dt, cupom)
        if not ok:
            _marcar_request_failed(cupom, pdv)
    except Exception as exc:
        print("dashboard_reporter: erro video request %s: %s" % (cupom, exc), flush=True)
        _marcar_request_failed(cupom, pdv)


def postar_evento(cup, item, audit, modo="produto"):
    if not API_URL or not API_TOKEN:
        return
    try:
        qty = float(item.get("qty") or 1)
        total = float(item.get("value") or 0)
        unit_price = total / qty if qty > 0 else total
        payload = {
            "timestamp": "%s %s" % (time.strftime("%Y-%m-%d"), item.get("time", "")),
            "pdv": str(PDV_STATION),
            "cupom": str(cup.get("number", "")),
            "produto": item.get("desc", ""),
            "valor_unitario": round(unit_price, 2),
            "quantidade": qty,
            "modo": modo,
            "resultado": {
                "resultado": audit.get("resultado", "NAO_ANALISADO"),
                "confianca": int(audit.get("confianca") or 0),
                "comparacao_pdv": str(audit.get("comparacao_pdv") or ""),
                "possivel_divergencia": str(audit.get("possivel_divergencia") or ""),
                "acao_recomendada": str(audit.get("acao_recomendada") or ""),
            },
        }
        _post("/api/v1/events", payload)
    except Exception as exc:
        print("dashboard_reporter: erro evento: %s" % exc, flush=True)
