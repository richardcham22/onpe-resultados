"""Pack per-mesa results from onpe.db into compact gzipped shards.

ONPE's Cloudflare blocks requests from datacenter IPs, so the deployed app
can't proxy live lookups. Every mesa has an identical party matrix per
election, so votes are stored as int arrays aligned to one canonical party
list per (proceso, eleccion). Output: static_mesas/<proceso>/parties.json
and <shard>.json.gz where shard = first 3 digits of the mesa code.
"""
import gzip
import json
import os
import sqlite3
from collections import defaultdict

OUT = "static_mesas"
PROCESOS = ("EG2026", "SEP2026")

con = sqlite3.connect("onpe.db")

for proceso in PROCESOS:
    outdir = os.path.join(OUT, proceso)
    os.makedirs(outdir, exist_ok=True)

    # Canonical party order per election
    parties = {}
    for eid, in con.execute(
            "SELECT DISTINCT id_eleccion FROM resultados WHERE proceso=? ORDER BY id_eleccion",
            (proceso,)):
        parties[str(eid)] = [
            {"c": r[0], "n": r[1], "g": r[2]}
            for r in con.execute(
                """SELECT DISTINCT codigo_partido, nombre_partido, es_partido
                   FROM resultados WHERE proceso=? AND id_eleccion=?
                   ORDER BY codigo_partido""", (proceso, eid))
        ]
    with open(os.path.join(outdir, "parties.json"), "w", encoding="utf-8") as f:
        json.dump(parties, f, ensure_ascii=False)

    # Mesa metadata
    meta = {}
    for r in con.execute(
            """SELECT codigo_mesa, nombre_local, centro_poblado, total_electores,
                      total_votos_emitidos, total_votos_validos, pct_participacion,
                      estado_acta, codigo_estado
               FROM mesas WHERE proceso=?""", (proceso,)):
        meta[r[0]] = {"l": r[1], "cp": r[2], "eh": r[3], "ve": r[4], "vv": r[5],
                      "pp": r[6], "ea": r[7], "ce": r[8], "v": {}}

    # Vote arrays, aligned to the canonical order (same ORDER BY)
    for codigo, eid, votos in con.execute(
            """SELECT codigo_mesa, id_eleccion, votos
               FROM resultados WHERE proceso=?
               ORDER BY codigo_mesa, id_eleccion, codigo_partido""", (proceso,)):
        meta[codigo]["v"].setdefault(str(eid), []).append(votos)

    # Sanity: every mesa has full arrays
    for codigo, m in meta.items():
        for eid, arr in m["v"].items():
            assert len(arr) == len(parties[eid]), (proceso, codigo, eid)

    shards = defaultdict(dict)
    for codigo, m in meta.items():
        shards[codigo[:3]][codigo] = m

    total_bytes = 0
    for prefix, mesas in sorted(shards.items()):
        path = os.path.join(outdir, f"{prefix}.json.gz")
        payload = json.dumps({"mesas": mesas}, ensure_ascii=False).encode("utf-8")
        with gzip.open(path, "wb", compresslevel=9) as f:
            f.write(payload)
        total_bytes += os.path.getsize(path)
    print(f"{proceso}: {len(meta):,} mesas en {len(shards)} shards, "
          f"{total_bytes/1e6:.1f} MB comprimidos")
