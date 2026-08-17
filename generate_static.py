"""Precompute dashboard aggregates from onpe.db into static_data.json.

The 2026 results are final, so the deployed app serves these precomputed
aggregates instead of needing a database. Re-run after re-scraping.
"""
import json
import sqlite3
from datetime import datetime, timezone

PROCESOS = ("EG2026", "SEP2026")

con = sqlite3.connect("onpe.db")
out = {"generated_at": datetime.now(timezone.utc).isoformat(), "procesos": {}}

for p in PROCESOS:
    total_mesas, electores, emitidos, validos, avg_part = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(total_electores),0),
                  COALESCE(SUM(total_votos_emitidos),0),
                  COALESCE(SUM(total_votos_validos),0),
                  COALESCE(AVG(pct_participacion),0)
           FROM mesas WHERE proceso=?""", (p,)).fetchone()
    contabilizadas = con.execute(
        "SELECT COUNT(*) FROM mesas WHERE proceso=? AND codigo_estado='C'", (p,)
    ).fetchone()[0]

    overview = {
        "proceso": p,
        "total_mesas": total_mesas,
        "total_electores": int(electores),
        "total_emitidos": int(emitidos),
        "total_validos": int(validos),
        "avg_participacion": round(float(avg_part), 2),
        "contabilizadas": contabilizadas,
        "pendientes": total_mesas - contabilizadas,
    }

    participation = [
        {"bucket": r[0], "count": r[1]} for r in con.execute(
            """SELECT CASE
                 WHEN pct_participacion < 50 THEN '0-50%'
                 WHEN pct_participacion < 60 THEN '50-60%'
                 WHEN pct_participacion < 70 THEN '60-70%'
                 WHEN pct_participacion < 80 THEN '70-80%'
                 WHEN pct_participacion < 90 THEN '80-90%'
                 ELSE '90-100%' END AS bucket, COUNT(*)
               FROM mesas WHERE proceso=? GROUP BY bucket ORDER BY bucket""", (p,))
    ]

    acta_status = [
        {"estado": r[0], "count": r[1]} for r in con.execute(
            """SELECT COALESCE(codigo_estado,'?'), COUNT(*)
               FROM mesas WHERE proceso=? GROUP BY 1 ORDER BY 2 DESC""", (p,))
    ]

    elecciones = [
        {"id": r[0], "nombre": r[1], "total_votos": int(r[2] or 0)} for r in con.execute(
            """SELECT id_eleccion, nombre_eleccion, SUM(votos)
               FROM resultados WHERE proceso=? AND es_partido=1
               GROUP BY id_eleccion, nombre_eleccion ORDER BY id_eleccion""", (p,))
    ]

    # pct = share of the election's valid party votes (matches ONPE's
    # official percentages), not the unweighted per-mesa average.
    parties = {}
    for eid, in con.execute(
            "SELECT DISTINCT id_eleccion FROM resultados WHERE proceso=?", (p,)):
        rows = con.execute(
            """SELECT nombre_partido, SUM(votos)
               FROM resultados
               WHERE proceso=? AND id_eleccion=? AND es_partido=1
               GROUP BY nombre_partido ORDER BY 2 DESC""", (p, eid)).fetchall()
        total_votos = sum(r[1] or 0 for r in rows) or 1
        parties[str(eid)] = [
            {"partido": r[0], "votos": int(r[1] or 0),
             "pct": round((r[1] or 0) / total_votos * 100, 3)}
            for r in rows
        ]

    out["procesos"][p] = {
        "overview": overview,
        "participation": participation,
        "acta_status": acta_status,
        "elecciones": elecciones,
        "parties": parties,
    }

with open("static_data.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)

import os
print(f"static_data.json: {os.path.getsize('static_data.json'):,} bytes")
for p in PROCESOS:
    o = out["procesos"][p]["overview"]
    print(f"{p}: {o['total_mesas']:,} mesas, {o['total_emitidos']:,} votos emitidos")
