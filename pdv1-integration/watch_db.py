import sqlite3
db = sqlite3.connect('/data/easy-auditoria.db')
total = db.execute("SELECT COUNT(*) FROM auditoria_eventos").fetchone()[0]
print("Total alertas:", total)
rows = db.execute(
    "SELECT timestamp, pdv, cupom, produto, resultado, confianca, imagem FROM auditoria_eventos ORDER BY id DESC LIMIT 20"
).fetchall()
print("\n%-20s %-8s %-8s %-30s %-25s %s  %s" % ("Horario","PDV","Cupom","Produto","Resultado","Conf","Imagem"))
print("-"*130)
for r in rows:
    img = "SIM" if r[6] else "NAO"
    print("%-20s %-8s %-8s %-30s %-25s %s%%  %s" % (
        str(r[0])[:19], str(r[1]), str(r[2]), str(r[3])[:28], str(r[4]), r[5], img
    ))

print("\n=== DIVERGENCIAS com imagem ===")
div = db.execute(
    "SELECT id, produto, imagem FROM auditoria_eventos WHERE resultado='DIVERGENCIA_CATEGORIA' ORDER BY id DESC LIMIT 5"
).fetchall()
for r in div:
    print("  id=%s produto=%s imagem=%s" % (r[0], str(r[1])[:30], repr(r[2])))
