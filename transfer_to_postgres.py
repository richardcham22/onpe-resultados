"""Copy the locally scraped official results (onpe.db) into a Postgres DB.

Usage:
    set DATABASE_URL first (or put DATABASE_URL=... in a .env file), then:
    python transfer_to_postgres.py [--skip-zeros] [--drop-live]

    --skip-zeros  omit party rows with 0 votes (44% of rows; national SUMs
                  are unchanged, per-party AVG %% shifts slightly)
    --drop-live   delete the old live-count data (proceso='LIVE2026') from
                  Postgres first to free space
"""
import csv
import io
import os
import sqlite3
import sys
import time

SKIP_ZEROS = "--skip-zeros" in sys.argv
DROP_LIVE = "--drop-live" in sys.argv
BATCH = 50_000

# Load .env if DATABASE_URL isn't set
if not os.environ.get("DATABASE_URL") and os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

url = os.environ.get("DATABASE_URL", "")
if not url.startswith(("postgres://", "postgresql://")):
    sys.exit("DATABASE_URL must point at Postgres (set env var or .env file)")

# Let db.py create schema / run migration on the target
import db  # noqa: E402  (reads DATABASE_URL)
db.init_db()

import psycopg2  # noqa: E402

pg = psycopg2.connect(url.replace("postgres://", "postgresql://", 1))
pg.autocommit = False
src = sqlite3.connect("onpe.db")

MESA_COLS = ("proceso", "codigo_mesa", "nombre_local", "centro_poblado",
             "id_ubigeo", "total_electores", "total_votos_emitidos",
             "total_votos_validos", "pct_participacion", "estado_acta",
             "codigo_estado", "scraped_at")
RES_COLS = ("proceso", "codigo_mesa", "id_eleccion", "nombre_eleccion",
            "codigo_partido", "nombre_partido", "votos", "pct_validos",
            "pct_emitidos", "es_partido")


def pg_size(cur):
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    return cur.fetchone()[0]


def copy_table(table, cols, select_sql):
    cur = pg.cursor()
    total = 0
    t0 = time.time()
    rows = src.execute(select_sql)
    while True:
        chunk = rows.fetchmany(BATCH)
        if not chunk:
            break
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerows(chunk)
        buf.seek(0)
        cur.copy_expert(
            f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv)", buf
        )
        total += len(chunk)
        pg.commit()
        rate = total / (time.time() - t0)
        print(f"  {table}: {total:,} rows ({rate:,.0f}/s)", flush=True)
    cur.close()
    return total


cur = pg.cursor()
print("Postgres size before:", pg_size(cur))

if DROP_LIVE:
    cur.execute("DELETE FROM resultados WHERE proceso='LIVE2026'")
    cur.execute("DELETE FROM mesas WHERE proceso='LIVE2026'")
    print(f"dropped LIVE2026 rows")

cur.execute("DELETE FROM resultados WHERE proceso IN ('EG2026','SEP2026')")
cur.execute("DELETE FROM mesas WHERE proceso IN ('EG2026','SEP2026')")
pg.commit()

copy_table("mesas", MESA_COLS,
           f"SELECT {', '.join(MESA_COLS)} FROM mesas WHERE proceso IN ('EG2026','SEP2026')")

res_where = "proceso IN ('EG2026','SEP2026')"
if SKIP_ZEROS:
    res_where += " AND (votos > 0 OR es_partido = 0)"
copy_table("resultados", RES_COLS,
           f"SELECT {', '.join(RES_COLS)} FROM resultados WHERE {res_where}")

print("Postgres size after:", pg_size(cur))
cur.execute("SELECT proceso, COUNT(*) FROM mesas GROUP BY proceso")
for r in cur.fetchall():
    print(" mesas:", r)
cur.close()
pg.close()
print("TRANSFER COMPLETO")
