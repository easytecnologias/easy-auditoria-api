import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel, Field

JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    import sys
    JWT_SECRET = secrets.token_hex(32)
    print("[AVISO] JWT_SECRET nao definido — tokens serao invalidados ao reiniciar. "
          "Defina JWT_SECRET no ambiente de producao.", file=sys.stderr)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "8"))
LOGIN_LIMIT_ATTEMPTS = int(os.environ.get("LOGIN_LIMIT_ATTEMPTS", "8"))
LOGIN_LIMIT_WINDOW_SECONDS = int(os.environ.get("LOGIN_LIMIT_WINDOW_SECONDS", "300"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_BYTES", str(250 * 1024 * 1024)))
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
PDV_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_ATTEMPTS_LAST_CLEAN: float = 0.0


def _hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def _verificar_senha(senha: str, hash_: str) -> bool:
    return bcrypt.checkpw(senha.encode(), hash_.encode())


def _validar_safe_id(valor: str, campo: str) -> str:
    valor = (valor or "").strip()
    if not SAFE_ID_RE.fullmatch(valor):
        raise HTTPException(status_code=422, detail=f"{campo} invalido")
    return valor


def _validar_pdv(valor: str) -> str:
    valor = (valor or "").strip()
    if not PDV_RE.fullmatch(valor):
        raise HTTPException(status_code=422, detail="PDV invalido")
    return valor


def _validar_data(valor: Optional[str]) -> Optional[str]:
    if valor is None:
        return None
    valor = valor.strip()
    if not DATE_RE.fullmatch(valor):
        raise HTTPException(status_code=422, detail="Data invalida. Use AAAA-MM-DD")
    try:
        date.fromisoformat(valor)
    except ValueError:
        raise HTTPException(status_code=422, detail="Data invalida. Use AAAA-MM-DD")
    return valor


def _ler_upload(file: UploadFile, max_bytes: int, tipos: set[str]) -> bytes:
    if file.content_type not in tipos:
        raise HTTPException(status_code=415, detail="Tipo de arquivo nao permitido")
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="Arquivo muito grande")
    return data


def _rate_limit_login(chave: str) -> None:
    global _LOGIN_ATTEMPTS_LAST_CLEAN
    agora = time.monotonic()
    # Limpar chaves inativas a cada 10 minutos para evitar crescimento ilimitado
    if agora - _LOGIN_ATTEMPTS_LAST_CLEAN > 600:
        expirado = [k for k, q in LOGIN_ATTEMPTS.items() if not q or agora - q[-1] > LOGIN_LIMIT_WINDOW_SECONDS]
        for k in expirado:
            del LOGIN_ATTEMPTS[k]
        _LOGIN_ATTEMPTS_LAST_CLEAN = agora
    fila = LOGIN_ATTEMPTS[chave]
    while fila and agora - fila[0] > LOGIN_LIMIT_WINDOW_SECONDS:
        fila.popleft()
    if len(fila) >= LOGIN_LIMIT_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde e tente novamente")
    fila.append(agora)


def _validar_datetime(valor: str, campo: str = "datetime") -> str:
    valor = (valor or "").strip()
    if not DATETIME_RE.fullmatch(valor):
        raise HTTPException(status_code=422, detail=f"{campo} invalido. Use AAAA-MM-DD HH:MM:SS")
    return valor

DB_PATH = Path(os.environ.get("DB_PATH", "/data/easy-auditoria.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/data/images"))
VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "/data/videos"))
PURCHASE_VIDEOS_DIR = Path(os.environ.get("PURCHASE_VIDEOS_DIR", "/data/purchase_videos"))

RESULTADO_LABELS = {
    "CONFERE": "Confere",
    "CONFERE_POR_REGRA_DE_VALOR": "Confere",
    "NAO_CONFERE": "Nao confere",
    "INCONCLUSIVO": "Inconclusivo",
    "NAO_ANALISADO": "Nao analisado",
    "DIVERGENCIA_CATEGORIA": "Divergencia de categoria",
}

EVENTO_LABELS = {
    "CONFERE": ("Conferido", "Produto e registro compativeis"),
    "CONFERE_POR_REGRA_DE_VALOR": ("Conferido", "Liberado por regra de valor"),
    "NAO_CONFERE": ("Produto incompativel", "Divergencia identificada na analise visual"),
    "INCONCLUSIVO": ("Imagem inconclusiva", "Revisao manual recomendada"),
    "NAO_ANALISADO": ("Sem analise visual", "Auditoria visual nao executada"),
    "DIVERGENCIA_CATEGORIA": ("Categoria divergente", "IA local detectou categoria diferente do cupom"),
}

