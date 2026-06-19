CREATE TABLE IF NOT EXISTS lojas (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    nome TEXT NOT NULL,
    pdv_nome TEXT,
    api_token TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auditoria_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loja_id TEXT NOT NULL REFERENCES lojas(id),
    timestamp TEXT NOT NULL,
    pdv TEXT,
    cupom TEXT,
    imagem TEXT,
    produto TEXT,
    valor REAL,
    quantidade REAL,
    modo TEXT,
    resultado TEXT,
    confianca INTEGER,
    comparacao_pdv TEXT,
    possivel_divergencia TEXT,
    acao_recomendada TEXT,
    severidade TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_json TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (loja_id, timestamp, pdv, imagem)
);

CREATE INDEX IF NOT EXISTS idx_auditoria_eventos_loja
    ON auditoria_eventos (loja_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS pdv_health (
    loja_id TEXT NOT NULL REFERENCES lojas(id),
    pdv TEXT NOT NULL,
    bridge TEXT,
    imhdx TEXT,
    audit TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (loja_id, pdv)
);

CREATE TABLE IF NOT EXISTS pdv_sales (
    loja_id TEXT NOT NULL REFERENCES lojas(id),
    pdv TEXT NOT NULL,
    data TEXT NOT NULL,
    total REAL NOT NULL DEFAULT 0,
    cupons INTEGER NOT NULL DEFAULT 0,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (loja_id, pdv, data)
);

CREATE TABLE IF NOT EXISTS video_requests (
    loja_id TEXT NOT NULL,
    pdv TEXT NOT NULL,
    cupom TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    criado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (loja_id, pdv, cupom)
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    senha_hash TEXT NOT NULL,
    perfil TEXT NOT NULL CHECK (perfil IN ('admin', 'supervisor', 'operador')),
    loja_id TEXT REFERENCES lojas(id),
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
