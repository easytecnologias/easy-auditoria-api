import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel

JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "8"))


def _hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def _verificar_senha(senha: str, hash_: str) -> bool:
    return bcrypt.checkpw(senha.encode(), hash_.encode())

DB_PATH = Path(os.environ.get("DB_PATH", "/data/easy-auditoria.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/data/images"))
VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "/data/videos"))
PURCHASE_VIDEOS_DIR = Path(os.environ.get("PURCHASE_VIDEOS_DIR", "/data/purchase_videos"))

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
        cols_lojas = {row["name"] for row in conn.execute("PRAGMA table_info(lojas)").fetchall()}
        if cols_lojas and "pdv_nome" not in cols_lojas:
            conn.execute("ALTER TABLE lojas ADD COLUMN pdv_nome TEXT")
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
        admin = conn.execute("SELECT id FROM usuarios WHERE perfil = 'admin' LIMIT 1").fetchone()
        if admin is None:
            senha = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(12)
            conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, loja_id) VALUES (?, ?, ?, 'admin', NULL)",
                ("Administrador", "admin@easy.local", _hash_senha(senha)),
            )
            conn.commit()
            print(f"[easy-auditoria-api] Admin criado — email: admin@easy.local  senha: {senha}")


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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


# --- JWT helpers ---

def _criar_token(usuario_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(usuario_id), "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")


