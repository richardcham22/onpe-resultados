import json
import os
from flask import Flask, render_template, jsonify, request

from db import (
    init_db, get_session, ELECTION_NAMES,
    stats_overview, stats_parties,
    stats_participation_buckets, stats_acta_status, stats_elecciones,
)
import scraper
from scraper import PROCESOS, DEFAULT_PROCESO, get_http

app = Flask(__name__)

# Final official results precomputed by generate_static.py. When present,
# stats are served from this file and no database is needed (the results
# never change); the scraper endpoints are disabled.
STATIC_DATA = None
_static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_data.json")
if os.path.exists(_static_path):
    with open(_static_path, encoding="utf-8") as f:
        STATIC_DATA = json.load(f)


def _sd(proceso):
    return STATIC_DATA["procesos"].get(proceso, {})


# Per-mesa fallback shards (see generate_mesa_static.py). Used when ONPE's
# Cloudflare blocks the server's IP (always the case on cloud hosting).
_MESAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_mesas")
_parties_cache = {}


def _load_parties(proceso):
    if proceso not in _parties_cache:
        with open(os.path.join(_MESAS_DIR, proceso, "parties.json"), encoding="utf-8") as f:
            _parties_cache[proceso] = json.load(f)
    return _parties_cache[proceso]


def mesa_from_static(codigo, proceso):
    """Rebuild the ONPE API response shape from the bundled shards."""
    import gzip
    shard = os.path.join(_MESAS_DIR, proceso, f"{codigo[:3]}.json.gz")
    if not os.path.exists(shard):
        return None
    with gzip.open(shard, "rt", encoding="utf-8") as f:
        mesas = json.load(f)["mesas"]
    m = mesas.get(codigo)
    if not m:
        return None
    parties = _load_parties(proceso)
    vv, ve = m["vv"] or 0, m["ve"] or 0
    out = []
    for eid_str in sorted(m["v"], key=int):
        detalle = [{
            "adCodigo": p["c"],
            "adDescripcion": p["n"],
            "adGrafico": p["g"],
            "adVotos": v,
            "adPorcentajeVotosValidos": round(v / vv * 100, 3) if vv and p["g"] else 0,
            "adPorcentajeVotosEmitidos": round(v / ve * 100, 3) if ve else 0,
        } for p, v in zip(parties[eid_str], m["v"][eid_str])]
        out.append({
            "codigoMesa": codigo,
            "idEleccion": int(eid_str),
            "nombreLocalVotacion": m["l"],
            "centroPoblado": m["cp"],
            "totalElectoresHabiles": m["eh"],
            "totalVotosEmitidos": m["ve"],
            "totalVotosValidos": m["vv"],
            "porcentajeParticipacionCiudadana": m["pp"],
            "descripcionEstadoActa": m["ea"],
            "codigoEstadoActa": m["ce"],
            "detalle": detalle,
        })
    return out


def _get_proceso():
    p = request.args.get("proceso", DEFAULT_PROCESO)
    return p if p in PROCESOS else DEFAULT_PROCESO


def onpe_get(path, params=None, proceso=DEFAULT_PROCESO):
    base = PROCESOS[proceso]
    last_err = None
    for _ in range(2):
        try:
            resp = get_http().get(
                f"{base}/presentacion-backend/{path}", params=params,
                headers={"Accept": "application/json, text/plain, */*", "Referer": f"{base}/"},
                timeout=15,
            )
            text = resp.text.strip()
            if text.startswith("{"):
                return resp.json()
            last_err = ValueError("Respuesta vacía del servidor ONPE")
        except Exception as e:
            last_err = e
    raise last_err


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stats")
def stats_page():
    return render_template("stats.html")


@app.route("/distritos")
def distritos_page():
    return render_template("distritos.html")


# ── Mesa lookup ──────────────────────────────────────────────────────────────

