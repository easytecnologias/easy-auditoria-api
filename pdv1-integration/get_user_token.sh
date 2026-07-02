#!/bin/bash
echo "=== Token do usuario Elishafan ==="
docker exec easy-auditoria-api python3 - <<'PYEOF'
import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
row = db.execute("SELECT email, token FROM usuarios WHERE email LIKE '%elish%' OR perfil='admin' LIMIT 1").fetchone()
if row:
    print("Email:", row[0])
    print("Token:", row[1])
else:
    print("Usuario nao encontrado")
PYEOF

echo ""
echo "=== Testando GET /api/v1/events/122/image com token do usuario ==="
USER_TOKEN=$(docker exec easy-auditoria-api python3 -c "
import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
row = db.execute(\"SELECT token FROM usuarios WHERE perfil='admin' LIMIT 1\").fetchone()
print(row[0] if row else '')
")
echo "Token: ${USER_TOKEN:0:20}..."
curl -sk "https://localhost:8099/api/v1/events/122/image" \
    -H "Authorization: Bearer $USER_TOKEN" \
    -o /tmp/test_img.jpg \
    -w "HTTP %{http_code} Size:%{size_download}\n"
echo ""
echo "Primeiros bytes:"
xxd /tmp/test_img.jpg 2>/dev/null | head -2 || echo "arquivo vazio"
