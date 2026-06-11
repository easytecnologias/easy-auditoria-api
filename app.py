import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("DB_PATH", "/data/easy-auditoria.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/data/images"))
VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "/data/videos"))

RESULTADO_LABELS = {
    "CONFERE": "Confere",
    "CONFERE_POR_REGRA_DE_VALOR": "Confere",
    "NAO_CONFERE": "Não confere",
    "INCONCLUSIVO": "Inconclusivo",
    "NAO_ANALISADO": "Não analisado",
}

EVENTO_LABELS = {
    "CONFERE": ("Conferido", "Produto e registro compatíveis"),
    "CONFERE_POR_REGRA_DE_VALOR": ("Conferido", "Liberado por regra de valor"),
    "NAO_CONFERE": ("Produto incompatível", "Divergência identificada na análise visual"),
    "INCONCLUSIVO": ("Imagem inconclusiva", "Revisão manual recomendada"),
    "NAO_ANALISADO": ("Sem análise visual", "Auditoria visual não executada"),
}

STATE_LABELS = {
    "pending": "Em revisão",
    "review": "Revisar",
    "resolved": "Salvo",
    "ignored": "Ignorado",
}


def severidade_de_acao(acao_recomendada: Optional[str]) -> str:
    acao = (acao_recomendada or "").strip().lower()
    if acao == "revisar cupom":
        return "critical"
    if acao == "liberar":
        return "ok"
    return "warning"


def status_de_resultado(resultado: Optional[str]) -> str:
    if (resultado or "").startswith("CONFERE"):
        return "resolved"
    return "pending"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        colunas = {row["name"] for row in conn.execute("PRAGMA table_info(pdv_sales)").fetchall()}
        if colunas and "data" not in colunas:
            conn.execute("DROP TABLE pdv_sales")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        row = conn.execute("SELECT id FROM lojas WHERE id = ?", ("loja-106",)).fetchone()
        if row is None:
            token = os.environ.get("LOJA_106_TOKEN") or secrets.token_hex(24)
            conn.execute(
                "INSERT INTO lojas (id, slug, nome, api_token) VALUES (?, ?, ?, ?)",
                ("loja-106", "loja-106", "Loja 106", token),
            )
            conn.commit()
            print(f"[easy-auditoria-api] Token da loja-106: {token}")


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_db():
    with get_connection() as conn:
        yield conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Easy Auditoria API", lifespan=lifespan)


