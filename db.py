import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Text, Float, Boolean,
    DateTime, text, inspect as sa_inspect
)
from sqlalchemy.orm import DeclarativeBase, Session

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///onpe.db"
)
# Render provides postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


class Mesa(Base):
    __tablename__ = "mesas"
    id            = Column(Integer, primary_key=True)
    proceso       = Column(Text, nullable=False, default="EG2026")
    codigo_mesa   = Column(Text, nullable=False)
    nombre_local  = Column(Text)
    centro_poblado= Column(Text)
    id_ubigeo     = Column(Integer)
    total_electores      = Column(Integer)
    total_votos_emitidos = Column(Integer)
    total_votos_validos  = Column(Integer)
    pct_participacion    = Column(Float)
    estado_acta   = Column(Text)
    codigo_estado = Column(Text)
    scraped_at    = Column(DateTime, default=datetime.utcnow)


class Resultado(Base):
    __tablename__ = "resultados"
    id             = Column(Integer, primary_key=True)
    proceso        = Column(Text, nullable=False, default="EG2026")
    codigo_mesa    = Column(Text, nullable=False)
    id_eleccion    = Column(Integer, nullable=False)
    nombre_eleccion= Column(Text)
    codigo_partido = Column(Text)
    nombre_partido = Column(Text)
    votos          = Column(Integer, default=0)
    pct_validos    = Column(Float, default=0)
    pct_emitidos   = Column(Float, default=0)
    es_partido     = Column(Boolean, default=True)


class ScraperState(Base):
    __tablename__ = "scraper_state"
    id             = Column(Integer, primary_key=True, default=1)
    status         = Column(Text, default="idle")
    proceso        = Column(Text, default="EG2026")
    current_code   = Column(Integer, default=0)
    range_start    = Column(Integer, default=1)
    range_end      = Column(Integer, default=89999)
    total_scanned  = Column(Integer, default=0)
    total_found    = Column(Integer, default=0)
    started_at     = Column(DateTime)
    updated_at     = Column(DateTime)


ELECTION_NAMES = {
    10: "Presidencial",
    12: "Parlamento Andino",
    13: "Senadores DEU",
    14: "Senadores DEM",
    15: "Senadores 33",
    20: "Diputados",
}

PROCESO_NAMES = {
    "EG2026":  "Primera Vuelta — 12 abr 2026",
    "SEP2026": "Segunda Vuelta — 7 jun 2026",
}


def _migrate_add_proceso():
    """One-time migration: older DBs (live-count era) lack the proceso column."""
    insp = sa_inspect(engine)
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        for table in ("mesas", "resultados"):
            if not insp.has_table(table):
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if "proceso" in cols:
                continue
            if is_sqlite:
                # SQLite can't drop the old UNIQUE(codigo_mesa) constraint in
                # place; keep the stale live-count data aside and start fresh.
                conn.execute(text(f"ALTER TABLE {table} RENAME TO {table}_legacy"))
            else:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN proceso TEXT NOT NULL DEFAULT 'LIVE2026'"
                ))
                if table == "mesas":
                    conn.execute(text(
                        "ALTER TABLE mesas DROP CONSTRAINT IF EXISTS mesas_codigo_mesa_key"
                    ))
                else:
                    conn.execute(text(
                        "ALTER TABLE resultados DROP CONSTRAINT IF EXISTS "
                        "resultados_codigo_mesa_id_eleccion_codigo_partido_key"
                    ))
        if insp.has_table("scraper_state"):
            cols = [c["name"] for c in insp.get_columns("scraper_state")]
            if "proceso" not in cols:
                conn.execute(text(
                    "ALTER TABLE scraper_state ADD COLUMN proceso TEXT DEFAULT 'EG2026'"
                ))


def _ensure_indexes():
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mesas_proceso_codigo "
            "ON mesas (proceso, codigo_mesa)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_resultados_proc_mesa_elec_part "
            "ON resultados (proceso, codigo_mesa, id_eleccion, codigo_partido)"
        ))


def init_db():
    _migrate_add_proceso()
    Base.metadata.create_all(engine)
    _ensure_indexes()
    with Session(engine) as s:
        if not s.get(ScraperState, 1):
            s.add(ScraperState(id=1))
            s.commit()


def get_session():
    return Session(engine)