def autenticar_usuario(
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")
    usuario_id = _decode_token(authorization[len("Bearer "):].strip())
    row = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (usuario_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Usuário inativo ou não encontrado")
    return row


def requer_perfil(*perfis: str):
    def dep(usuario: sqlite3.Row = Depends(autenticar_usuario)) -> sqlite3.Row:
        if usuario["perfil"] not in perfis:
            raise HTTPException(status_code=403, detail="Sem permissão")
        return usuario
    return dep


# --- Auth endpoints ---

class LoginIn(BaseModel):
    email: str
    senha: str


@app.post("/auth/login")
def login(dados: LoginIn, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM usuarios WHERE email = ? AND ativo = 1", (dados.email,)
    ).fetchone()
    if row is None or not _verificar_senha(dados.senha, row["senha_hash"]):
        raise HTTPException(status_code=401, detail="Email ou senha inválidos")
    return {
        "token": _criar_token(row["id"]),
        "usuario": {
            "id": row["id"],
            "nome": row["nome"],
            "email": row["email"],
            "perfil": row["perfil"],
            "loja_id": row["loja_id"],
        },
    }


@app.get("/auth/me")
def me(usuario: sqlite3.Row = Depends(autenticar_usuario)):
    return {
        "id": usuario["id"],
        "nome": usuario["nome"],
        "email": usuario["email"],
        "perfil": usuario["perfil"],
        "loja_id": usuario["loja_id"],
    }


# --- CRUD de usuários ---

HIERARQUIA = {"admin": 3, "supervisor": 2, "operador": 1}


class UsuarioIn(BaseModel):
    nome: str
    email: str
    senha: str
    perfil: str
    loja_id: Optional[str] = None


class UsuarioUpdate(BaseModel):
    nome: Optional[str] = None
    email: Optional[str] = None
    perfil: Optional[str] = None
    loja_id: Optional[str] = None
    ativo: Optional[int] = None


class SenhaUpdate(BaseModel):
    nova_senha: str


def _pode_gerenciar(quem: sqlite3.Row, perfil_alvo: str, loja_alvo: Optional[str]) -> bool:
    """Retorna True se `quem` tem autoridade para criar/editar um usuário com perfil_alvo na loja_alvo."""
    if quem["perfil"] == "admin":
        return True
    if quem["perfil"] == "supervisor":
        # supervisor só pode gerenciar operadores da própria loja
        return perfil_alvo == "operador" and quem["loja_id"] == loja_alvo
    return False


@app.get("/api/v1/usuarios")
def listar_usuarios(
    usuario: sqlite3.Row = Depends(requer_perfil("admin", "supervisor")),
    db: sqlite3.Connection = Depends(get_db),
):
    if usuario["perfil"] == "admin":
        rows = db.execute("SELECT id, nome, email, perfil, loja_id, ativo, criado_em FROM usuarios ORDER BY nome").fetchall()
    else:
        rows = db.execute(
            "SELECT id, nome, email, perfil, loja_id, ativo, criado_em FROM usuarios WHERE loja_id = ? ORDER BY nome",
            (usuario["loja_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/v1/usuarios", status_code=201)
def criar_usuario(
    dados: UsuarioIn,
    usuario: sqlite3.Row = Depends(requer_perfil("admin", "supervisor")),
    db: sqlite3.Connection = Depends(get_db),
):
    if dados.perfil not in HIERARQUIA:
        raise HTTPException(status_code=400, detail="Perfil inválido")
    if not _pode_gerenciar(usuario, dados.perfil, dados.loja_id):
        raise HTTPException(status_code=403, detail="Sem permissão para criar este perfil")
    try:
        cur = db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, perfil, loja_id) VALUES (?, ?, ?, ?, ?)",
            (dados.nome, dados.email, _hash_senha(dados.senha), dados.perfil, dados.loja_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Email já cadastrado")
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/v1/usuarios/{usuario_id}")
def editar_usuario(
    usuario_id: int,
    dados: UsuarioUpdate,
    usuario: sqlite3.Row = Depends(requer_perfil("admin", "supervisor")),
    db: sqlite3.Connection = Depends(get_db),
):
    alvo = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    perfil_novo = dados.perfil or alvo["perfil"]
    loja_nova = dados.loja_id if dados.loja_id is not None else alvo["loja_id"]
    if not _pode_gerenciar(usuario, perfil_novo, loja_nova):
        raise HTTPException(status_code=403, detail="Sem permissão para editar este usuário")
    campos = {k: v for k, v in dados.model_dump().items() if v is not None}
    if not campos:
        return {"ok": True}
    sets = ", ".join(f"{k} = ?" for k in campos)
    db.execute(f"UPDATE usuarios SET {sets} WHERE id = ?", (*campos.values(), usuario_id))
    db.commit()
    return {"ok": True}


@app.post("/api/v1/usuarios/{usuario_id}/senha")
def redefinir_senha(
    usuario_id: int,
    dados: SenhaUpdate,
    usuario: sqlite3.Row = Depends(requer_perfil("admin", "supervisor")),
    db: sqlite3.Connection = Depends(get_db),
):
    alvo = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if not _pode_gerenciar(usuario, alvo["perfil"], alvo["loja_id"]):
        raise HTTPException(status_code=403, detail="Sem permissão")
    db.execute(
        "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
        (_hash_senha(dados.nova_senha), usuario_id),
    )
    db.commit()
    return {"ok": True}


@app.delete("/api/v1/usuarios/{usuario_id}")
def desativar_usuario(
    usuario_id: int,
    usuario: sqlite3.Row = Depends(requer_perfil("admin", "supervisor")),
    db: sqlite3.Connection = Depends(get_db),
):
    alvo = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if not _pode_gerenciar(usuario, alvo["perfil"], alvo["loja_id"]):
        raise HTTPException(status_code=403, detail="Sem permissão")
    if usuario_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="Não é possível desativar o próprio usuário")
    db.execute("UPDATE usuarios SET ativo = 0 WHERE id = ?", (usuario_id,))
    db.commit()
    return {"ok": True}


# --- CRUD de lojas ---

class LojaIn(BaseModel):
    id: str
    nome: str
    pdv_nome: Optional[str] = None


class LojaUpdate(BaseModel):
    nome: Optional[str] = None
    pdv_nome: Optional[str] = None


@app.get("/api/v1/lojas")
def listar_lojas(
    usuario: sqlite3.Row = Depends(requer_perfil("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    rows = db.execute("SELECT id, nome, pdv_nome, api_token, criado_em FROM lojas ORDER BY nome").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/v1/lojas", status_code=201)
def criar_loja(
    dados: LojaIn,
    usuario: sqlite3.Row = Depends(requer_perfil("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_id = dados.id.strip().lower()
    if not loja_id:
        raise HTTPException(status_code=400, detail="ID inválido")
    token = secrets.token_hex(24)
    try:
        db.execute(
            "INSERT INTO lojas (id, slug, nome, pdv_nome, api_token) VALUES (?, ?, ?, ?, ?)",
            (loja_id, loja_id, dados.nome, dados.pdv_nome, token),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="ID já cadastrado")
    return {"ok": True, "id": loja_id, "api_token": token}


@app.put("/api/v1/lojas/{loja_id}")
def editar_loja(
    loja_id: str,
    dados: LojaUpdate,
    usuario: sqlite3.Row = Depends(requer_perfil("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    loja = db.execute("SELECT id FROM lojas WHERE id = ?", (loja_id,)).fetchone()
    if loja is None:
        raise HTTPException(status_code=404, detail="Loja não encontrada")
    campos = {k: v for k, v in dados.model_dump().items() if v is not None}
    if campos:
        sets = ", ".join(f"{k} = ?" for k in campos)
        db.execute(f"UPDATE lojas SET {sets} WHERE id = ?", (*campos.values(), loja_id))
        db.commit()
    return {"ok": True}


@app.delete("/api/v1/lojas/{loja_id}")
def excluir_loja(
    loja_id: str,
    usuario: sqlite3.Row = Depends(requer_perfil("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    loja = db.execute("SELECT id FROM lojas WHERE id = ?", (loja_id,)).fetchone()
    if loja is None:
        raise HTTPException(status_code=404, detail="Loja não encontrada")
    eventos = db.execute(
        "SELECT COUNT(*) AS c FROM auditoria_eventos WHERE loja_id = ?", (loja_id,)
    ).fetchone()
    if eventos["c"] > 0:
        raise HTTPException(status_code=409, detail=f"Loja possui {eventos['c']} eventos registrados")
    db.execute("DELETE FROM lojas WHERE id = ?", (loja_id,))
    db.commit()
    return {"ok": True}


@app.post("/api/v1/lojas/{loja_id}/token")
def regenerar_token_loja(
    loja_id: str,
    usuario: sqlite3.Row = Depends(requer_perfil("admin")),
    db: sqlite3.Connection = Depends(get_db),
):
    loja = db.execute("SELECT id FROM lojas WHERE id = ?", (loja_id,)).fetchone()
    if loja is None:
        raise HTTPException(status_code=404, detail="Loja não encontrada")
    token = secrets.token_hex(24)
    db.execute("UPDATE lojas SET api_token = ? WHERE id = ?", (token, loja_id))
    db.commit()
    return {"ok": True, "api_token": token}


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
        "timestamp": row["timestamp"] or "",
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
        "imageUrl": f"/api/v1/events/{row['id']}/image" if row["imagem"] else None,
        "videoUrl": f"/api/v1/events/{row['id']}/video",
    }


@app.get("/api/v1/alerts")
def listar_alertas(
    loja: str,
    filter: str = "all",
    data: Optional[str] = None,
    cupom: Optional[str] = None,
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
    if cupom:
        query += " AND cupom = ?"
        params.append(cupom)
    if pdv:
        query += f" AND pdv IN ({','.join('?' * len(pdv))})"
        params.extend(pdv)
    if filter == "critical":
        query += " AND severidade = 'critical'"
    elif filter == "review":
        query += " AND status != 'resolved'"
    elif filter == "resolved":
        query += " AND status = 'resolved'"

    # Quando busca por cupom específico: ordem cronológica (ASC)
    # Quando lista todos os alertas: mais recente primeiro (DESC)
    order = "ASC" if cupom else "DESC"
    query += f" ORDER BY timestamp {order} LIMIT 200"
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


def _purchase_video_path(loja_id: int, pdv: str, cupom: str) -> Path:
    PURCHASE_VIDEOS_DIR.mkdir(exist_ok=True)
    return PURCHASE_VIDEOS_DIR / f"{loja_id}_{pdv}_{cupom}.mp4"


@app.post("/api/v1/cupom_video/request")
def solicitar_video_cupom(
    cupom: str = Query(...),
    pdv: str = Query(...),
    start_time: str = Query(...),
    end_time: str = Query(...),
    loja: Optional[str] = Query(default=None),
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_id = usuario["loja_id"]
    if not loja_id:
        # admin sem loja fixa — usa o slug enviado pelo dashboard
        if not loja:
            raise HTTPException(status_code=403, detail="Informe o parâmetro loja")
        row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Loja não encontrada")
        loja_id = row["id"]
    if _purchase_video_path(loja_id, pdv, cupom).is_file():
        return {"status": "ready"}
    db.execute(
        "INSERT OR REPLACE INTO video_requests (loja_id, pdv, cupom, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'pending')",
        (loja_id, pdv, cupom, start_time, end_time),
    )
    db.commit()
    return {"status": "queued"}


@app.get("/api/v1/cupom_video/pending")
def listar_video_pendentes(
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    rows = db.execute(
        "SELECT cupom, pdv, start_time, end_time FROM video_requests WHERE loja_id = ? AND status = 'pending' ORDER BY criado_em LIMIT 3",
        (loja["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/v1/cupom_video/request/failed")
def marcar_video_failed(
    cupom: str = Query(...),
    pdv: str = Query(...),
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    db.execute(
        "UPDATE video_requests SET status = 'failed' WHERE loja_id = ? AND pdv = ? AND cupom = ?",
        (loja["id"], pdv, cupom),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/v1/cupom_video/status")
def status_video_request(
    cupom: str = Query(...),
    pdv: str = Query(...),
    loja: str = Query(...),
    db: sqlite3.Connection = Depends(get_db),
):
    row_loja = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
    if row_loja is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")
    row = db.execute(
        "SELECT status FROM video_requests WHERE loja_id = ? AND pdv = ? AND cupom = ?",
        (row_loja["id"], pdv, cupom),
    ).fetchone()
    if row is None:
        return {"status": "not_found"}
    return {"status": row["status"]}


@app.post("/api/v1/cupom_video")
def upload_video_cupom(
    cupom: str = Query(...),
    pdv: str = Query(...),
    file: UploadFile = None,
    loja: sqlite3.Row = Depends(autenticar_loja),
    db: sqlite3.Connection = Depends(get_db),
):
    if file is None:
        raise HTTPException(status_code=400, detail="Arquivo ausente")
    caminho = _purchase_video_path(loja["id"], pdv, cupom)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(file.file.read())
    db.execute(
        "UPDATE video_requests SET status = 'done' WHERE loja_id = ? AND pdv = ? AND cupom = ?",
        (loja["id"], pdv, cupom),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/v1/cupom_video")
def obter_video_cupom(
    cupom: str = Query(...),
    pdv: str = Query(...),
    loja: str = Query(...),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")
    caminho = _purchase_video_path(row["id"], pdv, cupom)
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
