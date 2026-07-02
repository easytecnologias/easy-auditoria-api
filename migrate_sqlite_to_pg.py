#!/usr/bin/env python3
"""
Migra os dados do banco SQLite antigo (easy-auditoria.db) para o novo PostgreSQL.
Execução única, depois que o container PostgreSQL já estiver rodando com o schema_pg.sql aplicado.

Uso:
    python3 migrate_sqlite_to_pg.py /caminho/easy-auditoria.db "postgresql://easy:senha@host:5432/easy_auditoria"
"""
import sqlite3
import sys

import psycopg2
import psycopg2.extras


def migrar(sqlite_path: str, pg_dsn: str) -> None:
    sconn = sqlite3.connect(sqlite_path)
    sconn.row_factory = sqlite3.Row
    pconn = psycopg2.connect(pg_dsn)
    pcur = pconn.cursor()

    def copiar(tabela: str, colunas: list[str]):
        rows = sconn.execute(f"SELECT {', '.join(colunas)} FROM {tabela}").fetchall()
        if not rows:
            print(f"  {tabela}: 0 registros (nada a migrar)")
            return
        placeholders = ", ".join(["%s"] * len(colunas))
        cols_sql = ", ".join(colunas)
        sql = f"INSERT INTO {tabela} ({cols_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        n = 0
        for row in rows:
            valores = [row[c] for c in colunas]
            try:
                pcur.execute(sql, valores)
                n += 1
            except Exception as e:
                print(f"    [erro] {tabela} linha ignorada: {e}")
                pconn.rollback()
                continue
        pconn.commit()
        print(f"  {tabela}: {n}/{len(rows)} registros migrados")

    print("Migrando lojas...")
    copiar("lojas", ["id", "slug", "nome", "pdv_nome", "api_token", "criado_em"])

    print("Migrando usuarios...")
    copiar("usuarios", ["id", "nome", "email", "senha_hash", "perfil", "loja_id", "ativo", "criado_em"])

    print("Migrando auditoria_eventos...")
    colunas_eventos = [
        "id", "loja_id", "timestamp", "pdv", "cupom", "imagem", "produto", "valor",
        "quantidade", "modo", "resultado", "confianca", "comparacao_pdv",
        "possivel_divergencia", "acao_recomendada", "severidade", "status",
        "raw_json", "criado_em",
    ]
    # observacao pode não existir na tabela antiga se nunca foi adicionada dinamicamente
    cols_existentes = {r[1] for r in sconn.execute("PRAGMA table_info(auditoria_eventos)").fetchall()}
    if "observacao" in cols_existentes:
        colunas_eventos.append("observacao")
    copiar("auditoria_eventos", colunas_eventos)

    print("Migrando pdv_health...")
    copiar("pdv_health", ["loja_id", "pdv", "bridge", "imhdx", "audit", "atualizado_em"])

    print("Migrando pdv_sales...")
    copiar("pdv_sales", ["loja_id", "pdv", "data", "total", "cupons", "atualizado_em"])

    print("Migrando video_requests...")
    copiar("video_requests", ["loja_id", "pdv", "cupom", "start_time", "end_time", "status", "criado_em"])

    # Ajustar sequence do BIGSERIAL para não colidir com IDs já migrados
    for tabela, coluna in [("usuarios", "id"), ("auditoria_eventos", "id")]:
        pcur.execute(f"SELECT setval(pg_get_serial_sequence('{tabela}', '{coluna}'), COALESCE((SELECT MAX({coluna}) FROM {tabela}), 1))")
    pconn.commit()

    sconn.close()
    pcur.close()
    pconn.close()
    print("\nMigração concluída.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    migrar(sys.argv[1], sys.argv[2])