STATE_LABELS = {
    "pending": "Em revisao",
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
            print("[easy-auditoria-api] Loja inicial criada. Guarde o token fora dos logs.")
        admin = conn.execute("SELECT id FROM usuarios WHERE perfil = 'admin' LIMIT 1").fetchone()
        if admin is None:
            senha = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(12)
            conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, loja_id) VALUES (?, ?, ?, 'admin', NULL)",
                ("Administrador", "admin@easy.local", _hash_senha(senha)),
            )
            conn.commit()
            print("[easy-auditoria-api] Admin inicial criado. Guarde a senha fora dos logs.")


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

# CORS: restringe a origens conhecidas (dashboard + rede local)
_CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "http://10.10.12.7:8098,https://10.10.12.7:8098,http://localhost:8098",
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    # CSP conservador: permite scripts/estilos inline apenas do próprio host
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "media-src 'self' http://138.99.28.216:8765 blob:; connect-src 'self' http://138.99.28.216:8765",
    )
    if request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# --- JWT helpers ---

def _criar_token(usuario_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(usuario_id), "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Token invÃ¡lido ou expirado")


def autenticar_usuario(
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")
    usuario_id = _decode_token(authorization[len("Bearer "):].strip())
    row = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (usuario_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="UsuÃ¡rio inativo ou nÃ£o encontrado")
    return row


def requer_perfil(*perfis: str):
    def dep(usuario: sqlite3.Row = Depends(autenticar_usuario)) -> sqlite3.Row:
        if usuario["perfil"] not in perfis:
            raise HTTPException(status_code=403, detail="Sem permissÃ£o")
        return usuario
    return dep


def _usuario_pode_acessar_loja(usuario: sqlite3.Row, loja_id: str) -> bool:
    return usuario["perfil"] == "admin" or usuario["loja_id"] == loja_id


def _loja_por_slug_autorizada(
    loja_slug: str,
    usuario: sqlite3.Row,
    db: sqlite3.Connection,
) -> sqlite3.Row:
    loja_slug = _validar_safe_id(loja_slug, "Loja")
    loja_row = db.execute("SELECT id, slug, nome FROM lojas WHERE slug = ?", (loja_slug,)).fetchone()
    if loja_row is None:
        raise HTTPException(status_code=404, detail="Loja nao encontrada")
    if not _usuario_pode_acessar_loja(usuario, loja_row["id"]):
        raise HTTPException(status_code=403, detail="Sem permissao para esta loja")
    return loja_row


def _evento_autorizado(
    evento_id: int,
    usuario: sqlite3.Row,
    db: sqlite3.Connection,
) -> sqlite3.Row:
    row = db.execute("SELECT id, loja_id FROM auditoria_eventos WHERE id = ?", (evento_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")
    if not _usuario_pode_acessar_loja(usuario, row["loja_id"]):
        raise HTTPException(status_code=403, detail="Sem permissao para este evento")
    return row


# --- Auth endpoints ---

class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    senha: str = Field(min_length=1, max_length=256)


@app.post("/auth/login")
def login(request: Request, dados: LoginIn, db: sqlite3.Connection = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    _rate_limit_login(f"{ip}:{dados.email.lower()}")
    row = db.execute(
        "SELECT * FROM usuarios WHERE email = ? AND ativo = 1", (dados.email,)
    ).fetchone()
    if row is None or not _verificar_senha(dados.senha, row["senha_hash"]):
        raise HTTPException(status_code=401, detail="Email ou senha invÃ¡lidos")
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


# --- CRUD de usuÃ¡rios ---

HIERARQUIA = {"admin": 3, "supervisor": 2, "operador": 1}


class UsuarioIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    senha: str = Field(min_length=8, max_length=256)
    perfil: str = Field(pattern=r"^(admin|supervisor|operador)$")
    loja_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class UsuarioUpdate(BaseModel):
    nome: Optional[str] = Field(default=None, min_length=1, max_length=120)
    email: Optional[str] = Field(default=None, min_length=3, max_length=254)
    perfil: Optional[str] = Field(default=None, pattern=r"^(admin|supervisor|operador)$")
    loja_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    ativo: Optional[int] = Field(default=None, ge=0, le=1)


class SenhaUpdate(BaseModel):
    nova_senha: str = Field(min_length=8, max_length=256)


def _pode_gerenciar(quem: sqlite3.Row, perfil_alvo: str, loja_alvo: Optional[str]) -> bool:
    """Retorna True se `quem` tem autoridade para criar/editar um usuÃ¡rio com perfil_alvo na loja_alvo."""
    if quem["perfil"] == "admin":
        return True
    if quem["perfil"] == "supervisor":
        # supervisor sÃ³ pode gerenciar operadores da prÃ³pria loja
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
        raise HTTPException(status_code=400, detail="Perfil invÃ¡lido")
    if not _pode_gerenciar(usuario, dados.perfil, dados.loja_id):
        raise HTTPException(status_code=403, detail="Sem permissÃ£o para criar este perfil")
    try:
        cur = db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, perfil, loja_id) VALUES (?, ?, ?, ?, ?)",
            (dados.nome, dados.email, _hash_senha(dados.senha), dados.perfil, dados.loja_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Email jÃ¡ cadastrado")
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
        raise HTTPException(status_code=404, detail="UsuÃ¡rio nÃ£o encontrado")
    perfil_novo = dados.perfil or alvo["perfil"]
    loja_nova = dados.loja_id if dados.loja_id is not None else alvo["loja_id"]
    if not _pode_gerenciar(usuario, perfil_novo, loja_nova):
        raise HTTPException(status_code=403, detail="Sem permissÃ£o para editar este usuÃ¡rio")
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
        raise HTTPException(status_code=404, detail="UsuÃ¡rio nÃ£o encontrado")
    if not _pode_gerenciar(usuario, alvo["perfil"], alvo["loja_id"]):
        raise HTTPException(status_code=403, detail="Sem permissÃ£o")
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
        raise HTTPException(status_code=404, detail="UsuÃ¡rio nÃ£o encontrado")
    if not _pode_gerenciar(usuario, alvo["perfil"], alvo["loja_id"]):
        raise HTTPException(status_code=403, detail="Sem permissÃ£o")
    if usuario_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="NÃ£o Ã© possÃ­vel desativar o prÃ³prio usuÃ¡rio")
    db.execute("UPDATE usuarios SET ativo = 0 WHERE id = ?", (usuario_id,))
    db.commit()
    return {"ok": True}


# --- CRUD de lojas ---

class LojaIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    nome: str = Field(min_length=1, max_length=120)
    pdv_nome: Optional[str] = Field(default=None, max_length=120)


class LojaUpdate(BaseModel):
    nome: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pdv_nome: Optional[str] = Field(default=None, max_length=120)


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
        raise HTTPException(status_code=400, detail="ID invÃ¡lido")
    token = secrets.token_hex(24)
    try:
        db.execute(
            "INSERT INTO lojas (id, slug, nome, pdv_nome, api_token) VALUES (?, ?, ?, ?, ?)",
            (loja_id, loja_id, dados.nome, dados.pdv_nome, token),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="ID jÃ¡ cadastrado")
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
        raise HTTPException(status_code=404, detail="Loja nÃ£o encontrada")
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
        raise HTTPException(status_code=404, detail="Loja nÃ£o encontrada")
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
        raise HTTPException(status_code=404, detail="Loja nÃ£o encontrada")
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
    resultado: str = Field(pattern=r"^(CONFERE|CONFERE_POR_REGRA_DE_VALOR|NAO_CONFERE|INCONCLUSIVO|NAO_ANALISADO|DIVERGENCIA_CATEGORIA)$")
    confianca: Optional[int] = Field(default=None, ge=0, le=100)
    comparacao_pdv: Optional[str] = Field(default=None, max_length=4000)
    possivel_divergencia: Optional[str] = Field(default=None, max_length=4000)
    acao_recomendada: Optional[str] = Field(default=None, max_length=80)


class EventoIn(BaseModel):
    timestamp: str = Field(min_length=10, max_length=32)
    pdv: str = Field(pattern=r"^[A-Za-z0-9_-]{1,16}$")
    cupom: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    imagem: Optional[str] = Field(default=None, max_length=512)
    produto: str = Field(min_length=1, max_length=255)
    valor_unitario: float = Field(ge=0, le=100000)
    quantidade: float = Field(ge=0, le=100000)
    modo: str = Field(min_length=1, max_length=40)
    resultado: ResultadoIn


class HealthItemIn(BaseModel):
    pdv: str = Field(pattern=r"^[A-Za-z0-9_-]{1,16}$")
    bridge: str = Field(max_length=40)
    imhdx: str = Field(max_length=40)
    audit: str = Field(max_length=40)


class SalesIn(BaseModel):
    pdv: str = Field(pattern=r"^[A-Za-z0-9_-]{1,16}$")
    total: float = Field(ge=0, le=100000000)
    cupons: int = Field(ge=0, le=1000000)
    data: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


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
    # ORDER BY id DESC para pegar o evento recém-inserido (não um anterior com mesma timestamp)
    row = db.execute(
        "SELECT id FROM auditoria_eventos WHERE loja_id = ? AND timestamp = ? AND pdv = ? AND imagem IS ? ORDER BY id DESC LIMIT 1",
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
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_row = _loja_por_slug_autorizada(loja, usuario, db)

    data = _validar_data(data) or date.today().isoformat()
    query = "SELECT COALESCE(SUM(total), 0) AS total, COALESCE(SUM(cupons), 0) AS cupons FROM pdv_sales WHERE loja_id = ? AND data = ?"
    params: list = [loja_row["id"], data]
    if pdv:
        pdv = [_validar_pdv(p) for p in pdv]
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


def _evento_para_alerta(row: sqlite3.Row, loja_token: str = "") -> dict:
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
        "stateText": STATE_LABELS.get(status, "Em revisÃ£o"),
        "qty": _formatar_qty(row["quantidade"] or 0),
        "value": _formatar_valor(row["valor"] or 0),
        "result": RESULTADO_LABELS.get(resultado, resultado),
        "analysis": row["comparacao_pdv"] or "",
        "note": row["possivel_divergencia"] or "",
        "imageUrl": (
            row["imagem"] if (row["imagem"] or "").startswith("/streamer/")
            else f"/api/v1/events/{row['id']}/image?token={loja_token}" if row["imagem"]
            else None
        ),
        "videoUrl": f"/api/v1/events/{row['id']}/video",
    }


@app.get("/api/v1/alerts")
def listar_alertas(
    loja: str,
    filter: str = "all",
    data: Optional[str] = None,
    cupom: Optional[str] = None,
    pdv: list[str] = Query(default=[]),
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_row = _loja_por_slug_autorizada(loja, usuario, db)

    query = "SELECT * FROM auditoria_eventos WHERE loja_id = ?"
    params: list = [loja_row["id"]]
    if data:
        data = _validar_data(data)
        query += " AND timestamp LIKE ?"
        params.append(f"{data}%")
    if cupom:
        cupom = _validar_safe_id(cupom, "Cupom")
        query += " AND cupom = ?"
        params.append(cupom)
    if pdv:
        pdv = [_validar_pdv(p) for p in pdv]
        query += f" AND pdv IN ({','.join('?' * len(pdv))})"
        params.extend(pdv)
    if filter == "critical":
        query += " AND severidade = 'critical'"
    elif filter == "review":
        query += " AND status != 'resolved'"
    elif filter == "resolved":
        query += " AND status = 'resolved'"

    # Quando busca por cupom especÃ­fico: ordem cronolÃ³gica (ASC)
    # Quando lista todos os alertas: mais recente primeiro (DESC)
    order = "ASC" if cupom else "DESC"
    query += f" ORDER BY timestamp {order} LIMIT 200"
    rows = db.execute(query, params).fetchall()
    loja_full = db.execute("SELECT * FROM lojas WHERE id = ?", (loja_row["id"],)).fetchone()
    loja_api_token = loja_full["api_token"] if (loja_full and "api_token" in loja_full.keys()) else ""
    return [_evento_para_alerta(row, loja_token=loja_api_token) for row in rows]


@app.get("/api/v1/health")
def listar_health(
    loja: str,
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    loja_row = _loja_por_slug_autorizada(loja, usuario, db)

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
    _imagem_path(evento_id).write_bytes(_ler_upload(file, MAX_IMAGE_BYTES, {"image/jpeg", "image/png", "image/webp"}))
    # Marcar evento como tendo imagem para o frontend saber buscar
    db.execute("UPDATE auditoria_eventos SET imagem = 'bip.jpg' WHERE id = ?", (evento_id,))
    db.commit()
    return {"ok": True}


@app.get("/api/v1/events/{evento_id}/image")
def obter_imagem_evento(
    evento_id: int,
    token: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    # Aceita token da loja via query param OU JWT de usuario via header
    loja = None
    if token:
        loja = db.execute("SELECT id FROM lojas WHERE api_token = ?", (token,)).fetchone()
    if loja is None:
        # Fallback: tentar JWT de usuario
        try:
            auth_header = authorization or ""
            if not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Token ausente")
            usuario_id = _decode_token(auth_header[len("Bearer "):].strip())
            usuario = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (usuario_id,)).fetchone()
            if usuario is None:
                raise HTTPException(status_code=401, detail="Token invalido")
            _evento_autorizado(evento_id, usuario, db)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Token invalido")
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
    _video_path(evento_id).write_bytes(_ler_upload(file, MAX_VIDEO_BYTES, {"video/mp4", "video/quicktime"}))
    return {"ok": True}


@app.get("/api/v1/events/{evento_id}/video")
def obter_video_evento(
    evento_id: int,
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    _evento_autorizado(evento_id, usuario, db)
    caminho = _video_path(evento_id)
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Video nao encontrado")
    return FileResponse(caminho, media_type="video/mp4")


def _purchase_video_path(loja_id: int, pdv: str, cupom: str) -> Path:
    PURCHASE_VIDEOS_DIR.mkdir(exist_ok=True)
    pdv = _validar_pdv(pdv)
    cupom = _validar_safe_id(cupom, "Cupom")
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
    cupom = _validar_safe_id(cupom, "Cupom")
    pdv = _validar_pdv(pdv)
    _validar_datetime(start_time, "start_time")
    _validar_datetime(end_time, "end_time")
    loja_id = usuario["loja_id"]
    if not loja_id:
        # admin sem loja fixa â€” usa o slug enviado pelo dashboard
        if not loja:
            raise HTTPException(status_code=403, detail="Informe o parÃ¢metro loja")
        row = db.execute("SELECT id FROM lojas WHERE slug = ?", (loja,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Loja nÃ£o encontrada")
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
    cupom = _validar_safe_id(cupom, "Cupom")
    pdv = _validar_pdv(pdv)
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
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    row_loja = _loja_por_slug_autorizada(loja, usuario, db)
    cupom = _validar_safe_id(cupom, "Cupom")
    pdv = _validar_pdv(pdv)
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
    cupom = _validar_safe_id(cupom, "Cupom")
    pdv = _validar_pdv(pdv)
    if file is None:
        raise HTTPException(status_code=400, detail="Arquivo ausente")
    caminho = _purchase_video_path(loja["id"], pdv, cupom)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(_ler_upload(file, MAX_VIDEO_BYTES, {"video/mp4", "video/quicktime"}))
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
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    row = _loja_por_slug_autorizada(loja, usuario, db)
    caminho = _purchase_video_path(row["id"], pdv, cupom)
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Video nao encontrado")
    return FileResponse(caminho, media_type="video/mp4")


class DecisionIn(BaseModel):
    action: str = Field(pattern=r"^(save|ignore)$")
    observacao: Optional[str] = Field(default=None, max_length=500)


@app.post("/api/v1/alerts/{alerta_id}/decision")
def decidir_alerta(
    alerta_id: int,
    decisao: DecisionIn,
    usuario: sqlite3.Row = Depends(autenticar_usuario),
    db: sqlite3.Connection = Depends(get_db),
):
    _evento_autorizado(alerta_id, usuario, db)
    novo_status = "resolved" if decisao.action == "save" else "ignored"
    # Adicionar coluna observacao se nao existir
    try:
        db.execute("ALTER TABLE auditoria_eventos ADD COLUMN observacao TEXT")
        db.commit()
    except Exception:
        pass
    cursor = db.execute(
        "UPDATE auditoria_eventos SET status = ?, observacao = ? WHERE id = ?",
        (novo_status, decisao.observacao or "", alerta_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alerta nao encontrado")
    if decisao.action == "ignore":
        _imagem_path(alerta_id).unlink(missing_ok=True)
        _video_path(alerta_id).unlink(missing_ok=True)
    return {"ok": True, "status": novo_status}
