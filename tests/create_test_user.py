#!/usr/bin/env python3
import sqlite3, bcrypt
DB = "/data/easy-auditoria.db"
db = sqlite3.connect(DB)
h = bcrypt.hashpw(b"teste@1234", bcrypt.gensalt()).decode()
db.execute(
    "INSERT OR REPLACE INTO usuarios(nome,email,senha_hash,perfil,loja_id,ativo) VALUES(?,?,?,?,?,1)",
    ("Teste Seg", "teste.sec@easy.local", h, "operador", "loja-106")
)
db.commit()
print("usuario teste criado: teste.sec@easy.local / teste@1234")
