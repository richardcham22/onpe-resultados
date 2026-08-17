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


def _get_proceso():
    p = request.args.get("proceso", DEFAULT_PROCESO)
    return p if p in PROCESOS else DEFAULT_PROCESO


def onpe_get(path, params=None, proceso=DEFAULT_PROCESO):
    base = PROCESOS[proceso]
    resp = get_http().get(
        f"{base}/presentacion-backend/{path}", params=params,
        headers={"Accept": "application/json, text/plain, */*", "Referer": f"{base}/"},
        timeout=15,
    )
    text = resp.text.strip()
    if not text or not text.startswith("{"):
        raise ValueError("Respuesta vacía del servidor ONPE")
    return resp.json()


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stats")
def stats_page():
    return render_template("stats.html")


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
        for mesa in data["data"]:
            eid = mesa.get("idEleccion")
            nombre = ELECTION_NAMES.get(eid, f"Elección {eid}")
            if proceso == "SEP2026" and eid == 10:
                nombre = "Presidencial — 2da Vuelta"
            mesa["nombreEleccion"] = nombre
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Error al consultar ONPE: {e}"}), 500


# ── Scraper control ──────────────────────────────────────────────────────────

@app.route("/api/scraper/start", methods=["POST"])
def scraper_start():
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
        return jsonify(stats_overview(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/parties")
def api_parties():
    try:
        eid   = int(request.args.get("eleccion", 10))
        limit = int(request.args.get("limit", 30))
        return jsonify(stats_parties(eid, limit, _get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/participation")
def api_participation():
    try:
        return jsonify(stats_participation_buckets(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/acta_status")
def api_acta_status():
    try:
        return jsonify(stats_acta_status(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/elecciones")
def api_elecciones():
    try:
        return jsonify(stats_elecciones(_get_proceso()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bootstrap ────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
