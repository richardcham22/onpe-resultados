"""Precompute second-round results per district into static/distritos_sep2026.json.

Joins per-mesa votes (onpe.db) with ONPE's ubigeo catalog
(ubigeo_catalog.json, from the dep-prov-distritos endpoint). Overseas
locations follow the same scheme: CONTINENTE \\ PAÍS \\ CIUDAD.
"""
import json
import os
import sqlite3

FP = "FUERZA POPULAR"
JPP = "JUNTOS POR EL PERÚ"

cat = json.load(open("ubigeo_catalog.json", encoding="utf-8"))
names = {}
for ubigeo, nombre in cat.items():
    parts = [p.strip() for p in nombre.split("\\")]
    if len(parts) == 3:
        names[int(ubigeo)] = parts

con = sqlite3.connect("onpe.db")

votes = {}
for ubi, partido, v in con.execute("""
        SELECT m.id_ubigeo, r.nombre_partido, SUM(r.votos)
        FROM resultados r JOIN mesas m
          ON m.codigo_mesa = r.codigo_mesa AND m.proceso = r.proceso
        WHERE r.proceso='SEP2026' AND r.id_eleccion=10 AND r.es_partido=1
        GROUP BY m.id_ubigeo, r.nombre_partido"""):
    votes.setdefault(ubi, {})[partido] = v

extra = {}
for ubi, mesas, emitidos in con.execute("""
        SELECT id_ubigeo, COUNT(*), COALESCE(SUM(total_votos_emitidos),0)
        FROM mesas WHERE proceso='SEP2026' GROUP BY id_ubigeo"""):
    extra[ubi] = (mesas, int(emitidos))

missing = [u for u in votes if u not in names]
assert not missing, f"ubigeos sin nombre: {missing[:10]}"

distritos = []
for ubi, v in sorted(votes.items()):
    dep, prov, dist = names[ubi]
    mesas, emitidos = extra.get(ubi, (0, 0))
    distritos.append({
        "u": str(ubi).zfill(6),
        "dep": dep, "prov": prov, "dist": dist,
        "fp": v.get(FP, 0), "jpp": v.get(JPP, 0),
        "mesas": mesas, "emitidos": emitidos,
    })

os.makedirs("static", exist_ok=True)
out = {"proceso": "SEP2026", "fp": FP, "jpp": JPP, "distritos": distritos}
with open("static/distritos_sep2026.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)

fp_w = sum(1 for d in distritos if d["fp"] > d["jpp"])
jpp_w = sum(1 for d in distritos if d["jpp"] > d["fp"])
print(f"{len(distritos)} distritos | FP gana {fp_w} | JPP gana {jpp_w} | "
      f"empates {len(distritos)-fp_w-jpp_w} | "
      f"{os.path.getsize('static/distritos_sep2026.json'):,} bytes")