# ── Statistics queries ──────────────────────────────────────────────────────

def stats_overview(proceso="EG2026"):
    with get_session() as s:
        p = {"p": proceso}
        total_mesas   = s.execute(text("SELECT COUNT(*) FROM mesas WHERE proceso=:p"), p).scalar()
        total_electores = s.execute(text("SELECT COALESCE(SUM(total_electores),0) FROM mesas WHERE proceso=:p"), p).scalar()
        total_emitidos  = s.execute(text("SELECT COALESCE(SUM(total_votos_emitidos),0) FROM mesas WHERE proceso=:p"), p).scalar()
        total_validos   = s.execute(text("SELECT COALESCE(SUM(total_votos_validos),0) FROM mesas WHERE proceso=:p"), p).scalar()
        avg_part = s.execute(text("SELECT COALESCE(AVG(pct_participacion),0) FROM mesas WHERE proceso=:p"), p).scalar()
        contabilizadas = s.execute(text("SELECT COUNT(*) FROM mesas WHERE proceso=:p AND codigo_estado='C'"), p).scalar()
        pendientes     = s.execute(text("SELECT COUNT(*) FROM mesas WHERE proceso=:p AND (codigo_estado!='C' OR codigo_estado IS NULL)"), p).scalar()
        state = s.get(ScraperState, 1)
        return {
            "proceso": proceso,
            "total_mesas": total_mesas,
            "total_electores": int(total_electores or 0),
            "total_emitidos": int(total_emitidos or 0),
            "total_validos": int(total_validos or 0),
            "avg_participacion": round(float(avg_part or 0), 2),
            "contabilizadas": contabilizadas,
            "pendientes": pendientes,
            "scraper": {
                "status": state.status if state else "idle",
                "proceso": (state.proceso if state and state.proceso else "EG2026"),
                "current_code": state.current_code if state else 0,
                "range_end": state.range_end if state else 89999,
                "total_scanned": state.total_scanned if state else 0,
                "total_found": state.total_found if state else 0,
            }
        }


def stats_parties(id_eleccion=10, limit=30, proceso="EG2026"):
    with get_session() as s:
        rows = s.execute(text("""
            SELECT nombre_partido, SUM(votos) as total, AVG(pct_validos) as pct
            FROM resultados
            WHERE proceso=:p AND id_eleccion=:e AND es_partido=TRUE
            GROUP BY nombre_partido
            ORDER BY total DESC
            LIMIT :l
        """), {"p": proceso, "e": id_eleccion, "l": limit}).fetchall()
        return [{"partido": r[0], "votos": int(r[1] or 0), "pct": round(float(r[2] or 0), 2)} for r in rows]


def stats_participation_buckets(proceso="EG2026"):
    with get_session() as s:
        rows = s.execute(text("""
            SELECT
              CASE
                WHEN pct_participacion < 50 THEN '0-50%'
                WHEN pct_participacion < 60 THEN '50-60%'
                WHEN pct_participacion < 70 THEN '60-70%'
                WHEN pct_participacion < 80 THEN '70-80%'
                WHEN pct_participacion < 90 THEN '80-90%'
                ELSE '90-100%'
              END as bucket,
              COUNT(*) as cnt
            FROM mesas
            WHERE proceso=:p
            GROUP BY bucket
            ORDER BY bucket
        """), {"p": proceso}).fetchall()
        return [{"bucket": r[0], "count": int(r[1])} for r in rows]


def stats_acta_status(proceso="EG2026"):
    with get_session() as s:
        rows = s.execute(text("""
            SELECT COALESCE(codigo_estado,'?') as estado, COUNT(*) as cnt
            FROM mesas WHERE proceso=:p GROUP BY estado ORDER BY cnt DESC
        """), {"p": proceso}).fetchall()
        return [{"estado": r[0], "count": int(r[1])} for r in rows]


def stats_elecciones(proceso="EG2026"):
    with get_session() as s:
        rows = s.execute(text("""
            SELECT id_eleccion, nombre_eleccion, SUM(votos) as total
            FROM resultados WHERE proceso=:p AND es_partido=TRUE
            GROUP BY id_eleccion, nombre_eleccion ORDER BY id_eleccion
        """), {"p": proceso}).fetchall()
        return [{"id": r[0], "nombre": r[1], "total_votos": int(r[2] or 0)} for r in rows]
