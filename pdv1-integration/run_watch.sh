#!/bin/bash
docker cp /tmp/watch_db.py easy-auditoria-api:/tmp/watch_db.py
docker exec easy-auditoria-api python3 /tmp/watch_db.py
