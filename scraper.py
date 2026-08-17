import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy import insert as sa_insert
from sqlalchemy.orm import Session
from curl_cffi import requests as req

from db import engine, Mesa, Resultado, ScraperState, ELECTION_NAMES

# Official ONPE historical results portals (post-election, final data).
# The live-count site (resultadoelectoral.onpe.gob.pe) was taken down after
# the elections; these portals expose the same presentacion-backend API.
# They sit behind Cloudflare TLS fingerprinting, hence curl_cffi with
# Chrome impersonation instead of plain requests.
PROCESOS = {
    "EG2026":  "https://resultadohistorico-eg2026.onpe.gob.pe",   # Primera vuelta (12 abr 2026)
    "SEP2026": "https://resultadohistorico-sep2026.onpe.gob.pe",  # Segunda vuelta (7 jun 2026)
}
DEFAULT_PROCESO = "EG2026"

_stop_event = threading.Event()
_scraper_thread = None
_tls = threading.local()


def get_http():
    """Thread-local Chrome-impersonating session."""
    if not hasattr(_tls, "session"):
        _tls.session = req.Session(impersonate="chrome")
    return _tls.session


def _headers(base):
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{base}/",
    }


def fetch_mesa(codigo: str, proceso: str = DEFAULT_PROCESO):
    """Returns list of election dicts for this mesa, or None if not found.

    A nonexistent mesa returns HTTP 204 (definitive). Anything else that
    isn't valid JSON is treated as transient (WAF challenge, timeout) and
    retried, so blocks don't silently register mesas as missing.
    """
    base = PROCESOS[proceso]
    for attempt in range(3):
        try:
            resp = get_http().get(
                f"{base}/presentacion-backend/actas/buscar/mesa",
                params={"codigoMesa": codigo},
                headers=_headers(base),
                timeout=12,
            )
            if resp.status_code == 204:
                return None
            if resp.status_code == 200:
                text = resp.text.strip()
                if text.startswith("{"):
                    return resp.json().get("data") or None
        except Exception:
            pass
        time.sleep(1 + attempt * 2)
    return None


def _save_mesa(db: Session, elections: list, proceso: str):
    """Upsert mesa + resultados rows from API response."""
    first = elections[0]
    codigo = first["codigoMesa"]

    mesa = db.query(Mesa).filter_by(codigo_mesa=codigo, proceso=proceso).first()
    if not mesa:
        mesa = Mesa(codigo_mesa=codigo, proceso=proceso)
        db.add(mesa)

    mesa.nombre_local        = first.get("nombreLocalVotacion")
    mesa.centro_poblado      = first.get("centroPoblado")
    mesa.id_ubigeo           = first.get("idUbigeo")
    mesa.total_electores     = first.get("totalElectoresHabiles")
    mesa.total_votos_emitidos= first.get("totalVotosEmitidos")
    mesa.total_votos_validos = first.get("totalVotosValidos")
    mesa.pct_participacion   = first.get("porcentajeParticipacionCiudadana")
    mesa.estado_acta         = first.get("descripcionEstadoActa")
    mesa.codigo_estado       = first.get("codigoEstadoActa")
    mesa.scraped_at          = datetime.utcnow()

    # Delete old resultados for this mesa (clean upsert)
    db.query(Resultado).filter_by(codigo_mesa=codigo, proceso=proceso).delete()

    rows = []
    for election in elections:
        eid   = election.get("idEleccion")
        ename = ELECTION_NAMES.get(eid, f"Elección {eid}")
        for p in election.get("detalle", []):
            # In SEP2026 ONPE marks VOTOS IMPUGNADOS with adGrafico=1;
            # exclude all "VOTOS ..." rows from party rankings regardless.
            descripcion = (p.get("adDescripcion") or "").strip()
            es_partido = bool(p.get("adGrafico") == 1) and not descripcion.upper().startswith("VOTOS")
            rows.append(dict(
                proceso        = proceso,
                codigo_mesa    = codigo,
                id_eleccion    = eid,
                nombre_eleccion= ename,
                codigo_partido = p.get("adCodigo"),
                nombre_partido = descripcion,
                votos          = p.get("adVotos") or 0,
                pct_validos    = p.get("adPorcentajeVotosValidos") or 0,
                pct_emitidos   = p.get("adPorcentajeVotosEmitidos") or 0,
                es_partido     = es_partido,
            ))
    if rows:
        db.execute(sa_insert(Resultado), rows)
    db.commit()


def _update_state(db: Session, **kwargs):
    state = db.get(ScraperState, 1)
    for k, v in kwargs.items():
        setattr(state, k, v)
    state.updated_at = datetime.utcnow()
    db.commit()


def _run_scraper(start: int, end: int, workers: int, proceso: str):
    with Session(engine) as db:
        _update_state(db, status="running", proceso=proceso,
                      range_start=start, range_end=end,
                      started_at=datetime.utcnow(), total_scanned=0, total_found=0)

    batch_size = workers * 4
    scanned = 0
    found = 0

    try:
        codes = range(start, end + 1)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            it = iter(codes)
            while not _stop_event.is_set():
                batch = []
                for _ in range(batch_size):
                    c = next(it, None)
                    if c is None:
                        break
                    batch.append(str(c).zfill(6))
                if not batch:
                    break

                futures = {pool.submit(fetch_mesa, c, proceso): c for c in batch}
                for future in as_completed(futures):
                    if _stop_event.is_set():
                        break
                    futures[future]
                    scanned += 1
                    result = future.result()
                    if result:
                        found += 1
                        try:
                            with Session(engine) as db:
                                _save_mesa(db, result, proceso)
                        except Exception:
                            pass

                if scanned % 500 == 0:
                    with Session(engine) as db:
                        _update_state(db,
                            current_code=int(batch[-1]),
                            total_scanned=scanned,
                            total_found=found,
                        )

        status = "stopped" if _stop_event.is_set() else "done"
    except Exception:
        status = "stopped"
    finally:
        with Session(engine) as db:
            _update_state(db, status=status, total_scanned=scanned, total_found=found)


def start(start=1, end=89999, workers=20, proceso=DEFAULT_PROCESO):
    global _scraper_thread
    if proceso not in PROCESOS:
        raise ValueError(f"Proceso inválido: {proceso}")
    _stop_event.clear()
    _scraper_thread = threading.Thread(
        target=_run_scraper, args=(start, end, workers, proceso), daemon=True
    )
    _scraper_thread.start()
    return True


def stop():
    _stop_event.set()
    return True


def is_running():
    return _scraper_thread is not None and _scraper_thread.is_alive()