def autenticar_loja(
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")
    token = authorization[len("Bearer "):].strip()
    loja = db.execute("SELECT * FROM lojas WHERE api_token = ?", (token,)).fetchone()
    if loja is None:
        raise HTTPException(status_code=401, detail="Token invalido")
    return loja


class ResultadoIn(BaseModel):
    resultado: str
    confianca: Optional[int] = None
    comparacao_pdv: Optional[str] = None
    possivel_divergencia: Optional[str] = None
    acao_recomendada: Optional[str] = None


class EventoIn(BaseModel):
    timestamp: str
    pdv: str
    cupom: Optional[str] = None
    imagem: Optional[str] = None
    produto: str
    valor_unitario: float
    quantidade: float
    modo: str
    resultado: ResultadoIn


class HealthItemIn(BaseModel):
    pdv: str
    bridge: str
    imhdx: str
    audit: str


class SalesIn(BaseModel):
    pdv: str
    total: float
    cupons: int
    data: Optional[str] = None


@app.post("/api/v1/events")
def criar_evento(
    evento: EventoIn,
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    severidade = severidade_de_acao(evento.resultado.acao_recomendada)
    status = status_de_resultado(evento.resultado.resultado)
    db.execute(
        """
        INSERT INTO auditoria_eventos (
            loja_id, timestamp, pdv, cupom, imagem, produto, valor, quantidade,
            modo, resultado, confianca, comparacao_pdv, possivel_divergencia,
            acao_recomendada, severidade, status, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (loja_id, timestamp, pdv, imagem) DO NOTHING
        """,
        (
            loja["id"],
            evento.timestamp,
            evento.pdv,
            evento.cupom,
            evento.imagem,
            evento.produto,
            evento.valor_unitario,
            evento.quantidade,
            evento.modo,
            evento.resultado.resultado,
            evento.resultado.confianca,
            evento.resultado.comparacao_pdv,
            evento.resultado.possivel_divergencia,
            evento.resultado.acao_recomendada,
            severidade,
            status,
            json.dumps(evento.model_dump(), ensure_ascii=False),
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT id FROM auditoria_eventos WHERE loja_id = ? AND timestamp = ? AND pdv = ? AND imagem IS ?",
        (loja["id"], evento.timestamp, evento.pdv, evento.imagem),
    ).fetchone()
    return {"ok": True, "id": row["id"] if row else None}


@app.post("/api/v1/health")
def atualizar_health(
    itens: list[HealthItemIn],
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    for item in itens:
        db.execute(
            """
            INSERT INTO pdv_health (loja_id, pdv, bridge, imhdx, audit, atualizado_em)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (loja_id, pdv) DO UPDATE SET
                bridge = excluded.bridge,
                imhdx = excluded.imhdx,
                audit = excluded.audit,
                atualizado_em = excluded.atualizado_em
            """,
            (loja["id"], item.pdv, item.bridge, item.imhdx, item.audit),
        )
    db.commit()
    return {"ok": True}


@app.post("/api/v1/sales")
def atualizar_vendas(
    vendas: SalesIn,
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    data = vendas.data or date.today().isoformat()
    db.execute(
        """
        INSERT INTO pdv_sales (loja_id, pdv, data, total, cupons, atualizado_em)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (loja_id, pdv, data) DO UPDATE SET
            total = excluded.total,
            cupons = excluded.cupons,
            atualizado_em = excluded.atualizado_em
        """,
        (loja["id"], vendas.pdv, data, vendas.total, vendas.cupons),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/v1/sales")
def obter_vendas(
    loja: str,
    data: Optional[str] = None,
    pdv: list[str] = Query(default=[]),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
    if loja_row is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")

    data = data or date.today().isoformat()
    query = "SELECT COALESCE(SUM(total), 0) AS total, COALESCE(SUM(cupons), 0) AS cupons FROM pdv_sales WHERE loja_id = ? AND data = ?"
    params: list = [loja_row["id"], data]
    if pdv:
        query += f" AND pdv IN ({','.join('?' * len(pdv))})"
        params.extend(pdv)

    row = db.execute(query, params).fetchone()
    return {"total": row["total"], "cupons": row["cupons"]}


def _formatar_pdv(pdv: str) -> str:
    pdv = (pdv or "").strip()
    if not pdv:
        return "-"
    if pdv.upper().startswith("PDV"):
        return pdv.upper()
    return f"PDV {pdv.zfill(2)}"


def _formatar_qty(quantidade: float) -> str:
    if quantidade == int(quantidade):
        qtd = int(quantidade)
        return f"{qtd} unidade" + ("s" if qtd != 1 else "")
    return f"{quantidade:g} kg".replace(".", ",")


def _formatar_valor(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _evento_para_alerta(row: sqlite3.Row) -> dict:
    resultado = row["resultado"] or "NAO_ANALISADO"
    event, default_subtitle = EVENTO_LABELS.get(resultado, EVENTO_LABELS["NAO_ANALISADO"])
    status = row["status"]
    state = "resolved" if status in ("resolved", "ignored") else status
    return {
        "id": row["id"],
        "severity": row["severidade"],
        "time": (row["timestamp"] or "")[11:19] or (row["timestamp"] or ""),
        "pdv": _formatar_pdv(row["pdv"]),
        "receipt": row["cupom"] or "-",
        "event": event,
        "subtitle": default_subtitle,
        "product": row["produto"],
        "code": "-",
        "confidence": row["confianca"] if row["confianca"] is not None else 0,
        "state": state,
        "stateText": STATE_LABELS.get(status, "Em revisão"),
        "qty": _formatar_qty(row["quantidade"] or 0),
        "value": _formatar_valor(row["valor"] or 0),
        "result": RESULTADO_LABELS.get(resultado, resultado),
        "analysis": row["comparacao_pdv"] or "",
        "note": row["possivel_divergencia"] or "",
        "imageUrl": f"/api/v1/events/{row['id']}/image",
        "videoUrl": f"/api/v1/events/{row['id']}/video",
    }


@app.get("/api/v1/alerts")
def listar_alertas(
    loja: str,
    filter: str = "all",
    data: Optional[str] = None,
    pdv: list[str] = Query(default=[]),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
    if loja_row is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")

    query = "SELECT * FROM auditoria_eventos WHERE loja_id = ?"
    params: list = [loja_row["id"]]
    if data:
        query += " AND timestamp LIKE ?"
        params.append(f"{data}%")
    if pdv:
        query += f" AND pdv IN ({','.join('?' * len(pdv))})"
        params.extend(pdv)
    if filter == "critical":
        query += " AND severidade = 'critical'"
    elif filter == "review":
        query += " AND status != 'resolved'"
    elif filter == "resolved":
        query += " AND status = 'resolved'"

    query += " ORDER BY timestamp DESC LIMIT 200"
    rows = db.execute(query, params).fetchall()
    return [_evento_para_alerta(row) for row in rows]


@app.get("/api/v1/health")
def listar_health(loja: str, db: sqlite3.Connection = Depends(get_db)):
    loja_row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
    if loja_row is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")

    rows = db.execute(
        "SELECT pdv, bridge, imhdx, audit FROM pdv_health WHERE loja_id = ? ORDER BY pdv",
        (loja_row["id"],),
    ).fetchall()
    return [dict(row) for row in rows]


def _imagem_path(evento_id: int) -> Path:
    return IMAGES_DIR / f"{evento_id}.jpg"


def _video_path(evento_id: int) -> Path:
    return VIDEOS_DIR / f"{evento_id}.mp4"


@app.post("/api/v1/events/{evento_id}/image")
def enviar_imagem_evento(
    evento_id: int,
    file: UploadFile,
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        "SELECT id FROM auditoria_eventos WHERE id = ? AND loja_id = ?",
        (evento_id, loja["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")
    _imagem_path(evento_id).write_bytes(file.file.read())
    return {"ok": True}


@app.get("/api/v1/events/{evento_id}/image")
def obter_imagem_evento(evento_id: int):
    caminho = _imagem_path(evento_id)
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Imagem nao encontrada")
    return FileResponse(caminho, media_type="image/jpeg")


@app.post("/api/v1/events/{evento_id}/video")
def enviar_video_evento(
    evento_id: int,
    file: UploadFile,
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        "SELECT id FROM auditoria_eventos WHERE id = ? AND loja_id = ?",
        (evento_id, loja["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")
    _video_path(evento_id).write_bytes(file.file.read())
    return {"ok": True}


@app.get("/api/v1/events/{evento_id}/video")
def obter_video_evento(evento_id: int):
    caminho = _video_path(evento_id)
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Video nao encontrado")
    return FileResponse(caminho, media_type="video/mp4")


class DecisionIn(BaseModel):
    action: str


@app.post("/api/v1/alerts/{alerta_id}/decision")
def decidir_alerta(alerta_id: int, decisao: DecisionIn, db: sqlite3.Connection = Depends(get_db)):
    if decisao.action not in ("save", "ignore"):
        raise HTTPException(status_code=400, detail="Acao invalida")
    novo_status = "resolved" if decisao.action == "save" else "ignored"
    cursor = db.execute(
        "UPDATE auditoria_eventos SET status = ? WHERE id = ?",
        (novo_status, alerta_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alerta nao encontrado")
    if decisao.action == "ignore":
        _imagem_path(alerta_id).unlink(missing_ok=True)
        _video_path(alerta_id).unlink(missing_ok=True)
    return {"ok": True, "status": novo_status}
