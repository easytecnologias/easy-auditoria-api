#!/bin/bash
echo "=== Verificando tabelas no container ==="
docker exec easy-auditoria-api python3 -c "
import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
tables = db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
for t in tables:
    name = t[0]
    count = db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
    print(name, ':', count, 'registros')
"

echo ""
echo "=== Zerando alertas/eventos ==="
docker exec easy-auditoria-api python3 -c "
import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
tables = db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
table_names = [t[0] for t in tables]
# Apagar tabelas de eventos/alertas (manter usuarios e pdvs)
for t in table_names:
    if t not in ('usuarios', 'pdvs', 'lojas', 'users', 'alembic_version'):
        deleted = db.execute('DELETE FROM ' + t).rowcount
        print('Apagado', deleted, 'registros de', t)
db.commit()
print('Feito.')
"

echo ""
echo "=== Reiniciando API ==="
docker restart easy-auditoria-api
echo "OK"
