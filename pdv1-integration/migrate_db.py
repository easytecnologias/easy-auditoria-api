#!/usr/bin/env python3
import sqlite3
DB = "/data/easy-auditoria.db"
SCHEMA = "/app/schema.sql"
db = sqlite3.connect(DB)
db.executescript(open(SCHEMA).read())
db.commit()
print("ok")