@app.route("/api/mesa")
def get_mesa():
    codigo = request.args.get("codigoMesa", "").strip().zfill(6)
    proceso = _get_proceso()
    if not codigo or not codigo.isdigit():
        return jsonify({"error": "Código de mesa inválido"}), 400
    try:
        data = onpe_get("actas/buscar/mesa", {"codigoMesa": codigo}, proceso=proceso)
        if not data.get("success") or not data.get("data"):
            return jsonify({"error": "Mesa no encontrada o sin resultados"}), 404
        elections = data["data"]
        fuente = "onpe"
    except Exception as e:
        elections = mesa_from_static(codigo, proceso)
        if elections is None:
            return jsonify({"error": f"Mesa no encontrada (ONPE inaccesible: {e})"}), 404
        fuente = "local"
    for mesa in elections:
        eid = mesa.get("idEleccion")
        nombre = ELECTION_NAMES.get(eid, f"Elección {eid}")
        if proceso == "SEP2026" and eid == 10:
            nombre = "Presidencial — 2da Vuelta"
        mesa["nombreEleccion"] = nombre
    return jsonify({"success": True, "fuente": fuente, "data": elections})


# ── Scraper control ──────────────────────────────────────────────────────────

@app.route("/api/scraper/start", methods=["POST"])
def scraper_start():
    if STATIC_DATA:
        return jsonify({"error": "Scraper deshabilitado: resultados finales precargados"}), 410
    if scraper.is_running():
        return jsonify({"error": "El scraper ya está en ejecución"}), 409
    body = request.get_json(silent=True) or {}
    start = int(body.get("start", 1))
    end   = int(body.get("end",   89999))
    workers = int(body.get("workers", 20))
    proceso = body.get("proceso", DEFAULT_PROCESO)
    if proceso not in PROCESOS:
        return jsonify({"error": f"Proceso inválido: {proceso}"}), 400
    scraper.start(start=start, end=end, workers=workers, proceso=proceso)
    return jsonify({"ok": True, "start": start, "end": end, "workers": workers, "proceso": proceso})


@app.route("/api/scraper/stop", methods=["POST"])
def scraper_stop():
    scraper.stop()
    return jsonify({"ok": True})


@app.route("/api/scraper/status")
def scraper_status():
    if STATIC_DATA:
        return jsonify({
            "status": "static", "running": False, "proceso": None,
            "current_code": 0, "range_end": 0, "total_scanned": 0, "total_found": 0,
        })
    ov = stats_overview()
    running = scraper.is_running()
    state = ov["scraper"]
    # Heal stuck "running" status when the thread is no longer alive
    if not running and state.get("status") == "running":
        from db import engine, ScraperState
        from sqlalchemy.orm import Session as _Session
        from datetime import datetime
        with _Session(engine) as db:
            s = db.get(ScraperState, 1)
            if s:
                s.status = "stopped"
                s.updated_at = datetime.utcnow()
                db.commit()
        state["status"] = "stopped"
    return jsonify({**state, "running": running})


# ── Statistics ───────────────────────────────────────────────────────────────

@app.route("/api/stats/overview")
def api_overview():
    try:
        if STATIC_DATA:
            ov = dict(_sd(_get_proceso()).get("overview", {}))
            ov["scraper"] = {"status": "static", "proceso": None, "current_code": 0,
                             "range_end": 0, "total_scanned": 0, "total_found": 0}
            return jsonify(ov)
        return jsonify(stats_overview(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/parties")
def api_parties():
    try:
        eid   = int(request.args.get("eleccion", 10))
        limit = int(request.args.get("limit", 30))
        if STATIC_DATA:
            return jsonify(_sd(_get_proceso()).get("parties", {}).get(str(eid), [])[:limit])
        return jsonify(stats_parties(eid, limit, _get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/participation")
def api_participation():
    try:
        if STATIC_DATA:
            return jsonify(_sd(_get_proceso()).get("participation", []))
        return jsonify(stats_participation_buckets(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/acta_status")
def api_acta_status():
    try:
        if STATIC_DATA:
            return jsonify(_sd(_get_proceso()).get("acta_status", []))
        return jsonify(stats_acta_status(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/elecciones")
def api_elecciones():
    try:
        if STATIC_DATA:
            return jsonify(_sd(_get_proceso()).get("elecciones", []))
        return jsonify(stats_elecciones(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bootstrap ────────────────────────────────────────────────────────────────

if STATIC_DATA is None:
    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
