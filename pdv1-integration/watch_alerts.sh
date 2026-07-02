#!/bin/bash
echo "=== Alertas no banco (últimos 20) ==="
docker exec easy-auditoria-api python3 - <<'PYEOF'
import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
rows = db.execute(
    "SELECT timestamp, pdv, cupom, produto, resultado, confianca FROM auditoria_eventos ORDER BY id DESC LIMIT 20"
).fetchall()
total = db.execute("SELECT COUNT(*) FROM auditoria_eventos").fetchone()[0]
print("Total de alertas: %d\n" % total)
print("%-20s %-8s %-10s %-30s %-25s %s" % ("Horário","PDV","Cupom","Produto","Resultado","Conf"))
print("-"*110)
for r in rows:
    print("%-20s %-8s %-10s %-30s %-25s %s%%" % (
        str(r[0])[:19], str(r[1]), str(r[2]), str(r[3])[:28], str(r[4]), r[5]
    ))
PYEOF
