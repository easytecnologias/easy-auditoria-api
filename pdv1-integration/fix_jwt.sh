#!/bin/bash
echo "=== JWT_SECRET atual no container ==="
docker exec easy-auditoria-api env | grep JWT

echo ""
echo "=== Como container foi iniciado ==="
docker inspect easy-auditoria-api | python3 -c "
import sys, json
c = json.load(sys.stdin)[0]
env = c.get('Config',{}).get('Env',[])
for e in env:
    if 'JWT' in e or 'SECRET' in e or 'TOKEN' in e:
        print(e)
cmd = c.get('Config',{}).get('Cmd',[])
print('CMD:', cmd)
"

echo ""
echo "=== Salvando JWT_SECRET fixo ==="
JWT=$(docker exec easy-auditoria-api env | grep JWT_SECRET | cut -d= -f2)
echo "JWT_SECRET atual: $JWT"

# Adicionar JWT_SECRET fixo ao arquivo de ambiente do serviço
if [ -f /opt/pdv-visual-auditor/pdv-video-streamer.env ]; then
    echo "Arquivo env encontrado"
else
    echo "Sem arquivo env separado"
fi

echo ""
echo "=== Zerando alertas ==="
bash /tmp/reset_alerts.sh
