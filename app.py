from __future__ import annotations

import html
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st
from werkzeug.security import check_password_hash, generate_password_hash


def get_config_value(name: str, default: str = "") -> str:
    """Lee configuración desde variables de entorno o desde Streamlit Secrets."""
    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    try:
        value = st.secrets.get(name, default)
    except Exception:
        # En ejecución local puede no existir el archivo .streamlit/secrets.toml.
        value = default

    return str(value if value is not None else default).strip()


# ============================================================
# CONFIGURACIÓN
# ============================================================
DB_PATH = get_config_value("FANTASY_DB_PATH", "fantasy.db")
ADMIN_USERNAME = "usuariomaestro"
ADMIN_PASSWORD_ENV = "FANTASY_ADMIN_PASSWORD"
VOTING_OPENS_AFTER_HOURS = 5
VOTING_CLOSES_AFTER_HOURS = 48
MAX_SQUAD_SIZE = 5
MAX_STARTERS = 4
MIN_PLAYER_PRICE = Decimal("0.50")
MAX_PLAYER_PRICE = Decimal("1000000.00")  # Límite de seguridad; ajustable a las reglas del juego.
MONEY_CENT = Decimal("0.01")
MARKET_UPDATE_INTERVAL_DAYS = 3
MARKET_UPDATE_KEY = "automatic"
VALID_POSITIONS = ("POR", "DEF", "MED", "DEL")

try:
    MADRID_TZ = ZoneInfo("Europe/Madrid")
except ZoneInfoNotFoundError:
    # En Windows, instala la dependencia `tzdata` incluida en requirements.txt.
    MADRID_TZ = None


# ============================================================
# 1. CONEXIONES, TRANSACCIONES Y MIGRACIONES
# ============================================================
def get_connection() -> sqlite3.Connection:
    """Abre una conexión con claves foráneas y espera ante bloqueos breves."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db_transaction():
    """Transacción de escritura atómica; revierte todos los cambios si falla."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def run_query(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Ejecuta una consulta simple y devuelve sus filas, si las hay."""
    conn = get_connection()
    try:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_dataframe(query: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_players_price_floor() -> None:
    """Quita una antigua restricción price >= 1 para permitir precios de 0,50.

    SQLite no permite modificar directamente un CHECK existente. La reconstrucción
    conserva IDs y datos y mantiene las referencias de las tablas relacionadas.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='players'"
        ).fetchone()
        if not table or not table[0]:
            return

        table_sql = str(table[0])
        has_old_floor = bool(
            re.search(r"CHECK\s*\(\s*price\s*>=\s*1(?:\.0+)?\s*\)", table_sql, re.IGNORECASE)
        )
        if not has_old_floor:
            return

        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()
        }
        required_columns = {"id", "name", "position", "price", "points"}
        if not required_columns.issubset(existing_columns):
            raise RuntimeError(
                "No se puede migrar el precio de players: faltan columnas esperadas."
            )

        dependent_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE tbl_name='players' AND type IN ('index', 'trigger') AND sql IS NOT NULL"
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE players RENAME TO players_old_price_migration")
        conn.execute(
            "CREATE TABLE players ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "position TEXT NOT NULL, "
            "price REAL NOT NULL DEFAULT 5 CHECK(price >= 0.5), "
            "points REAL NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO players(id, name, position, price, points) "
            "SELECT id, name, position, price, points FROM players_old_price_migration"
        )
        conn.execute("DROP TABLE players_old_price_migration")
        for (sql_statement,) in dependent_sql:
            conn.execute(sql_statement)
        conn.commit()

        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(
                "La migración de precios ha detectado referencias de base de datos inválidas."
            )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.close()


def _to_money(value: Any) -> Decimal:
    """Convierte un importe a dos decimales, evitando operaciones con texto inválido."""
    try:
        amount = Decimal(str(value)).quantize(MONEY_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("El importe indicado no es válido.") from exc
    if not amount.is_finite():
        raise ValueError("El importe debe ser un número finito.")
    return amount


def format_rupias(value: Any) -> str:
    """Formatea rupias con coma decimal y sin decimales innecesarios."""
    amount = _to_money(value)
    text = f"{amount:,.2f}"
    text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    if text.endswith(",00"):
        text = text[:-3]
    elif text.endswith("0"):
        text = text[:-1]
    return text


def init_db() -> None:
    """Crea el esquema y migra las instalaciones anteriores sin borrar datos."""
    admin_password = get_config_value(ADMIN_PASSWORD_ENV)
    if admin_password and len(admin_password) < 12:
        raise ValueError(
            f"{ADMIN_PASSWORD_ENV} debe tener al menos 12 caracteres."
        )

    _migrate_players_price_floor()

    with db_transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                rupias REAL NOT NULL DEFAULT 20 CHECK(rupias >= 0),
                total_points REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                position TEXT NOT NULL,
                price REAL NOT NULL DEFAULT 5 CHECK(price >= 0.5),
                points REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_squads (
                user_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                is_starter INTEGER NOT NULL DEFAULT 0 CHECK(is_starter IN (0, 1)),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE RESTRICT,
                UNIQUE(user_id, player_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jornada TEXT NOT NULL DEFAULT 'Sin jornada',
                rival TEXT NOT NULL DEFAULT '',
                match_date TEXT NOT NULL DEFAULT '',
                match_time TEXT NOT NULL DEFAULT '00:00',
                status TEXT NOT NULL DEFAULT 'PENDIENTE'
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE RESTRICT,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
                UNIQUE(user_id, player_id, match_id)
            )"""
        )
        # player_id se añadió para las nuevas entradas. Las entradas antiguas
        # tienen player_id NULL y no permiten una reversión exacta por jugador.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points_awarded REAL NOT NULL,
                player_id INTEGER,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE RESTRICT
            )"""
        )
        # Registro auditable para borrar/restaurar solo los puntos concedidos a managers.
        # Los puntos individuales de los jugadores permanecen intactos.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS manager_points_reset_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                reset_at TEXT NOT NULL,
                reset_by TEXT NOT NULL DEFAULT 'usuariomaestro',
                status TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK(status IN ('ACTIVE', 'RESTORED', 'INVALIDATED')),
                managers_affected INTEGER NOT NULL DEFAULT 0,
                history_rows INTEGER NOT NULL DEFAULT 0,
                points_removed REAL NOT NULL DEFAULT 0,
                restored_at TEXT,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS manager_points_reset_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reset_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                player_id INTEGER,
                points_awarded REAL NOT NULL,
                FOREIGN KEY(reset_id) REFERENCES manager_points_reset_logs(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE SET NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_player_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                points_awarded REAL NOT NULL,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE RESTRICT,
                UNIQUE(match_id, player_id)
            )"""
        )
        # Fotografía de las plantillas en el inicio del partido.
        # Se captura al procesar la jornada, pero representa el estado del kickoff
        # porque el mercado y las plantillas quedan bloqueados desde ese instante.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_snapshot_meta (
                match_id INTEGER PRIMARY KEY,
                kickoff_at TEXT NOT NULL,
                captured_at TEXT,
                managers_count INTEGER NOT NULL DEFAULT 0,
                snapshot_status TEXT NOT NULL DEFAULT 'CAPTURED',
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_user_snapshot (
                match_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                starter_count INTEGER NOT NULL DEFAULT 0,
                eligible INTEGER NOT NULL DEFAULT 0 CHECK(eligible IN (0, 1)),
                PRIMARY KEY(match_id, user_id),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_squad_snapshot (
                match_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                is_starter INTEGER NOT NULL DEFAULT 0 CHECK(is_starter IN (0, 1)),
                PRIMARY KEY(match_id, user_id, player_id),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE RESTRICT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS market_updates (
                update_key TEXT PRIMARY KEY,
                executed_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS market_price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                price REAL NOT NULL,
                owners_count INTEGER NOT NULL DEFAULT 0,
                managers_total INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'market',
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )"""
        )

        # Migraciones para bases de datos ya existentes.
        migrations = {
            "users": {
                "username": "TEXT",
                "password": "TEXT",
                "role": "TEXT DEFAULT 'user'",
                "rupias": "REAL DEFAULT 20",
                "total_points": "REAL DEFAULT 0",
            },
            "players": {
                "name": "TEXT",
                "position": "TEXT DEFAULT 'MED'",
                "price": "REAL DEFAULT 5",
                "points": "REAL DEFAULT 0",
            },
            "user_squads": {
                "user_id": "INTEGER",
                "player_id": "INTEGER",
                "is_starter": "INTEGER DEFAULT 0",
            },
            "matches": {
                "jornada": "TEXT DEFAULT 'Sin jornada'",
                "rival": "TEXT DEFAULT ''",
                "match_date": "TEXT DEFAULT ''",
                "match_time": "TEXT DEFAULT '00:00'",
                "status": "TEXT DEFAULT 'PENDIENTE'",
            },
            "ratings": {
                "user_id": "INTEGER",
                "player_id": "INTEGER",
                "match_id": "INTEGER",
                "rating": "INTEGER DEFAULT 3",
            },
            "match_history": {
                "match_id": "INTEGER",
                "user_id": "INTEGER",
                "points_awarded": "REAL DEFAULT 0",
                "player_id": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            for column, definition in columns.items():
                _ensure_column(conn, table, column, definition)

        # Limpia duplicados antiguos antes de crear el índice único. Si había
        # una fila titular y otra suplente, conserva la de titular.
        conn.execute(
            """DELETE FROM user_squads
               WHERE rowid NOT IN (
                   SELECT CASE
                       WHEN MAX(COALESCE(is_starter, 0)) = 1
                           THEN MIN(CASE WHEN is_starter = 1 THEN rowid END)
                       ELSE MIN(rowid)
                   END
                   FROM user_squads
                   GROUP BY user_id, player_id
               )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_squad_user_player "
            "ON user_squads(user_id, player_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_status_date "
            "ON matches(status, match_date, match_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ratings_match_player "
            "ON ratings(match_id, player_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_history_match "
            "ON match_history(match_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_player_history_match "
            "ON match_player_history(match_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manager_points_reset_match_status "
            "ON manager_points_reset_logs(match_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manager_points_reset_items_reset "
            "ON manager_points_reset_items(reset_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_user_snapshot_eligible "
            "ON match_user_snapshot(match_id, eligible, starter_count)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_squad_snapshot_player "
            "ON match_squad_snapshot(match_id, player_id, is_starter)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_price_player_date "
            "ON market_price_history(player_id, recorded_at)"
        )
        # Crea un punto de partida para los gráficos de precios de jugadores ya existentes.
        conn.execute(
            """INSERT INTO market_price_history(
                   player_id, price, owners_count, managers_total, recorded_at, source
               )
               SELECT p.id, p.price,
                      (SELECT COUNT(DISTINCT s.user_id) FROM user_squads s JOIN users u ON u.id=s.user_id WHERE s.player_id=p.id AND u.role='user'),
                      (SELECT COUNT(*) FROM users WHERE role='user'),
                      ?, 'baseline'
               FROM players p
               WHERE NOT EXISTS (
                   SELECT 1 FROM market_price_history h WHERE h.player_id=p.id
               )""",
            (now_local().isoformat(timespec="seconds"),),
        )

        admin = conn.execute(
            "SELECT id, password FROM users WHERE LOWER(username)=? LIMIT 1",
            (ADMIN_USERNAME,),
        ).fetchone()
        if admin is None and admin_password:
            conn.execute(
                "INSERT INTO users(username, password, role, rupias, total_points) "
                "VALUES (?, ?, 'admin', 9999, 0)",
                (ADMIN_USERNAME, generate_password_hash(admin_password)),
            )
        elif admin is not None and admin_password:
            # Migra automáticamente la contraseña débil de la versión anterior.
            try:
                has_default_password = check_password_hash(admin["password"], "1234")
            except (ValueError, TypeError):
                has_default_password = False
            if has_default_password:
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(admin_password), admin["id"]),
                )


def admin_setup_needed() -> bool:
    rows = run_query(
        "SELECT password FROM users WHERE LOWER(username)=? LIMIT 1", (ADMIN_USERNAME,)
    )
    if not rows:
        return not get_config_value(ADMIN_PASSWORD_ENV)
    try:
        return check_password_hash(rows[0]["password"], "1234") and not get_config_value(
            ADMIN_PASSWORD_ENV
        )
    except (ValueError, TypeError):
        return False


# ============================================================
# 2. FECHAS, MERCADO Y PUNTUACIONES
# ============================================================
def now_local() -> datetime:
    """Hora de España peninsular, devuelta sin tzinfo para comparar con SQLite."""
    if MADRID_TZ is not None:
        return datetime.now(MADRID_TZ).replace(tzinfo=None)
    return datetime.now()


def parse_match_datetime(match_date: Any, match_time: Any) -> datetime:
    date_text = str(match_date).strip()
    time_text = str(match_time).strip().split(".")[0]
    if not date_text or not time_text:
        raise ValueError("La fecha o la hora del partido están vacías.")
    return datetime.fromisoformat(f"{date_text}T{time_text}")


def format_countdown(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def get_voting_window(match_date: Any, match_time: Any) -> tuple[datetime, datetime, datetime]:
    kickoff = parse_match_datetime(match_date, match_time)
    return (
        kickoff,
        kickoff + timedelta(hours=VOTING_OPENS_AFTER_HOURS),
        kickoff + timedelta(hours=VOTING_CLOSES_AFTER_HOURS),
    )


def market_lock_info(conn: sqlite3.Connection | None = None) -> tuple[bool, sqlite3.Row | None, str | None]:
    """Bloquea el mercado desde el inicio del primer partido pendiente hasta procesarlo."""
    owns_connection = conn is None
    if conn is None:
        conn = get_connection()
    try:
        pending = conn.execute(
            "SELECT id, jornada, rival, match_date, match_time FROM matches "
            "WHERE status='PENDIENTE' ORDER BY match_date, match_time, id"
        ).fetchall()
        now = now_local()
        for match in pending:
            try:
                kickoff = parse_match_datetime(match["match_date"], match["match_time"])
            except (TypeError, ValueError):
                return True, match, "Hay un partido pendiente con fecha u hora no válida."
            if now >= kickoff:
                return True, match, None
        return False, None, None
    finally:
        if owns_connection:
            conn.close()


def is_market_locked() -> bool:
    return market_lock_info()[0]


def calculate_laliga_points(
    pos: str,
    goles: int,
    asistencias: int,
    porteria_cero: bool,
    penalti_parado: int,
    penalti_fallado: int,
) -> float:
    """Puntuación base según las reglas configuradas en esta aplicación."""
    pts = 0
    if pos in ("POR", "DEF"):
        pts += goles * 6
    elif pos == "MED":
        pts += goles * 5
    elif pos == "DEL":
        pts += goles * 4
    pts += asistencias * 3
    if porteria_cero:
        if pos in ("POR", "DEF"):
            pts += 4
        elif pos == "MED":
            pts += 1
    if pos == "POR":
        pts += penalti_parado * 5
    pts -= penalti_fallado * 2
    return float(pts)


def calculate_market_price(
    current_price: float, owners: int, total_managers: int
) -> tuple[float, float]:
    """Calcula la variación por porcentaje de managers propietarios.

    Tramos: 0 %: -3 %; >0 % y <25 %: -1 %; 25 % a <50 %: +1 %;
    50 % a 85 % (incluidos): +5 %; >85 %: +7 %.
    Redondea a céntimos y nunca permite un precio inferior a 0,50 rupias.
    """
    old_price = max(MIN_PLAYER_PRICE, _to_money(current_price))
    safe_owners = max(0, int(owners))
    safe_total = max(0, int(total_managers))
    ownership_share = (safe_owners / safe_total) if safe_total > 0 else 0.0

    if safe_owners == 0 or ownership_share == 0:
        variation = Decimal("-0.03")
    elif ownership_share < 0.25:
        variation = Decimal("-0.01")
    elif ownership_share < 0.50:
        variation = Decimal("0.01")
    elif ownership_share <= 0.85:
        variation = Decimal("0.05")
    else:
        variation = Decimal("0.07")

    adjusted = (old_price * (Decimal("1.00") + variation)).quantize(
        MONEY_CENT, rounding=ROUND_HALF_UP
    )
    new_price = min(MAX_PLAYER_PRICE, max(MIN_PLAYER_PRICE, adjusted))
    return float(new_price), float(variation)


def get_market_update_schedule() -> tuple[datetime | None, datetime | None]:
    """Devuelve la última actualización registrada y la siguiente fecha prevista."""
    rows = run_query(
        "SELECT executed_at FROM market_updates ORDER BY executed_at DESC LIMIT 1"
    )
    if not rows:
        return None, None
    try:
        last_update = datetime.fromisoformat(str(rows[0]["executed_at"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("La fecha de la última actualización del mercado no es válida.") from exc
    return last_update, last_update + timedelta(days=MARKET_UPDATE_INTERVAL_DAYS)


def automatic_market_update_if_due() -> tuple[bool, str]:
    """Actualiza el mercado al iniciar/ejecutar la app si han pasado tres días.

    Streamlit Community Cloud no mantiene procesos de fondo cuando la app está inactiva.
    Por ello, la revisión se realiza en cada ejecución de la aplicación y la actualización
    se aplica en la primera visita/ejecución después de cumplirse el plazo.
    """
    with db_transaction() as conn:
        now = now_local()
        last_row = conn.execute(
            "SELECT executed_at FROM market_updates ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()

        # Primera ejecución de esta instalación: inicia el contador de tres días.
        if last_row is None:
            conn.execute(
                "INSERT INTO market_updates(update_key, executed_at) VALUES (?, ?)",
                (MARKET_UPDATE_KEY, now.isoformat(timespec="seconds")),
            )
            return False, "Calendario de actualización del mercado iniciado."

        try:
            last_update = datetime.fromisoformat(str(last_row["executed_at"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "No se puede interpretar la fecha de la última actualización del mercado."
            ) from exc

        next_update = last_update + timedelta(days=MARKET_UPDATE_INTERVAL_DAYS)
        if now < next_update:
            return False, "Todavía no corresponde actualizar el mercado."

        locked, _, reason = market_lock_info(conn)
        if locked:
            return False, reason or (
                "La actualización está pendiente: el mercado está bloqueado mientras haya "
                "un partido comenzado sin procesar. Se ejecutará cuando se desbloquee."
            )

        total_managers = int(
            conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
        )
        players = conn.execute("SELECT id, price FROM players ORDER BY id").fetchall()
        increased_players = 0
        decreased_players = 0
        unchanged_players = 0
        for player in players:
            owners = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT s.user_id) FROM user_squads s "
                    "JOIN users u ON u.id=s.user_id WHERE s.player_id=? AND u.role='user'",
                    (player["id"],),
                ).fetchone()[0]
            )
            old_price = float(player["price"])
            new_price, variation = calculate_market_price(
                old_price, owners, total_managers
            )
            if abs(new_price - old_price) > 0.000001:
                conn.execute(
                    "UPDATE players SET price=? WHERE id=?",
                    (new_price, player["id"]),
                )
                if new_price > old_price:
                    increased_players += 1
                else:
                    decreased_players += 1
            else:
                unchanged_players += 1
            # Un punto histórico por jugador y ciclo, incluso cuando el precio no cambia.
            conn.execute(
                "INSERT INTO market_price_history(player_id, price, owners_count, managers_total, recorded_at, source) "
                "VALUES (?, ?, ?, ?, ?, 'automatico')",
                (
                    int(player["id"]), new_price, owners, total_managers,
                    now.isoformat(timespec="seconds"),
                ),
            )

        # Se conserva un único registro periódico y se actualiza su fecha en cada ciclo.
        conn.execute(
            "INSERT INTO market_updates(update_key, executed_at) VALUES (?, ?) "
            "ON CONFLICT(update_key) DO UPDATE SET executed_at=excluded.executed_at",
            (MARKET_UPDATE_KEY, now.isoformat(timespec="seconds")),
        )
        return True, (
            "Mercado actualizado automáticamente: "
            f"{increased_players} jugadores han subido, "
            f"{decreased_players} han bajado y {unchanged_players} se mantienen "
            "(por ejemplo, al haber alcanzado el precio mínimo de 0,50 rupias)."
        )


@st.fragment(run_every="300s")
def market_scheduler_monitor() -> None:
    """Comprueba el vencimiento cada minuto mientras haya una sesión activa."""
    try:
        updated, message = automatic_market_update_if_due()
        if updated:
            st.success(message)
    except (sqlite3.Error, ValueError) as exc:
        st.warning(f"No se pudo comprobar/actualizar el mercado automáticamente: {exc}")


# ============================================================
# 3. OPERACIONES DE NEGOCIO ATÓMICAS
# ============================================================
def add_player(name: str, position: str, price: float) -> None:
    clean_name = name.strip()
    amount = _to_money(price)
    if not clean_name:
        raise ValueError("El nombre del jugador no puede estar vacío.")
    if position not in VALID_POSITIONS:
        raise ValueError("La posición seleccionada no es válida.")
    if not MIN_PLAYER_PRICE <= amount <= MAX_PLAYER_PRICE:
        raise ValueError(
            f"El precio debe estar entre {format_rupias(MIN_PLAYER_PRICE)} y "
            f"{format_rupias(MAX_PLAYER_PRICE)} rupias."
        )
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "No se pueden modificar jugadores durante un partido.")
        cursor = conn.execute(
            "INSERT INTO players(name, position, price, points) VALUES (?, ?, ?, 0)",
            (clean_name, position, float(amount)),
        )
        player_id = int(cursor.lastrowid)
        managers_total = int(
            conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO market_price_history(player_id, price, owners_count, managers_total, recorded_at, source) "
            "VALUES (?, ?, 0, ?, ?, 'alta')",
            (player_id, float(amount), managers_total, now_local().isoformat(timespec="seconds")),
        )


def update_player_price(player_id: int, price: float) -> None:
    amount = _to_money(price)
    if not MIN_PLAYER_PRICE <= amount <= MAX_PLAYER_PRICE:
        raise ValueError(
            f"El precio debe estar entre {format_rupias(MIN_PLAYER_PRICE)} y "
            f"{format_rupias(MAX_PLAYER_PRICE)} rupias."
        )
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "No se pueden cambiar precios durante un partido.")
        cursor = conn.execute(
            "UPDATE players SET price=? WHERE id=?", (float(amount), int(player_id))
        )
        if cursor.rowcount == 0:
            raise ValueError("No existe ningún jugador con ese ID.")
        owners = int(
            conn.execute(
                "SELECT COUNT(DISTINCT s.user_id) FROM user_squads s "
                "JOIN users u ON u.id=s.user_id WHERE s.player_id=? AND u.role='user'",
                (int(player_id),),
            ).fetchone()[0]
        )
        managers_total = int(
            conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO market_price_history(player_id, price, owners_count, managers_total, recorded_at, source) "
            "VALUES (?, ?, ?, ?, ?, 'admin')",
            (int(player_id), float(amount), owners, managers_total, now_local().isoformat(timespec="seconds")),
        )


def delete_player(player_id: int) -> None:
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "No se pueden borrar jugadores durante un partido.")
        player = conn.execute(
            "SELECT id, points FROM players WHERE id=?", (int(player_id),)
        ).fetchone()
        if player is None:
            raise ValueError("No existe ningún jugador con ese ID.")
        has_history = conn.execute(
            "SELECT 1 FROM match_player_history WHERE player_id=? LIMIT 1",
            (int(player_id),),
        ).fetchone()
        has_final_rating = conn.execute(
            "SELECT 1 FROM ratings r JOIN matches m ON m.id=r.match_id "
            "WHERE r.player_id=? AND m.status='FINALIZADO' LIMIT 1",
            (int(player_id),),
        ).fetchone()
        has_user_history = conn.execute(
            "SELECT 1 FROM match_history WHERE player_id=? LIMIT 1", (int(player_id),)
        ).fetchone()
        has_kickoff_snapshot = conn.execute(
            "SELECT 1 FROM match_squad_snapshot WHERE player_id=? LIMIT 1", (int(player_id),)
        ).fetchone()
        if abs(float(player["points"] or 0)) > 0.0001 or has_history or has_final_rating or has_user_history or has_kickoff_snapshot:
            raise ValueError(
                "No se puede borrar un jugador con puntuaciones, historial o una fotografía de plantilla de un partido. "
                "Conserva el registro para mantener la trazabilidad y poder revertir jornadas."
            )
        conn.execute("DELETE FROM user_squads WHERE player_id=?", (int(player_id),))
        conn.execute("DELETE FROM ratings WHERE player_id=?", (int(player_id),))
        conn.execute("DELETE FROM players WHERE id=?", (int(player_id),))


def delete_match(match_id: int) -> None:
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT id, status, match_date, match_time FROM matches WHERE id=?", (int(match_id),)
        ).fetchone()
        if match is None:
            raise ValueError("No existe ningún partido con ese ID.")
        if match["status"] != "PENDIENTE":
            raise ValueError("No se puede borrar un partido finalizado. Revierte sus puntos primero.")
        try:
            kickoff = parse_match_datetime(match["match_date"], match["match_time"])
        except (TypeError, ValueError):
            raise ValueError("No se puede borrar el partido porque su fecha u hora no es válida.")
        if now_local() >= kickoff:
            raise ValueError(
                "No se puede borrar un partido que ya ha comenzado; debe procesarse para conservar "
                "la integridad de las plantillas y las puntuaciones."
            )
        has_player_history = conn.execute(
            "SELECT 1 FROM match_player_history WHERE match_id=? LIMIT 1", (int(match_id),)
        ).fetchone()
        has_user_history = conn.execute(
            "SELECT 1 FROM match_history WHERE match_id=? LIMIT 1", (int(match_id),)
        ).fetchone()
        if has_player_history or has_user_history:
            raise ValueError("Este partido tiene historial de puntuación y no se puede borrar.")
        conn.execute("DELETE FROM ratings WHERE match_id=?", (int(match_id),))
        conn.execute("DELETE FROM matches WHERE id=?", (int(match_id),))


def adjust_user_money(user_id: int, amount: float) -> None:
    balance = _to_money(amount)
    if balance < 0:
        raise ValueError("El saldo no puede ser negativo.")
    with db_transaction() as conn:
        cursor = conn.execute(
            "UPDATE users SET rupias=? WHERE id=? AND role='user'",
            (float(balance), int(user_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError("No existe ningún manager con ese ID.")


def reset_manager_password(user_id: int, new_password: str) -> None:
    """Restablece la contraseña de un manager sin revelar la contraseña anterior."""
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    with db_transaction() as conn:
        cursor = conn.execute(
            "UPDATE users SET password=? WHERE id=? AND role='user'",
            (generate_password_hash(new_password), int(user_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError("No existe ningún manager con ese ID. La cuenta de administrador no se puede modificar aquí.")


def delete_manager_account(user_id: int) -> str:
    """Elimina una cuenta de manager y sus registros dependientes de forma atómica."""
    with db_transaction() as conn:
        user = conn.execute(
            "SELECT id, username, role FROM users WHERE id=?", (int(user_id),)
        ).fetchone()
        if user is None or user["role"] != "user":
            raise ValueError("La cuenta no existe o no es una cuenta de manager. El administrador está protegido.")

        # Un borrado de puntos que incluya a este manager ya no podrá restaurarse
        # íntegramente si se elimina su cuenta; se invalida y se limpian sus copias.
        conn.execute(
            "UPDATE manager_points_reset_logs SET status='INVALIDATED' "
            "WHERE id IN (SELECT reset_id FROM manager_points_reset_items WHERE user_id=?) "
            "AND status='ACTIVE'",
            (int(user_id),),
        )
        conn.execute("DELETE FROM manager_points_reset_items WHERE user_id=?", (int(user_id),))
        # Borrar primero las tablas que tienen claves foráneas restrictivas.
        conn.execute("DELETE FROM user_squads WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM ratings WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM match_history WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM match_squad_snapshot WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM match_user_snapshot WHERE user_id=?", (int(user_id),))
        cursor = conn.execute(
            "DELETE FROM users WHERE id=? AND role='user'", (int(user_id),)
        )
        if cursor.rowcount != 1:
            raise ValueError("No se pudo eliminar la cuenta.")
        return str(user["username"])




def set_starter(user_id: int, player_id: int, make_starter: bool) -> None:
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "No se pueden modificar las plantillas durante un partido.")
        squad_entry = conn.execute(
            "SELECT is_starter FROM user_squads WHERE user_id=? AND player_id=?",
            (int(user_id), int(player_id)),
        ).fetchone()
        if squad_entry is None:
            raise ValueError("El jugador ya no pertenece a tu plantilla.")
        if make_starter and not squad_entry["is_starter"]:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_squads WHERE user_id=? AND is_starter=1",
                (int(user_id),),
            ).fetchone()[0]
            if count >= MAX_STARTERS:
                raise ValueError(f"Solo puedes tener {MAX_STARTERS} titulares.")
        conn.execute(
            "UPDATE user_squads SET is_starter=? WHERE user_id=? AND player_id=?",
            (int(make_starter), int(user_id), int(player_id)),
        )


def buy_player(user_id: int, player_id: int) -> None:
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "El mercado está cerrado durante un partido.")
        player = conn.execute(
            "SELECT id, price FROM players WHERE id=?", (int(player_id),)
        ).fetchone()
        user = conn.execute(
            "SELECT rupias, role FROM users WHERE id=?", (int(user_id),)
        ).fetchone()
        if player is None or user is None or user["role"] != "user":
            raise ValueError("No se ha podido identificar al usuario o al jugador.")
        if conn.execute(
            "SELECT 1 FROM user_squads WHERE user_id=? AND player_id=?",
            (int(user_id), int(player_id)),
        ).fetchone():
            raise ValueError("Ya tienes a este jugador en tu plantilla.")
        squad_count = conn.execute(
            "SELECT COUNT(*) FROM user_squads WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
        if squad_count >= MAX_SQUAD_SIZE:
            raise ValueError(f"La plantilla está llena: máximo {MAX_SQUAD_SIZE} jugadores.")
        player_price = _to_money(player["price"])
        user_balance = _to_money(user["rupias"])
        if user_balance < player_price:
            raise ValueError("Saldo insuficiente.")
        conn.execute(
            "UPDATE users SET rupias=? WHERE id=?",
            (float(user_balance - player_price), int(user_id)),
        )
        conn.execute(
            "INSERT INTO user_squads(user_id, player_id, is_starter) VALUES (?, ?, 0)",
            (int(user_id), int(player_id)),
        )


def sell_player(user_id: int, player_id: int) -> float:
    with db_transaction() as conn:
        locked, _, reason = market_lock_info(conn)
        if locked:
            raise ValueError(reason or "No se pueden vender jugadores durante un partido.")
        row = conn.execute(
            "SELECT p.price FROM user_squads s JOIN players p ON p.id=s.player_id "
            "WHERE s.user_id=? AND s.player_id=?",
            (int(user_id), int(player_id)),
        ).fetchone()
        if row is None:
            raise ValueError("El jugador ya no está en tu plantilla.")
        price = _to_money(row["price"])
        conn.execute(
            "DELETE FROM user_squads WHERE user_id=? AND player_id=?",
            (int(user_id), int(player_id)),
        )
        conn.execute(
            "UPDATE users SET rupias=rupias+? WHERE id=?", (float(price), int(user_id))
        )
        return float(price)


def submit_ratings(user_id: int, match_id: int, ratings: dict[int, int]) -> None:
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT match_date, match_time, status FROM matches WHERE id=?",
            (int(match_id),),
        ).fetchone()
        if match is None or match["status"] != "PENDIENTE":
            raise ValueError("Este partido ya no admite votaciones.")
        try:
            _, opens_at, closes_at = get_voting_window(
                match["match_date"], match["match_time"]
            )
        except (TypeError, ValueError):
            raise ValueError("La fecha u hora del partido no es válida.")
        now = now_local()
        if now < opens_at or now >= closes_at:
            raise ValueError("La ventana de votación está cerrada.")
        if not ratings:
            raise ValueError("No hay jugadores a los que votar.")
        if conn.execute(
            "SELECT 1 FROM ratings WHERE user_id=? AND match_id=? LIMIT 1",
            (int(user_id), int(match_id)),
        ).fetchone():
            raise ValueError("Ya has votado en este partido.")
        expected = {
            int(row[0])
            for row in conn.execute("SELECT id FROM players").fetchall()
        }
        if set(map(int, ratings.keys())) != expected:
            raise ValueError("La lista de jugadores ha cambiado. Recarga y vuelve a votar.")
        for player_id, rating in ratings.items():
            if not 1 <= int(rating) <= 5:
                raise ValueError("Todas las valoraciones deben estar entre 1 y 5.")
            conn.execute(
                "INSERT INTO ratings(user_id, player_id, match_id, rating) VALUES (?, ?, ?, ?)",
                (int(user_id), int(player_id), int(match_id), int(rating)),
            )


def process_match(match_id: int, stats: dict[int, dict[str, Any]]) -> None:
    """
    Procesa un partido usando la plantilla que estaba congelada en su kickoff.

    Las plantillas se bloquean desde el inicio del primer partido pendiente. Por
    ello, aunque la fotografía se persista cuando se procesa la jornada (tras el
    cierre de votaciones), el contenido representa el estado de la plantilla en
    el momento de inicio del partido.

    Solo reciben puntos los managers que, en ese instante:
      1) tenían al futbolista en su plantilla como titular; y
      2) tenían al menos cuatro jugadores alineados.

    Se conserva la regla anterior de que los suplentes no puntúan para el manager.
    Los puntos individuales del futbolista sí se acumulan siempre.
    """
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT id, match_date, match_time, status FROM matches WHERE id=?",
            (int(match_id),),
        ).fetchone()
        if match is None or match["status"] != "PENDIENTE":
            raise ValueError("El partido ya no está pendiente de procesar.")
        try:
            kickoff, _, closes_at = get_voting_window(
                match["match_date"], match["match_time"]
            )
        except (TypeError, ValueError):
            raise ValueError("La fecha u hora del partido no es válida.")
        if now_local() < closes_at:
            raise ValueError(
                "No se puede procesar la jornada hasta que cierre la votación "
                f"({closes_at.strftime('%d/%m/%Y %H:%M')})."
            )

        players = conn.execute(
            "SELECT id, position FROM players ORDER BY id"
        ).fetchall()
        if not players:
            raise ValueError("Añade al menos un jugador antes de procesar el partido.")
        player_ids = {int(player["id"]) for player in players}
        if set(map(int, stats.keys())) != player_ids:
            raise ValueError("La lista de jugadores ha cambiado. Recarga el formulario.")

        # La fotografía se crea una sola vez. Si el partido se revierte y se
        # reprocesa, se conserva la fotografía original del kickoff.
        snapshot_meta = conn.execute(
            "SELECT snapshot_status FROM match_snapshot_meta WHERE match_id=?",
            (int(match_id),),
        ).fetchone()
        if snapshot_meta is not None and snapshot_meta["snapshot_status"] == "LEGACY":
            raise ValueError(
                "Este partido procede de una versión anterior y no dispone de una "
                "fotografía fiable de las plantillas al inicio. No se puede reprocesar "
                "con las nuevas condiciones sin reconstruir manualmente ese historial."
            )

        if snapshot_meta is None:
            managers_count = int(
                conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
            )
            captured_at = now_local().isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO match_snapshot_meta(
                    match_id, kickoff_at, captured_at, managers_count, snapshot_status
                ) VALUES (?, ?, ?, ?, 'CAPTURED')""",
                (int(match_id), kickoff.isoformat(timespec="seconds"), captured_at, managers_count),
            )
            conn.execute(
                """INSERT INTO match_user_snapshot(match_id, user_id, starter_count, eligible)
                   SELECT ?, u.id,
                          COALESCE(SUM(CASE WHEN s.is_starter=1 THEN 1 ELSE 0 END), 0),
                          CASE WHEN COALESCE(SUM(CASE WHEN s.is_starter=1 THEN 1 ELSE 0 END), 0) >= 4
                               THEN 1 ELSE 0 END
                   FROM users u
                   LEFT JOIN user_squads s ON s.user_id=u.id
                   WHERE u.role='user'
                   GROUP BY u.id""",
                (int(match_id),),
            )
            conn.execute(
                """INSERT INTO match_squad_snapshot(match_id, user_id, player_id, is_starter)
                   SELECT ?, s.user_id, s.player_id, s.is_starter
                   FROM user_squads s
                   JOIN users u ON u.id=s.user_id
                   WHERE u.role='user'""",
                (int(match_id),),
            )
        elif snapshot_meta["snapshot_status"] != "CAPTURED":
            raise ValueError("El estado de la fotografía de plantillas no es válido.")

        for player in players:
            player_id = int(player["id"])
            stat = stats[player_id]
            goals = int(stat["g"])
            assists = int(stat["a"])
            penalties_saved = int(stat["pp"])
            penalties_missed = int(stat["pf"])
            if min(goals, assists, penalties_saved, penalties_missed) < 0:
                raise ValueError("Las estadísticas no pueden ser negativas.")

            base_points = calculate_laliga_points(
                player["position"],
                goals,
                assists,
                bool(stat["pc"]),
                penalties_saved,
                penalties_missed,
            )
            avg_row = conn.execute(
                "SELECT AVG(rating) AS average_rating FROM ratings "
                "WHERE player_id=? AND match_id=?",
                (player_id, int(match_id)),
            ).fetchone()
            average_rating = avg_row["average_rating"]
            total_points = round(
                base_points + (round(float(average_rating), 2) if average_rating is not None else 0),
                2,
            )

            conn.execute(
                "UPDATE players SET points=ROUND(COALESCE(points, 0)+?, 2) WHERE id=?",
                (total_points, player_id),
            )
            conn.execute(
                "INSERT INTO match_player_history(match_id, player_id, points_awarded) "
                "VALUES (?, ?, ?)",
                (int(match_id), player_id, total_points),
            )

            # Se exigen simultáneamente: jugador titular en la fotografía de
            # kickoff y al menos cuatro titulares registrados para ese manager.
            eligible_starters = conn.execute(
                """SELECT squad.user_id
                   FROM match_squad_snapshot squad
                   JOIN match_user_snapshot manager
                     ON manager.match_id=squad.match_id AND manager.user_id=squad.user_id
                   WHERE squad.match_id=? AND squad.player_id=?
                     AND squad.is_starter=1 AND manager.eligible=1
                     AND manager.starter_count >= 4""",
                (int(match_id), player_id),
            ).fetchall()
            for starter in eligible_starters:
                user_id = int(starter["user_id"])
                conn.execute(
                    "UPDATE users SET total_points=ROUND(COALESCE(total_points, 0)+?, 2) WHERE id=?",
                    (total_points, user_id),
                )
                conn.execute(
                    "INSERT INTO match_history(match_id, user_id, points_awarded, player_id) "
                    "VALUES (?, ?, ?, ?)",
                    (int(match_id), user_id, total_points, player_id),
                )

        cursor = conn.execute(
            "UPDATE matches SET status='FINALIZADO' WHERE id=? AND status='PENDIENTE'",
            (int(match_id),),
        )
        if cursor.rowcount != 1:
            raise ValueError("No se pudo actualizar el estado del partido; no se guardó la jornada.")


def reverse_match(match_id: int) -> None:
    """Revierte tanto los totales de jugadores como los de managers."""
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT id, status FROM matches WHERE id=?", (int(match_id),)
        ).fetchone()
        if match is None or match["status"] != "FINALIZADO":
            raise ValueError("Solo se pueden revertir partidos finalizados.")

        player_history = conn.execute(
            "SELECT player_id, points_awarded FROM match_player_history WHERE match_id=?",
            (int(match_id),),
        ).fetchall()
        legacy_history = conn.execute(
            "SELECT 1 FROM match_history WHERE match_id=? AND player_id IS NULL LIMIT 1",
            (int(match_id),),
        ).fetchone()
        if not player_history or legacy_history:
            raise ValueError(
                "No se puede revertir automáticamente esta jornada porque se procesó "
                "con una versión antigua que no guardaba el historial por jugador. "
                "Se necesita una corrección manual o reconstruir ese historial antes de revertirla."
            )

        snapshot_meta = conn.execute(
            "SELECT snapshot_status FROM match_snapshot_meta WHERE match_id=?",
            (int(match_id),),
        ).fetchone()
        if snapshot_meta is None or snapshot_meta["snapshot_status"] != "CAPTURED":
            raise ValueError(
                "Este partido se procesó con una versión anterior que no guardaba "
                "la plantilla del inicio. No se puede revertir con garantías porque "
                "no es posible reconstruir qué jugadores tenía cada manager en el kickoff."
            )

        for item in player_history:
            cursor = conn.execute(
                "UPDATE players SET points=ROUND(COALESCE(points, 0)-?, 2) WHERE id=?",
                (float(item["points_awarded"]), int(item["player_id"])),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"No existe el jugador ID {item['player_id']}; no se ha revertido ningún cambio."
                )

        users_history = conn.execute(
            "SELECT user_id, SUM(points_awarded) AS points_to_remove FROM match_history "
            "WHERE match_id=? AND player_id IS NOT NULL GROUP BY user_id",
            (int(match_id),),
        ).fetchall()
        for item in users_history:
            conn.execute(
                "UPDATE users SET total_points=ROUND(COALESCE(total_points, 0)-?, 2) WHERE id=?",
                (float(item["points_to_remove"] or 0), int(item["user_id"])),
            )

        conn.execute("DELETE FROM match_history WHERE match_id=?", (int(match_id),))
        conn.execute("DELETE FROM match_player_history WHERE match_id=?", (int(match_id),))
        # La fotografía de kickoff se conserva para que un reprocesamiento
        # utilice exactamente la misma plantilla y alineación originales.
        conn.execute(
            "UPDATE matches SET status='PENDIENTE' WHERE id=?", (int(match_id),)
        )
        # Si antes se habían borrado solo los puntos de managers, la reversión
        # completa hace que la antigua copia deje de poder restaurarse.
        conn.execute(
            "UPDATE manager_points_reset_logs SET status='INVALIDATED' "
            "WHERE match_id=? AND status='ACTIVE'",
            (int(match_id),),
        )


def reset_manager_points_for_match(match_id: int, reset_by: str = ADMIN_USERNAME) -> dict[str, Any]:
    """Elimina solo los puntos de managers de un partido finalizado.

    Conserva los puntos acumulados de los jugadores, el estado del partido,
    las votaciones y la fotografía de las plantillas. Guarda una copia auditable
    para permitir restaurar los puntos de los managers si fue un error.
    """
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT id, jornada, rival, status FROM matches WHERE id=?",
            (int(match_id),),
        ).fetchone()
        if match is None or match["status"] != "FINALIZADO":
            raise ValueError("Solo se pueden borrar puntos de managers de un partido finalizado.")

        active_reset = conn.execute(
            "SELECT id FROM manager_points_reset_logs "
            "WHERE match_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
            (int(match_id),),
        ).fetchone()
        if active_reset:
            raise ValueError(
                "Los puntos de los managers de este partido ya están borrados. "
                "Puedes restaurarlos desde el apartado de administración."
            )

        history_rows = conn.execute(
            "SELECT id, user_id, player_id, points_awarded FROM match_history "
            "WHERE match_id=? ORDER BY id",
            (int(match_id),),
        ).fetchall()
        if not history_rows:
            raise ValueError("Este partido no tiene puntos de managers registrados para borrar.")

        user_totals: dict[int, float] = {}
        for row in history_rows:
            user_id = int(row["user_id"])
            user_totals[user_id] = user_totals.get(user_id, 0.0) + float(row["points_awarded"] or 0.0)

        total_removed = sum(user_totals.values())
        reset_cursor = conn.execute(
            """INSERT INTO manager_points_reset_logs(
                   match_id, reset_at, reset_by, status, managers_affected,
                   history_rows, points_removed
               ) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?)""",
            (
                int(match_id),
                now_local().isoformat(timespec="seconds"),
                str(reset_by or ADMIN_USERNAME)[:100],
                len(user_totals),
                len(history_rows),
                round(total_removed, 2),
            ),
        )
        reset_id = int(reset_cursor.lastrowid)

        conn.executemany(
            """INSERT INTO manager_points_reset_items(
                   reset_id, user_id, player_id, points_awarded
               ) VALUES (?, ?, ?, ?)""",
            [
                (
                    reset_id,
                    int(row["user_id"]),
                    int(row["player_id"]) if row["player_id"] is not None else None,
                    float(row["points_awarded"] or 0.0),
                )
                for row in history_rows
            ],
        )

        for user_id, points_to_remove in user_totals.items():
            cursor = conn.execute(
                "UPDATE users SET total_points=ROUND(COALESCE(total_points, 0)-?, 2) "
                "WHERE id=? AND role='user'",
                (points_to_remove, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"No se pudo actualizar el manager ID {user_id}; no se aplicó ningún cambio."
                )

        conn.execute("DELETE FROM match_history WHERE match_id=?", (int(match_id),))
        return {
            "reset_id": reset_id,
            "managers_affected": len(user_totals),
            "history_rows": len(history_rows),
            "points_removed": round(total_removed, 2),
            "jornada": str(match["jornada"]),
            "rival": str(match["rival"]),
        }


def restore_manager_points_for_match(match_id: int) -> dict[str, Any]:
    """Restaura el último borrado activo de puntos de managers de un partido."""
    with db_transaction() as conn:
        match = conn.execute(
            "SELECT id, jornada, rival, status FROM matches WHERE id=?",
            (int(match_id),),
        ).fetchone()
        if match is None or match["status"] != "FINALIZADO":
            raise ValueError("Solo se pueden restaurar puntos de managers si el partido sigue finalizado.")

        reset_log = conn.execute(
            "SELECT id FROM manager_points_reset_logs "
            "WHERE match_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
            (int(match_id),),
        ).fetchone()
        if reset_log is None:
            raise ValueError("No hay ningún borrado de puntos pendiente de restaurar para este partido.")

        existing_history = conn.execute(
            "SELECT 1 FROM match_history WHERE match_id=? LIMIT 1",
            (int(match_id),),
        ).fetchone()
        if existing_history:
            raise ValueError(
                "Ya existen puntos de managers registrados para este partido. "
                "No se pueden restaurar para evitar duplicidades."
            )

        items = conn.execute(
            "SELECT user_id, player_id, points_awarded FROM manager_points_reset_items "
            "WHERE reset_id=? ORDER BY id",
            (int(reset_log["id"]),),
        ).fetchall()
        if not items:
            raise ValueError("No se encontró la copia de seguridad de los puntos de managers.")

        for item in items:
            user_id = int(item["user_id"])
            points = float(item["points_awarded"] or 0.0)
            cursor = conn.execute(
                "UPDATE users SET total_points=ROUND(COALESCE(total_points, 0)+?, 2) "
                "WHERE id=? AND role='user'",
                (points, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"No existe el manager ID {user_id}; no se restauraron los puntos."
                )
            conn.execute(
                "INSERT INTO match_history(match_id, user_id, points_awarded, player_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    int(match_id),
                    user_id,
                    points,
                    int(item["player_id"]) if item["player_id"] is not None else None,
                ),
            )

        conn.execute(
            "UPDATE manager_points_reset_logs SET status='RESTORED', restored_at=? WHERE id=?",
            (now_local().isoformat(timespec="seconds"), int(reset_log["id"])),
        )
        return {
            "managers_affected": len({int(item["user_id"]) for item in items}),
            "history_rows": len(items),
            "points_restored": round(sum(float(item["points_awarded"] or 0.0) for item in items), 2),
            "jornada": str(match["jornada"]),
            "rival": str(match["rival"]),
        }


# ============================================================
# 4. AUTENTICACIÓN
# ============================================================
def login_user(username: str, password: str) -> None:
    normalized = username.strip().lower()
    if not normalized or not password:
        st.error("Introduce el usuario y la contraseña.")
        return
    users = run_query(
        "SELECT id, username, password, role FROM users WHERE LOWER(username)=? LIMIT 1",
        (normalized,),
    )
    if not users:
        st.error("Usuario o contraseña incorrectos.")
        return
    user = users[0]
    try:
        valid_password = check_password_hash(user["password"], password)
    except (ValueError, TypeError):
        valid_password = False
    if not valid_password:
        st.error("Usuario o contraseña incorrectos.")
        return
    if user["role"] == "admin":
        try:
            using_default_password = check_password_hash(user["password"], "1234")
        except (ValueError, TypeError):
            using_default_password = False
        if using_default_password:
            st.error(
                f"La contraseña antigua del administrador está bloqueada. Configura "
                f"{ADMIN_PASSWORD_ENV} con una contraseña de al menos 12 caracteres y reinicia la aplicación."
            )
            return

    st.session_state["user_id"] = int(user["id"])
    st.session_state["username"] = user["username"]
    st.session_state["role"] = user["role"]
    st.session_state["logged_in"] = True
    st.rerun()


def register_user(username: str, password: str) -> None:
    normalized = username.strip().lower()
    if normalized == ADMIN_USERNAME:
        st.error("Ese nombre está reservado.")
        return
    if not re.fullmatch(r"[a-z0-9_-]{3,24}", normalized):
        st.error("El usuario debe tener entre 3 y 24 caracteres: letras, números, guion o guion bajo.")
        return
    if len(password) < 8:
        st.error("La contraseña debe tener al menos 8 caracteres.")
        return
    try:
        with db_transaction() as conn:
            conn.execute(
                "INSERT INTO users(username, password, role, rupias, total_points) "
                "VALUES (?, ?, 'user', 20, 0)",
                (normalized, generate_password_hash(password)),
            )
        st.success("✅ Cuenta creada. Ya puedes iniciar sesión.")
    except sqlite3.IntegrityError:
        st.error("El usuario ya existe.")


# ============================================================
# 5. PANEL DE ADMINISTRACIÓN
# ============================================================
def _show_operation_error(action) -> bool:
    try:
        action()
        return True
    except (ValueError, sqlite3.Error) as exc:
        st.error(str(exc) or "No se pudo completar la operación.")
        return False


def admin_panel() -> None:
    st.markdown('<div class="fantasy-kicker">Gestión de la competición</div>', unsafe_allow_html=True)
    st.title("👑 Panel de administración")
    admin_overview = run_query(
        "SELECT (SELECT COUNT(*) FROM users WHERE role='user') AS managers, "
        "(SELECT COUNT(*) FROM players) AS players, "
        "(SELECT COUNT(*) FROM matches WHERE status='PENDIENTE') AS pending, "
        "(SELECT COALESCE(SUM(rupias),0) FROM users WHERE role='user') AS money"
    )[0]
    overview_cols = st.columns(4)
    overview_cols[0].metric("Managers", int(admin_overview["managers"]))
    overview_cols[1].metric("Jugadores", int(admin_overview["players"]))
    overview_cols[2].metric("Partidos pendientes", int(admin_overview["pending"]))
    overview_cols[3].metric("Rupias en circulación", f"{format_rupias(admin_overview['money'])} ₹")
    t1, t2, t3, t4, t5 = st.tabs(
        ["📅 Partidos", "⚽ Jugadores", "💸 Economía y Mercado", "📊 Puntuaciones", "👥 Usuarios"]
    )

    with t1:
        st.subheader("Añadir partido")
        with st.form("add_match_form"):
            c1, c2 = st.columns(2)
            jornada = c1.text_input("Jornada (ej. Jornada 1)")
            rival = c2.text_input("Rival")
            m_date = c1.date_input("Fecha", value=now_local().date())
            m_time = c2.time_input("Hora", value=time(20, 0))
            submitted = st.form_submit_button("Guardar partido")
        if submitted:
            jornada_clean, rival_clean = jornada.strip(), rival.strip()
            if not jornada_clean or not rival_clean:
                st.error("La jornada y el rival son obligatorios.")
            else:
                try:
                    with db_transaction() as conn:
                        conn.execute(
                            "INSERT INTO matches(jornada, rival, match_date, match_time, status) "
                            "VALUES (?, ?, ?, ?, 'PENDIENTE')",
                            (jornada_clean, rival_clean, m_date.isoformat(), m_time.isoformat(timespec="seconds")),
                        )
                    st.success("Partido añadido.")
                    st.rerun()
                except sqlite3.Error as exc:
                    st.error(f"No se pudo guardar el partido: {exc}")

        st.subheader("Partidos registrados")
        m_df = get_dataframe(
            "SELECT id, jornada, rival, match_date AS fecha, match_time AS hora, status AS estado "
            "FROM matches ORDER BY match_date, match_time, id"
        )
        st.dataframe(m_df, use_container_width=True, hide_index=True)
        with st.expander("Borrar partido pendiente"):
            m_id_del = st.number_input("ID del partido", min_value=1, step=1, key="delete_match_id")
            if st.button("Eliminar partido", key="delete_match_button"):
                if _show_operation_error(lambda: delete_match(int(m_id_del))):
                    st.success("Partido eliminado.")
                    st.rerun()

    with t2:
        st.subheader("Añadir jugador")
        with st.form("add_player_form"):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Nombre")
            pos = c2.selectbox("Posición", VALID_POSITIONS)
            price = c3.number_input("Precio inicial", min_value=0.5, max_value=float(MAX_PLAYER_PRICE), value=5.0, step=0.5, format="%.2f")
            submitted = st.form_submit_button("Añadir jugador")
        if submitted:
            if _show_operation_error(lambda: add_player(name, pos, float(price))):
                st.success("Jugador añadido.")
                st.rerun()

        st.subheader("Jugadores registrados")
        p_df = get_dataframe(
            "SELECT id, name, position, price, ROUND(points, 2) AS points FROM players ORDER BY id"
        )
        st.dataframe(p_df, use_container_width=True, hide_index=True)
        with st.expander("Modificar precio o borrar jugador"):
            c1, c2 = st.columns(2)
            p_id_edit = c1.number_input("ID del jugador", min_value=1, step=1, key="edit_player_id")
            new_price = c2.number_input(
                "Nuevo precio", min_value=0.5, max_value=float(MAX_PLAYER_PRICE), value=5.0, step=0.5, format="%.2f", key="edit_player_price"
            )
            if st.button("Actualizar precio", key="update_player_price_button"):
                if _show_operation_error(lambda: update_player_price(int(p_id_edit), float(new_price))):
                    st.success("Precio actualizado.")
                    st.rerun()
            if st.button("🗑️ Eliminar jugador", key="delete_player_button"):
                if _show_operation_error(lambda: delete_player(int(p_id_edit))):
                    st.success("Jugador eliminado.")
                    st.rerun()

    with t3:
        st.subheader("Actualización automática del mercado")
        st.write(
            "El mercado se actualiza automáticamente cada 3 días. La variación depende del "
            "porcentaje de managers que tienen a cada jugador en su plantilla: 0 % baja un 3 %; "
            "más del 0 % y menos del 25 % baja un 1 %; desde el 25 % hasta menos del 50 % "
            "sube un 1 %; desde el 50 % hasta el 85 % (incluidos) sube un 5 %; por encima "
            "del 85 % sube un 7 %. El precio mínimo es de 0,50 rupias."
        )
        last_update, next_update = get_market_update_schedule()
        if last_update is None or next_update is None:
            st.info("El calendario de actualización se iniciará en la próxima ejecución.")
        else:
            st.write(f"**Última actualización registrada:** {last_update.strftime('%d/%m/%Y %H:%M')}.")
            if now_local() < next_update:
                st.info(
                    "**Próxima actualización prevista:** "
                    f"{next_update.strftime('%d/%m/%Y %H:%M')} (hora peninsular española)."
                )
            else:
                locked, _, reason = market_lock_info()
                if locked:
                    st.warning(
                        "La actualización ya está pendiente y se ejecutará cuando se desbloquee "
                        "el mercado. " + (reason or "")
                    )
                else:
                    st.info("La actualización está vencida y se ejecutará automáticamente al cargar la aplicación.")
        st.caption(
            "Los porcentajes se calculan sobre el total de managers registrados. "
            "Los precios se redondean a dos decimales. Si todavía no hay managers, "
            "se considera que ningún manager posee al jugador (0 %). "
            "En Streamlit Cloud, la revisión automática se ejecuta cuando la aplicación "
            "vuelve a ejecutarse después de cumplirse los 3 días; no hay un proceso de "
            "fondo garantizado mientras la app está inactiva."
        )
        trend_players = run_query("SELECT id, name, position FROM players ORDER BY name")
        if trend_players:
            trend_options = {f"{row['name']} ({row['position']}) · ID {row['id']}": int(row['id']) for row in trend_players}
            trend_label = st.selectbox("Consultar evolución de precio", list(trend_options.keys()), key="admin_market_history_player")
            render_player_profile(trend_options[trend_label], title="Análisis de mercado")

        st.subheader("Modificar saldo de usuarios")
        u_df = get_dataframe(
            "SELECT id, username, rupias FROM users WHERE role='user' ORDER BY username"
        )
        st.dataframe(u_df, use_container_width=True, hide_index=True)
        with st.form("edit_money_form"):
            c1, c2 = st.columns(2)
            u_id_edit = c1.number_input("ID del manager", min_value=1, step=1)
            new_rup = c2.number_input("Nuevo saldo", min_value=0.0, step=0.5, format="%.2f")
            submitted = st.form_submit_button("Fijar saldo")
        if submitted:
            if _show_operation_error(lambda: adjust_user_money(int(u_id_edit), float(new_rup))):
                st.success("Saldo actualizado.")
                st.rerun()

    with t4:
        st.subheader("⚽ Procesar jornada")
        st.info(
            "Los puntos del manager se conceden únicamente si el futbolista estaba "
            "entre sus titulares al inicio del partido y el manager tenía al menos "
            "4 jugadores alineados en ese momento. Los suplentes no puntúan para el manager."
        )
        pending_df = get_dataframe(
            "SELECT id, jornada, rival, match_date, match_time FROM matches "
            "WHERE status='PENDIENTE' ORDER BY match_date, match_time, id"
        )
        players_df = get_dataframe("SELECT id, name, position FROM players ORDER BY id")
        if pending_df.empty:
            st.info("No hay partidos pendientes.")
        elif players_df.empty:
            st.warning("Añade al menos un jugador para poder procesar una jornada.")
        else:
            match_options = {
                f"ID {int(row['id'])} — {row['jornada']} vs {row['rival']}": int(row["id"])
                for _, row in pending_df.iterrows()
            }
            selected_label = st.selectbox("Partido", list(match_options.keys()), key="process_match_select")
            selected_match_id = match_options[selected_label]
            selected_match = pending_df[pending_df["id"] == selected_match_id].iloc[0]
            try:
                _, _, vote_close = get_voting_window(
                    selected_match["match_date"], selected_match["match_time"]
                )
                process_allowed = now_local() >= vote_close
                if not process_allowed:
                    st.info(
                        "Para evitar que cambien las puntuaciones después del reparto, el partido solo se puede "
                        f"procesar al cerrarse la votación: {vote_close.strftime('%d/%m/%Y %H:%M')} "
                        f"(faltan {format_countdown(vote_close - now_local())})."
                    )
                else:
                    st.caption("La votación está cerrada; puedes registrar las estadísticas definitivas.")
            except (TypeError, ValueError):
                process_allowed = False
                st.error("La fecha u hora de este partido no es válida; corrígela en la base de datos.")

            with st.form(f"stats_form_{selected_match_id}"):
                stats: dict[int, dict[str, Any]] = {}
                for _, player in players_df.iterrows():
                    player_id = int(player["id"])
                    st.write(f"**{player['name']}** ({player['position']})")
                    c1, c2, c3, c4, c5 = st.columns(5)
                    stats[player_id] = {
                        "g": c1.number_input("Gol", min_value=0, value=0, step=1, key=f"g_{selected_match_id}_{player_id}"),
                        "a": c2.number_input("Asistencias", min_value=0, value=0, step=1, key=f"a_{selected_match_id}_{player_id}"),
                        "pc": c3.checkbox("Portería a cero", key=f"pc_{selected_match_id}_{player_id}"),
                        "pp": c4.number_input(
                            "Penalti parado", min_value=0, value=0, step=1,
                            key=f"pp_{selected_match_id}_{player_id}",
                            disabled=player["position"] != "POR",
                        ),
                        "pf": c5.number_input("Penalti fallado", min_value=0, value=0, step=1, key=f"pf_{selected_match_id}_{player_id}"),
                    }
                submitted = st.form_submit_button(
                    "Guardar y repartir puntos", disabled=not process_allowed
                )
            if submitted:
                if _show_operation_error(lambda: process_match(selected_match_id, stats)):
                    st.success("Jornada procesada correctamente.")
                    st.rerun()

        st.subheader("⏪ Revertir jornada")
        finished_df = get_dataframe(
            "SELECT id, jornada, rival FROM matches WHERE status='FINALIZADO' "
            "ORDER BY match_date DESC, match_time DESC, id DESC"
        )
        if finished_df.empty:
            st.info("No hay partidos finalizados que revertir.")
        else:
            reverse_options = {
                f"ID {int(row['id'])} — {row['jornada']} vs {row['rival']}": int(row["id"])
                for _, row in finished_df.iterrows()
            }
            reverse_label = st.selectbox("Partido a revertir", list(reverse_options.keys()), key="reverse_match_select")
            reverse_id = reverse_options[reverse_label]
            st.warning(
                "La reversión resta los puntos del partido tanto a los jugadores como a los managers. "
                "Se conserva la fotografía de plantilla del inicio para que, si reprocesas el partido, "
                "se utilice la alineación original. Las jornadas antiguas sin fotografía no pueden revertirse automáticamente."
            )
            if st.button("⚠️ Revertir los puntos de este partido", key="reverse_match_button"):
                if _show_operation_error(lambda: reverse_match(reverse_id)):
                    st.success("Puntuaciones revertidas; el partido vuelve a estar pendiente.")
                    st.rerun()

        st.divider()
        st.subheader("🧹 Borrar únicamente los puntos de los managers")
        st.caption(
            "Esta operación elimina de la clasificación de managers los puntos de un partido concreto. "
            "NO modifica los puntos acumulados de los futbolistas, las votaciones, el estado del partido "
            "ni la fotografía de las plantillas. Se guarda una copia para poder restaurarlos."
        )
        if finished_df.empty:
            st.info("No hay partidos finalizados disponibles para esta operación.")
        else:
            manager_points_options = {
                f"ID {int(row['id'])} — {row['jornada']} vs {row['rival']}": int(row["id"])
                for _, row in finished_df.iterrows()
            }
            manager_points_label = st.selectbox(
                "Partido cuyos puntos de managers quieres gestionar",
                list(manager_points_options.keys()),
                key="manager_points_match_select",
            )
            manager_points_match_id = manager_points_options[manager_points_label]
            summary = run_query(
                "SELECT COUNT(*) AS rows_count, COUNT(DISTINCT user_id) AS managers_count, "
                "COALESCE(SUM(points_awarded), 0) AS points_total "
                "FROM match_history WHERE match_id=?",
                (manager_points_match_id,),
            )[0]
            active_reset = run_query(
                "SELECT id, reset_at, reset_by, managers_affected, history_rows, points_removed "
                "FROM manager_points_reset_logs WHERE match_id=? AND status='ACTIVE' "
                "ORDER BY id DESC LIMIT 1",
                (manager_points_match_id,),
            )
            info_cols = st.columns(3)
            info_cols[0].metric("Managers con puntos", int(summary["managers_count"]))
            info_cols[1].metric("Registros de puntos", int(summary["rows_count"]))
            info_cols[2].metric("Puntos de managers", f"{float(summary['points_total']):.2f}")

            if active_reset:
                reset = active_reset[0]
                st.warning(
                    "Los puntos de managers de este partido ya están borrados. "
                    f"Operación realizada el {reset['reset_at']} por {reset['reset_by']}. "
                    "Los puntos individuales de los futbolistas permanecen intactos."
                )
                if st.button(
                    "↩️ Restaurar los puntos de los managers",
                    key="restore_manager_points_button",
                ):
                    result = {}
                    if _show_operation_error(
                        lambda: result.update(restore_manager_points_for_match(manager_points_match_id))
                    ):
                        st.success(
                            f"Puntos restaurados para {result['managers_affected']} managers "
                            f"({result['points_restored']:.2f} puntos en total)."
                        )
                        st.rerun()
            elif int(summary["rows_count"]) == 0:
                st.info("Este partido no tiene puntos de managers registrados para borrar.")
            else:
                with st.form("reset_manager_points_form"):
                    st.warning(
                        "Acción administrativa: se eliminarán todos los puntos que este partido "
                        "aportó a los managers. Los puntos de los jugadores no se modificarán."
                    )
                    confirmation = st.text_input(
                        'Escribe BORRAR PUNTOS para confirmar',
                        key="reset_manager_points_confirmation",
                    )
                    reset_submitted = st.form_submit_button("🗑️ Borrar puntos de managers")
                if reset_submitted:
                    if confirmation.strip().upper() != "BORRAR PUNTOS":
                        st.error("Confirmación incorrecta. Escribe exactamente BORRAR PUNTOS.")
                    else:
                        result = {}
                        if _show_operation_error(
                            lambda: result.update(
                                reset_manager_points_for_match(
                                    manager_points_match_id,
                                    str(st.session_state.get("username", ADMIN_USERNAME)),
                                )
                            )
                        ):
                            st.success(
                                f"Se han borrado los puntos de {result['managers_affected']} managers "
                                f"({result['points_removed']:.2f} puntos). "
                                "Los puntos individuales de los jugadores no han cambiado. "
                                "Puedes restaurar los puntos desde este mismo apartado."
                            )
                            st.rerun()

    with t5:
        st.subheader("👥 Cuentas y perfiles registrados")
        st.info(
            "Aquí puedes consultar las cuentas de los managers, sus puntos, saldo y plantilla. "
            "Las contraseñas nunca se muestran porque se almacenan mediante hash; puedes restablecerlas."
        )
        accounts_df = get_dataframe(
            "SELECT u.id AS ID, u.username AS Usuario, u.role AS Rol, "
            "ROUND(u.rupias, 2) AS Rupias, ROUND(u.total_points, 2) AS Puntos, "
            "COUNT(DISTINCT s.player_id) AS Jugadores, "
            "COUNT(DISTINCT r.match_id) AS Partidos_votados "
            "FROM users u "
            "LEFT JOIN user_squads s ON s.user_id=u.id "
            "LEFT JOIN ratings r ON r.user_id=u.id "
            "GROUP BY u.id, u.username, u.role, u.rupias, u.total_points "
            "ORDER BY CASE WHEN u.role='admin' THEN 0 ELSE 1 END, u.username"
        )
        st.dataframe(accounts_df, use_container_width=True, hide_index=True)

        manager_rows = run_query(
            "SELECT id, username FROM users WHERE role='user' ORDER BY username"
        )
        if not manager_rows:
            st.info("Todavía no hay cuentas de managers registradas.")
        else:
            manager_options = {
                f"{row['username']} (ID {row['id']})": int(row["id"])
                for row in manager_rows
            }

            st.subheader("Consultar perfil y plantilla")
            selected_profile_label = st.selectbox(
                "Selecciona un manager",
                list(manager_options.keys()),
                key="admin_profile_select",
            )
            selected_profile_id = manager_options[selected_profile_label]
            profile_df = get_dataframe(
                "SELECT username AS Usuario, ROUND(rupias, 2) AS Rupias, "
                "ROUND(total_points, 2) AS Puntos_totales "
                "FROM users WHERE id=? AND role='user'",
                (selected_profile_id,),
            )
            squad_df = get_dataframe(
                "SELECT p.name AS Jugador, p.position AS Posicion, "
                "ROUND(p.price, 2) AS Precio, "
                "CASE WHEN s.is_starter=1 THEN 'Titular' ELSE 'Suplente' END AS Estado "
                "FROM user_squads s JOIN players p ON p.id=s.player_id "
                "WHERE s.user_id=? ORDER BY s.is_starter DESC, p.position, p.name",
                (selected_profile_id,),
            )
            c1, c2 = st.columns([1, 2])
            with c1:
                st.dataframe(profile_df, use_container_width=True, hide_index=True)
            with c2:
                st.write("**Plantilla actual**")
                if squad_df.empty:
                    st.caption("Este manager todavía no tiene jugadores.")
                else:
                    st.dataframe(squad_df, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("🔑 Restablecer contraseña de un manager")
            st.caption(
                "No es posible consultar la contraseña actual. Introduce una nueva para sustituirla."
            )
            with st.form("reset_manager_password_form"):
                reset_label = st.selectbox(
                    "Cuenta", list(manager_options.keys()), key="reset_password_user_select"
                )
                new_password = st.text_input(
                    "Nueva contraseña", type="password", key="reset_manager_password_new"
                )
                confirm_password = st.text_input(
                    "Repetir nueva contraseña", type="password", key="reset_manager_password_confirm"
                )
                reset_submitted = st.form_submit_button("Guardar nueva contraseña")
            if reset_submitted:
                if new_password != confirm_password:
                    st.error("Las contraseñas no coinciden.")
                elif _show_operation_error(
                    lambda: reset_manager_password(manager_options[reset_label], new_password)
                ):
                    st.success(f"Contraseña actualizada para {reset_label}.")
                    st.rerun()

            st.divider()
            st.subheader("🗑️ Eliminar cuenta de manager")
            st.warning(
                "La eliminación es permanente. También se borrarán su plantilla, sus votos y "
                "su historial individual como manager. Sus puntos dejarán de aparecer en la clasificación. "
                "No se puede eliminar la cuenta de administrador desde aquí."
            )
            with st.form("delete_manager_account_form"):
                delete_label = st.selectbox(
                    "Cuenta que quieres eliminar",
                    list(manager_options.keys()),
                    key="delete_manager_user_select",
                )
                expected_username = delete_label.rsplit(" (ID ", 1)[0]
                typed_username = st.text_input(
                    f"Escribe {expected_username} para confirmar",
                    key="delete_manager_username_confirm",
                )
                delete_submitted = st.form_submit_button("Eliminar definitivamente la cuenta")
            if delete_submitted:
                if typed_username.strip().lower() != expected_username.lower():
                    st.error("El nombre escrito no coincide. No se ha eliminado ninguna cuenta.")
                elif _show_operation_error(
                    lambda: delete_manager_account(manager_options[delete_label])
                ):
                    st.success(f"La cuenta {expected_username} se ha eliminado.")
                    st.rerun()


# ============================================================
# 6. COMPONENTES VISUALES REUTILIZABLES
# ============================================================
def apply_global_styles() -> None:
    """Aplica una identidad visual deportiva sin salir de Streamlit."""
    st.markdown(
        """
        <style>
        .stApp { background: radial-gradient(circle at 12% 0%, #1c3049 0, #0c1728 42%, #09111d 100%); color: #e5edf6; }
        .block-container { max-width: 1480px; padding-top: 1.5rem; padding-bottom: 3rem; }
        .stApp p, .stApp label, .stApp [data-testid="stMarkdownContainer"], .stApp [data-testid="stCaptionContainer"] { color: #e5edf6; }
        h1, h2, h3 { color: #f4f7fb; letter-spacing: -0.025em; }
        .stApp input, .stApp textarea, .stApp [data-baseweb="select"] > div { background-color: #17263b; color: #f8fafc; }
        [data-testid="stMetric"] { background: rgba(25, 42, 63, .88); border: 1px solid rgba(148, 163, 184, .18); padding: 14px 16px; border-radius: 14px; }
        [data-testid="stMetricLabel"] { color: #aebed0; }
        [data-testid="stMetricValue"] { color: #f8fafc; }
        [data-testid="stMetricDelta"] { font-weight: 700; }
        div[data-testid="stVerticalBlockBorderWrapper"] { border-color: rgba(148, 163, 184, .22); border-radius: 16px; background: rgba(17, 30, 47, .68); }
        div.stButton > button, div.stFormSubmitButton > button { border-radius: 10px; font-weight: 650; border: 1px solid rgba(74, 222, 128, .42); transition: all .15s ease-in-out; }
        div.stButton > button:hover, div.stFormSubmitButton > button:hover { border-color: #4ade80; color: #4ade80; transform: translateY(-1px); }
        [data-testid="stSidebar"] { background: #0b1523; border-right: 1px solid rgba(148,163,184,.16); }
        [data-testid="stTabs"] button { font-weight: 650; }
        .fantasy-kicker { color: #86efac; text-transform: uppercase; letter-spacing: .14em; font-size: .72rem; font-weight: 800; }
        .fantasy-subtitle { color: #aebed0; font-size: .94rem; }
        .fantasy-player-name { color: #f8fafc; font-weight: 800; font-size: 1.05rem; }
        .fantasy-muted { color: #aebed0; font-size: .82rem; }
        .fantasy-price { color: #f5d37b; font-size: 1.35rem; font-weight: 800; }
        .fantasy-green { color: #86efac; font-weight: 750; }
        .fantasy-gold { color: #f5d37b; font-weight: 750; }
        @media (max-width: 768px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; }
            [data-testid="stMetric"] { padding: 10px 11px; }
            .fantasy-price { font-size: 1.15rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_player_profile(player_id: int, title: str = "Ficha del jugador") -> None:
    """Muestra ficha y evolución de precio de un jugador."""
    rows = run_query(
        "SELECT id, name, position, price, points FROM players WHERE id=?",
        (int(player_id),),
    )
    if not rows:
        st.info("El jugador seleccionado ya no está disponible.")
        return
    player = rows[0]
    ownership = run_query(
        "SELECT COUNT(DISTINCT s.user_id) AS owners, "
        "(SELECT COUNT(*) FROM users WHERE role='user') AS total "
        "FROM user_squads s JOIN users u ON u.id=s.user_id "
        "WHERE s.player_id=? AND u.role='user'",
        (int(player_id),),
    )[0]
    owners = int(ownership["owners"] or 0)
    total = int(ownership["total"] or 0)
    share = (owners / total * 100) if total else 0.0
    st.markdown(f"#### {title}: {html.escape(str(player['name']))}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Posición", str(player["position"]))
    c2.metric("Precio actual", f"{format_rupias(player['price'])} ₹")
    c3.metric("Puntos acumulados", f"{float(player['points'] or 0):.2f}")
    c4.metric("Managers propietarios", f"{owners}/{total}" if total else "0/0")
    st.caption(f"Demanda actual: {share:.1f} % de los managers")
    history = get_dataframe(
        "SELECT recorded_at AS Fecha, price AS Precio, source AS Origen "
        "FROM market_price_history WHERE player_id=? ORDER BY recorded_at, id",
        (int(player_id),),
    )
    if not history.empty:
        history["Fecha"] = pd.to_datetime(history["Fecha"], errors="coerce")
        history = history.dropna(subset=["Fecha"])
        if len(history) >= 2:
            chart = history.set_index("Fecha")[["Precio"]]
            st.line_chart(chart, use_container_width=True)
        else:
            st.info("Se ha guardado el precio inicial. El gráfico aparecerá cuando exista otra actualización.")
        with st.expander("Ver historial de precios"):
            st.dataframe(
                history.sort_values("Fecha", ascending=False).head(12),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.caption("Todavía no hay movimientos históricos registrados para este jugador.")

    points_history = get_dataframe(
        "SELECT m.jornada || ' · ' || m.rival AS Partido, "
        "ROUND(h.points_awarded, 2) AS Puntos "
        "FROM match_player_history h JOIN matches m ON m.id=h.match_id "
        "WHERE h.player_id=? ORDER BY m.match_date, m.match_time, m.id",
        (int(player_id),),
    )
    if not points_history.empty:
        st.markdown("**Rendimiento por partido**")
        st.bar_chart(points_history.set_index("Partido")[["Puntos"]], use_container_width=True)


def _manager_rank(user_id: int) -> tuple[int, int]:
    rows = run_query(
        "SELECT username, total_points FROM users WHERE id=? AND role='user'",
        (int(user_id),),
    )
    total = int(run_query("SELECT COUNT(*) AS n FROM users WHERE role='user'")[0]["n"])
    if not rows:
        return 0, total
    username = str(rows[0]["username"])
    points = float(rows[0]["total_points"] or 0)
    ahead = int(
        run_query(
            "SELECT COUNT(*) AS n FROM users WHERE role='user' AND "
            "(total_points>? OR (total_points=? AND username<?))",
            (points, points, username),
        )[0]["n"]
    )
    return ahead + 1, total


def _render_dashboard(user_id: int, username: str, rupias: float) -> None:
    st.markdown('<div class="fantasy-kicker">Fantasy Local · Centro de mando</div>', unsafe_allow_html=True)
    st.title(f"¡Bienvenido, {username.capitalize()}!")
    st.markdown('<div class="fantasy-subtitle">Resumen de tu equipo, actividad y próximos acontecimientos.</div>', unsafe_allow_html=True)

    user_stats = run_query(
        "SELECT total_points FROM users WHERE id=? AND role='user'", (int(user_id),)
    )[0]
    points = float(user_stats["total_points"] or 0)
    rank, manager_count = _manager_rank(user_id)
    squad_stats = run_query(
        "SELECT COUNT(*) AS players, COALESCE(SUM(p.price),0) AS squad_value "
        "FROM user_squads s JOIN players p ON p.id=s.player_id WHERE s.user_id=?",
        (int(user_id),),
    )[0]

    metrics = st.columns(4)
    metrics[0].metric("Puntos totales", f"{points:.2f}")
    metrics[1].metric("Saldo disponible", f"{format_rupias(rupias)} ₹")
    metrics[2].metric("Posición", f"{rank}.º" if rank else "—", f"de {manager_count} managers" if manager_count else None)
    metrics[3].metric("Plantilla", f"{int(squad_stats['players'])}/{MAX_SQUAD_SIZE}", f"Valor {format_rupias(squad_stats['squad_value'])} ₹")

    left, right = st.columns([1.35, 1])
    with left:
        st.subheader("📅 Próximo partido")
        next_match = run_query(
            "SELECT id, jornada, rival, match_date, match_time FROM matches "
            "WHERE status='PENDIENTE' ORDER BY match_date, match_time, id LIMIT 1"
        )
        if next_match:
            match = next_match[0]
            try:
                kickoff, opens_at, closes_at = get_voting_window(match["match_date"], match["match_time"])
                now = now_local()
                st.markdown(f"### {html.escape(str(match['jornada']))} · vs {html.escape(str(match['rival']))}")
                st.caption(f"{kickoff.strftime('%d/%m/%Y · %H:%M')} · Hora peninsular española")
                if now < kickoff:
                    st.info(f"El partido comienza en **{format_countdown(kickoff-now)}**.")
                elif now < closes_at:
                    st.warning("Partido iniciado o pendiente de procesar.")
                else:
                    st.warning("El plazo de votación ha terminado. El administrador puede procesar el partido.")
                if now < opens_at:
                    st.caption(f"La votación se abrirá en {format_countdown(opens_at-now)}.")
                elif now < closes_at:
                    st.caption(f"La votación está abierta y cierra en {format_countdown(closes_at-now)}.")
                else:
                    st.caption("La votación está cerrada.")
            except (TypeError, ValueError):
                st.error("La fecha del próximo partido no es válida. Contacta con el administrador.")
        else:
            st.info("Todavía no hay partidos pendientes.")
    with right:
        st.subheader("💹 Próxima actualización del mercado")
        last_update, next_update = get_market_update_schedule()
        if next_update is None:
            st.info("El calendario se inicia con la primera ejecución de la aplicación.")
        else:
            remaining = next_update - now_local()
            if remaining.total_seconds() > 0:
                st.metric("Tiempo restante", format_countdown(remaining))
                st.caption(f"Prevista para {next_update.strftime('%d/%m/%Y · %H:%M')}.")
            else:
                st.warning("La actualización está vencida y se aplicará en la próxima ejecución, si el mercado está desbloqueado.")
            if last_update:
                st.caption(f"Última actualización: {last_update.strftime('%d/%m/%Y · %H:%M')}.")

    st.divider()
    st.subheader("📈 Tu evolución por jornadas")
    points_history = get_dataframe(
        "SELECT m.id, m.jornada || ' · ' || m.rival AS Partido, m.match_date AS Fecha, "
        "ROUND(SUM(h.points_awarded), 2) AS Puntos "
        "FROM match_history h JOIN matches m ON m.id=h.match_id "
        "WHERE h.user_id=? GROUP BY m.id, m.jornada, m.rival, m.match_date, m.match_time "
        "ORDER BY m.match_date, m.match_time, m.id",
        (int(user_id),),
    )
    if points_history.empty:
        st.info("Cuando se procese tu primera jornada, aquí aparecerá la evolución de tus puntos.")
    else:
        st.line_chart(points_history.set_index("Partido")[["Puntos"]], use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🏅 Tus logros")
        achievements: list[str] = []
        if int(squad_stats["players"]) == MAX_SQUAD_SIZE:
            achievements.append("🧩 Plantilla completa")
        if points > 0:
            achievements.append("⭐ Primeros puntos")
        if rank and rank <= 3:
            achievements.append("🏆 En el podio")
        votes = int(run_query("SELECT COUNT(DISTINCT match_id) AS n FROM ratings WHERE user_id=?", (int(user_id),))[0]["n"])
        if votes:
            achievements.append("🗳️ Manager participativo")
        if achievements:
            for achievement in achievements:
                st.success(achievement)
        else:
            st.caption("Tus primeros logros aparecerán a medida que avance la competición.")
    with c2:
        st.subheader("📰 Actividad reciente")
        activity = get_dataframe(
            "SELECT m.jornada AS Jornada, m.rival AS Rival, p.name AS Jugador, "
            "ROUND(h.points_awarded,2) AS Puntos "
            "FROM match_history h JOIN matches m ON m.id=h.match_id "
            "LEFT JOIN players p ON p.id=h.player_id WHERE h.user_id=? "
            "ORDER BY m.match_date DESC, m.match_time DESC, h.id DESC LIMIT 6",
            (int(user_id),),
        )
        if activity.empty:
            st.caption("Todavía no hay actividad puntuable.")
        else:
            st.dataframe(activity, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("🌐 Muro de la competición")
    league_feed = get_dataframe(
        "SELECT u.username AS Manager, m.jornada AS Jornada, m.rival AS Rival, "
        "p.name AS Jugador, ROUND(h.points_awarded,2) AS Puntos "
        "FROM match_history h JOIN users u ON u.id=h.user_id "
        "JOIN matches m ON m.id=h.match_id LEFT JOIN players p ON p.id=h.player_id "
        "ORDER BY m.match_date DESC, m.match_time DESC, h.id DESC LIMIT 10"
    )
    if league_feed.empty:
        st.caption("La actividad de la competición aparecerá cuando se procese la primera jornada.")
    else:
        st.dataframe(league_feed, use_container_width=True, hide_index=True)


# ============================================================
# 7. PANEL DEL MANAGER
# ============================================================
def user_panel() -> None:
    user_id = int(st.session_state["user_id"])
    user_rows = run_query(
        "SELECT username, rupias FROM users WHERE id=? AND role='user'", (user_id,)
    )
    if not user_rows:
        st.session_state.clear()
        st.rerun()
        return
    username, rupias = user_rows[0]["username"], float(user_rows[0]["rupias"])

    locked, locked_match, lock_reason = market_lock_info()
    if locked:
        if lock_reason:
            st.error(f"🔒 {lock_reason} Contacta con el administrador.")
        elif locked_match is not None:
            st.warning(
                f"🔒 {locked_match['jornada']} vs {locked_match['rival']}: mercado y cambios de plantilla cerrados hasta que el administrador procese el partido."
            )

    st.sidebar.markdown("# ⚽ FANTASY LOCAL")
    st.sidebar.caption(f"Manager: **{username.capitalize()}**")
    st.sidebar.metric("Saldo disponible", f"{format_rupias(rupias)} ₹")
    menu = st.sidebar.radio(
        "Navegación",
        ["🏠 Inicio", "📋 Mi Plantilla", "🛒 Mercado", "⭐ Votar", "🏆 Clasificación"],
        key="manager_navigation",
    )

    if menu == "🏠 Inicio":
        _render_dashboard(user_id, username, rupias)

    elif menu == "📋 Mi Plantilla":
        st.markdown('<div class="fantasy-kicker">Tu equipo · Gestión de alineación</div>', unsafe_allow_html=True)
        st.title("📋 Mi plantilla")
        st.info(
            "Para recibir puntos de un futbolista, debes tenerlo como titular al inicio del partido "
            "y contar con al menos 4 titulares en ese mismo instante. Con menos de 4 titulares, "
            "ningún jugador de tu equipo te sumará puntos en ese encuentro."
        )
        squad = get_dataframe(
            "SELECT p.id, p.name, p.position, p.price, p.points, s.is_starter "
            "FROM players p JOIN user_squads s ON p.id=s.player_id "
            "WHERE s.user_id=? ORDER BY s.is_starter DESC, p.position, p.name",
            (user_id,),
        )
        squad_count = len(squad)
        squad_value = float(squad["price"].sum()) if not squad.empty else 0.0
        starter_count = int((squad["is_starter"] == 1).sum()) if not squad.empty else 0
        total_player_points = float(squad["points"].sum()) if not squad.empty else 0.0
        m = st.columns(4)
        m[0].metric("Jugadores", f"{squad_count}/{MAX_SQUAD_SIZE}")
        m[1].metric("Titulares", f"{starter_count}/{MAX_STARTERS}")
        m[2].metric("Valor de plantilla", f"{format_rupias(squad_value)} ₹")
        m[3].metric("Puntos de tus jugadores", f"{total_player_points:.2f}")

        if squad.empty:
            st.info("Tu plantilla está vacía. Visita el mercado para fichar tus primeros jugadores.")
        else:
            starters_df = squad[squad["is_starter"] == 1]
            substitutes_df = squad[squad["is_starter"] == 0]
            st.subheader("🏟️ Alineación titular")
            st.caption("Distribución visual por posición. Los cambios siguen limitados a cuatro titulares.")
            pitch = (
                '<div style="position:relative;width:100%;height:430px;background:linear-gradient(180deg,#23794b,#155b3a);'
                'border:3px solid rgba(255,255,255,.9);border-radius:16px;overflow:hidden;margin-bottom:18px;'
                'box-shadow:inset 0 0 0 1px rgba(255,255,255,.15);">'
                '<div style="position:absolute;inset:50% 0 auto 0;border-top:2px solid rgba(255,255,255,.75);"></div>'
                '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:100px;height:100px;'
                'border:2px solid rgba(255,255,255,.75);border-radius:50%;"></div>'
                '<div style="position:absolute;top:0;left:20%;right:20%;height:16%;border:2px solid rgba(255,255,255,.75);border-top:0;"></div>'
                '<div style="position:absolute;bottom:0;left:20%;right:20%;height:16%;border:2px solid rgba(255,255,255,.75);border-bottom:0;"></div>'
                '<div style="position:absolute;top:0;left:38%;right:38%;height:7%;border:2px solid rgba(255,255,255,.75);border-top:0;"></div>'
                '<div style="position:absolute;bottom:0;left:38%;right:38%;height:7%;border:2px solid rgba(255,255,255,.75);border-bottom:0;"></div>'
            )
            lane_top = {"DEL": 14, "MED": 37, "DEF": 62, "POR": 83}
            shirt_svg = '<svg width="31" height="31" viewBox="0 0 24 24"><path fill="#f8fafc" stroke="#0f172a" stroke-width=".7" d="M12 2C10.3 2 9 3.3 9 5v1H5v7h2v9h10v-9h2V6h-4V5c0-1.7-1.3-3-3-3z"/></svg>'
            for position in ("DEL", "MED", "DEF", "POR"):
                group = starters_df[starters_df["position"] == position]
                for index, (_, player) in enumerate(group.iterrows()):
                    x = (index + 0.5) / len(group) * 100
                    top = lane_top[position]
                    player_name = html.escape(str(player["name"]))
                    pitch += (
                        f'<div style="position:absolute;top:{top}%;left:{x}%;transform:translate(-50%,-50%);'
                        'width:80px;text-align:center;color:#fff;text-shadow:0 1px 3px #07120c;'
                        'display:flex;flex-direction:column;align-items:center;">'
                        f'{shirt_svg}<span style="font-size:10px;background:rgba(3,15,10,.72);padding:2px 6px;border-radius:8px;margin-top:2px;">{position} · {float(player["points"] or 0):.1f} pts</span>'
                        f'<span style="font-size:12px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:80px;">{player_name}</span>'
                        '</div>'
                    )
            pitch += "</div>"
            st.markdown(pitch, unsafe_allow_html=True)

            st.subheader("🪑 Banquillo")
            if substitutes_df.empty:
                st.caption("No tienes jugadores en el banquillo.")
            else:
                bench_cols = st.columns(min(3, len(substitutes_df)))
                for idx, (_, player) in enumerate(substitutes_df.iterrows()):
                    with bench_cols[idx % len(bench_cols)]:
                        with st.container(border=True):
                            st.markdown(f"**{html.escape(str(player['name']))}**")
                            st.caption(f"{player['position']} · {float(player['points'] or 0):.2f} puntos")
                            st.metric("Precio", f"{format_rupias(player['price'])} ₹")

            st.subheader("⚙️ Gestionar jugadores")
            if locked:
                st.info("Los cambios se habilitarán cuando el administrador procese el partido pendiente.")
            else:
                for _, player in squad.iterrows():
                    player_id = int(player["id"])
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([3, 1.5, 1.5])
                        c1.markdown(f"**{html.escape(str(player['name']))}** · {player['position']}")
                        c1.caption(f"{float(player['points'] or 0):.2f} puntos · {format_rupias(player['price'])} rupias")
                        c2.write("🟢 Titular" if bool(player["is_starter"]) else "⚪ Suplente")
                        action_label = "Pasar a suplente" if bool(player["is_starter"]) else "Hacer titular"
                        if c2.button(action_label, key=f"starter_{player_id}"):
                            if _show_operation_error(
                                lambda: set_starter(user_id, player_id, not bool(player["is_starter"]))
                            ):
                                st.rerun()
                        if c3.button("Vender", key=f"sell_{player_id}"):
                            st.session_state["pending_sell_player_id"] = player_id
                            st.rerun()

            pending_sell_id = st.session_state.get("pending_sell_player_id")
            if pending_sell_id:
                pending_player = squad[squad["id"] == int(pending_sell_id)]
                if not pending_player.empty:
                    row = pending_player.iloc[0]
                    st.warning(
                        f"¿Confirmas la venta de {row['name']} por {format_rupias(row['price'])} rupias?"
                    )
                    confirm_col, cancel_col = st.columns(2)
                    if confirm_col.button("Confirmar venta", type="primary", key="confirm_sell"):
                        try:
                            sold_price = sell_player(user_id, int(pending_sell_id))
                            st.session_state.pop("pending_sell_player_id", None)
                            st.success(f"Jugador vendido por {format_rupias(sold_price)} rupias.")
                            st.rerun()
                        except (ValueError, sqlite3.Error) as exc:
                            st.error(str(exc))
                    if cancel_col.button("Cancelar venta", key="cancel_sell"):
                        st.session_state.pop("pending_sell_player_id", None)
                        st.rerun()
                else:
                    st.session_state.pop("pending_sell_player_id", None)

            all_squad_options = {
                f"{row['name']} ({row['position']}) · ID {row['id']}": int(row["id"])
                for _, row in squad.iterrows()
            }
            if all_squad_options:
                with st.expander("📊 Consultar ficha e historial de un jugador"):
                    selected_player = st.selectbox("Jugador", list(all_squad_options.keys()), key="squad_profile_select")
                    render_player_profile(all_squad_options[selected_player])

    elif menu == "🛒 Mercado":
        st.markdown('<div class="fantasy-kicker">Fichajes · Demanda · Revalorización</div>', unsafe_allow_html=True)
        st.title("🛒 Mercado de fichajes")
        schedule_last, schedule_next = get_market_update_schedule()
        if schedule_next is not None:
            remaining = schedule_next - now_local()
            if remaining.total_seconds() > 0:
                st.info(f"⏱️ Próxima actualización del mercado en **{format_countdown(remaining)}**.")
            else:
                st.warning("La actualización del mercado está pendiente de ejecutarse.")
        if locked:
            st.error("🔒 Mercado cerrado hasta que el administrador procese el partido pendiente.")

        all_market_df = get_dataframe(
            "SELECT p.id, p.name, p.position, p.price, p.points, "
            "(SELECT COUNT(DISTINCT s.user_id) FROM user_squads s JOIN users u ON u.id=s.user_id WHERE s.player_id=p.id AND u.role='user') AS owners, "
            "(SELECT COUNT(*) FROM users WHERE role='user') AS total_managers, "
            "(SELECT h.price FROM market_price_history h WHERE h.player_id=p.id "
            "ORDER BY h.id DESC LIMIT 1 OFFSET 1) AS previous_price "
            "FROM players p ORDER BY p.position, p.name"
        )
        if all_market_df.empty:
            st.info("Todavía no hay jugadores registrados en el mercado.")
            return
        all_market_df["ownership_pct"] = all_market_df.apply(
            lambda row: (float(row["owners"]) / float(row["total_managers"]) * 100)
            if float(row["total_managers"]) > 0 else 0.0,
            axis=1,
        )
        market_df = all_market_df[
            ~all_market_df["id"].isin(
                get_dataframe("SELECT player_id FROM user_squads WHERE user_id=?", (user_id,))["player_id"].tolist()
            )
        ].copy()
        top1, top2, top3 = st.columns(3)
        top1.metric("Jugadores disponibles", len(market_df))
        top2.metric("Managers registrados", int(all_market_df["total_managers"].max() or 0))
        top3.metric("Tu saldo", f"{format_rupias(rupias)} ₹")

        st.subheader("🔎 Encuentra tu próximo fichaje")
        f1, f2, f3 = st.columns([2, 1.2, 1.4])
        query = f1.text_input("Buscar futbolista", placeholder="Escribe un nombre...", key="market_search")
        positions = f2.multiselect("Posición", list(VALID_POSITIONS), default=list(VALID_POSITIONS), key="market_positions")
        sort_label = f3.selectbox(
            "Ordenar por", ["Más puntos", "Menor precio", "Mayor precio", "Mayor demanda", "Menor demanda", "Nombre"],
            key="market_sort",
        )
        if not market_df.empty:
            if query.strip():
                market_df = market_df[market_df["name"].str.contains(query.strip(), case=False, na=False, regex=False)]
            market_df = market_df[market_df["position"].isin(positions)]
            sort_map = {
                "Más puntos": ("points", False), "Menor precio": ("price", True),
                "Mayor precio": ("price", False), "Mayor demanda": ("ownership_pct", False),
                "Menor demanda": ("ownership_pct", True), "Nombre": ("name", True),
            }
            sort_col, ascending = sort_map[sort_label]
            market_df = market_df.sort_values(sort_col, ascending=ascending)
            if not market_df.empty:
                low_price = float(market_df["price"].min())
                high_price = float(market_df["price"].max())
                if high_price > low_price:
                    with st.expander("🎚️ Afinar búsqueda por precio"):
                        price_range = st.slider(
                            "Rango de precios (rupias)",
                            min_value=low_price, max_value=high_price,
                            value=(low_price, high_price), step=0.01, format="%.2f",
                            key="market_price_range",
                        )
                    market_df = market_df[market_df["price"].between(price_range[0], price_range[1])]

        if market_df.empty:
            st.info("No hay jugadores que coincidan con los filtros seleccionados.")
        elif locked:
            st.caption("Puedes consultar jugadores, pero los fichajes están deshabilitados mientras el mercado esté cerrado.")
        if not market_df.empty:
            st.caption(f"Mostrando {len(market_df)} futbolistas disponibles.")
            for start_index in range(0, len(market_df), 3):
                card_cols = st.columns(3)
                group = market_df.iloc[start_index:start_index + 3]
                for offset, (_, player) in enumerate(group.iterrows()):
                    player_id = int(player["id"])
                    with card_cols[offset]:
                        with st.container(border=True):
                            st.markdown(f'<div class="fantasy-player-name">{html.escape(str(player["name"]))}</div>', unsafe_allow_html=True)
                            st.caption(f"{player['position']} · ID {player_id}")
                            st.markdown(f'<div class="fantasy-price">{format_rupias(player["price"])} ₹</div>', unsafe_allow_html=True)
                            price_delta = None
                            if pd.notna(player["previous_price"]):
                                price_delta = float(player["price"]) - float(player["previous_price"])
                            m1, m2, m3 = st.columns(3)
                            m1.metric("Puntos", f"{float(player['points'] or 0):.2f}")
                            m2.metric("Propietarios", f"{float(player['ownership_pct']):.1f}%")
                            m3.metric(
                                "Cambio reciente",
                                f"{price_delta:+.2f} ₹" if price_delta is not None else "—",
                                delta_color="normal",
                            )
                            st.progress(min(1.0, max(0.0, float(player["ownership_pct"]) / 100)))
                            if player["ownership_pct"] > 85:
                                st.caption("🔥 Próxima actualización: +7 %")
                            elif player["ownership_pct"] >= 50:
                                st.caption("📈 Próxima actualización: +5 %")
                            elif player["ownership_pct"] >= 25:
                                st.caption("📊 Próxima actualización: +1 %")
                            elif player["owners"] == 0:
                                st.caption("📉 Próxima actualización: −3 %")
                            else:
                                st.caption("📉 Próxima actualización: −1 %")
                            if st.button("Fichar jugador", key=f"buy_{player_id}", disabled=locked):
                                st.session_state["pending_buy_player_id"] = player_id
                                st.rerun()

        pending_buy_id = st.session_state.get("pending_buy_player_id")
        if pending_buy_id:
            pending = all_market_df[all_market_df["id"] == int(pending_buy_id)]
            already_owned = get_dataframe(
                "SELECT 1 AS found FROM user_squads WHERE user_id=? AND player_id=?",
                (user_id, int(pending_buy_id)),
            )
            if pending.empty or not already_owned.empty:
                st.session_state.pop("pending_buy_player_id", None)
            else:
                player = pending.iloc[0]
                st.warning(
                    f"¿Confirmas el fichaje de {player['name']} por {format_rupias(player['price'])} rupias? "
                    f"Tu saldo actual es {format_rupias(rupias)} rupias."
                )
                confirm_col, cancel_col = st.columns(2)
                if confirm_col.button("Confirmar fichaje", type="primary", key="confirm_buy"):
                    try:
                        buy_player(user_id, int(pending_buy_id))
                        st.session_state.pop("pending_buy_player_id", None)
                        st.success(f"Has fichado a {player['name']}.")
                        st.rerun()
                    except (ValueError, sqlite3.Error) as exc:
                        st.error(str(exc))
                if cancel_col.button("Cancelar", key="cancel_buy"):
                    st.session_state.pop("pending_buy_player_id", None)
                    st.rerun()

        with st.expander("📊 Ficha completa e historial de precios"):
            profile_options = {
                f"{row['name']} ({row['position']}) · ID {row['id']}": int(row["id"])
                for _, row in all_market_df.iterrows()
            }
            selected_profile = st.selectbox("Selecciona un futbolista", list(profile_options.keys()), key="market_profile_select")
            render_player_profile(profile_options[selected_profile])

    elif menu == "⭐ Votar":
        st.markdown('<div class="fantasy-kicker">Participa · Valora · Decide</div>', unsafe_allow_html=True)
        st.title("⭐ Votación de jugadores")
        matches_df = get_dataframe(
            "SELECT id, jornada, rival, match_date, match_time, status FROM matches "
            "ORDER BY match_date DESC, match_time DESC, id DESC"
        )
        if matches_df.empty:
            st.info("Todavía no hay partidos registrados.")
            return
        options = {
            f"ID {int(row['id'])} — {row['jornada']} vs {row['rival']} ({row['match_date']})": int(row["id"])
            for _, row in matches_df.iterrows()
        }
        label = st.selectbox("Selecciona el partido", list(options.keys()), key="vote_match_select")
        match_id = options[label]
        match = matches_df[matches_df["id"] == match_id].iloc[0]
        if match["status"] != "PENDIENTE":
            st.error("🔒 Las votaciones de este partido están cerradas: ya ha sido procesado.")
            return
        try:
            _, opens_at, closes_at = get_voting_window(match["match_date"], match["match_time"])
        except (TypeError, ValueError):
            st.error("La fecha u hora del partido no es válida. Contacta con el administrador.")
            return
        now = now_local()
        st.markdown(f"### {html.escape(str(match['jornada']))} · vs {html.escape(str(match['rival']))}")
        if now < opens_at:
            st.warning(f"La votación se abrirá el {opens_at.strftime('%d/%m/%Y a las %H:%M')}.")
            st.progress(0.0, text=f"Se abre en {format_countdown(opens_at-now)}")
            return
        if now >= closes_at:
            st.error("🔒 La ventana de votación ha finalizado.")
            st.progress(1.0, text="Votación cerrada")
            return
        elapsed = (now - opens_at).total_seconds()
        duration = max(1.0, (closes_at - opens_at).total_seconds())
        st.progress(min(1.0, max(0.0, elapsed / duration)), text=f"La votación cierra en {format_countdown(closes_at-now)}")
        previous_votes = run_query(
            "SELECT COUNT(*) AS n FROM ratings WHERE user_id=? AND match_id=?",
            (user_id, match_id),
        )[0]["n"]
        if previous_votes:
            st.success("✅ Ya has enviado tu votación para este partido.")
            return
        vote_players = get_dataframe("SELECT id, name, position FROM players ORDER BY position, name")
        if vote_players.empty:
            st.info("No hay jugadores disponibles para votar.")
            return
        st.caption("Valora a todos los futbolistas del 1 al 5. Solo puedes enviar una votación por partido.")
        with st.form(f"vote_form_{match_id}"):
            vote_inputs: dict[int, int] = {}
            for start_index in range(0, len(vote_players), 2):
                cols = st.columns(2)
                for offset, (_, player) in enumerate(vote_players.iloc[start_index:start_index + 2].iterrows()):
                    with cols[offset]:
                        with st.container(border=True):
                            st.markdown(f"**{html.escape(str(player['name']))}**")
                            st.caption(str(player["position"]))
                            vote_inputs[int(player["id"])] = st.slider(
                                "Valoración", min_value=1, max_value=5, value=3,
                                key=f"vote_{match_id}_{int(player['id'])}",
                            )
            submitted = st.form_submit_button("Enviar todas las valoraciones", type="primary")
        if submitted:
            try:
                submit_ratings(user_id, match_id, vote_inputs)
                st.success("Votación guardada correctamente.")
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(str(exc))

    elif menu == "🏆 Clasificación":
        st.markdown('<div class="fantasy-kicker">La carrera por el liderato</div>', unsafe_allow_html=True)
        st.title("🏆 Clasificación general")
        ranking = get_dataframe(
            "SELECT id, username AS Manager, ROUND(total_points, 2) AS Puntos, "
            "ROUND(rupias, 2) AS Rupias FROM users WHERE role='user' "
            "ORDER BY total_points DESC, username ASC"
        )
        if ranking.empty:
            st.info("Todavía no hay managers registrados.")
            return
        if len(ranking) >= 1:
            st.subheader("🥇 Podio de la competición")
            podium = st.columns(3)
            podium_order = [1, 0, 2]
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            for col, position in zip(podium, podium_order):
                if position < len(ranking):
                    row = ranking.iloc[position]
                    with col:
                        with st.container(border=True):
                            st.markdown(f"## {medals[position]}")
                            st.markdown(f"**{html.escape(str(row['Manager']))}**")
                            st.metric("Puntos", f"{float(row['Puntos']):.2f}")
                            st.caption(f"Posición {position + 1}")
        st.subheader("📊 Todos los managers")
        table = ranking.drop(columns=["id"]).copy()
        table.insert(0, "Posición", range(1, len(table) + 1))
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.bar_chart(ranking.set_index("Manager")[["Puntos"]], use_container_width=True)
        progression = get_dataframe(
            "SELECT u.username AS Manager, m.jornada AS Jornada, m.rival AS Rival, "
            "m.match_date AS Fecha, m.match_time AS Hora, SUM(h.points_awarded) AS Puntos "
            "FROM match_history h JOIN users u ON u.id=h.user_id "
            "JOIN matches m ON m.id=h.match_id "
            "GROUP BY u.id, m.id, m.jornada, m.match_date, m.match_time "
            "ORDER BY m.match_date, m.match_time, m.id"
        )
        if not progression.empty:
            progression["Puntos acumulados"] = progression.groupby("Manager")["Puntos"].cumsum()
            progression["Jornada etiqueta"] = (
                progression["Fecha"].astype(str) + " " + progression["Hora"].astype(str)
                + " · " + progression["Jornada"].astype(str) + " vs " + progression["Rival"].astype(str)
            )
            points_pivot = progression.pivot_table(
                index="Jornada etiqueta", columns="Manager", values="Puntos acumulados", aggfunc="last"
            ).ffill()
            st.subheader("📈 Evolución de puntos acumulados")
            st.line_chart(points_pivot, use_container_width=True)

        latest_mvp = run_query(
            "SELECT m.jornada, m.rival, p.name, h.points_awarded "
            "FROM match_player_history h JOIN matches m ON m.id=h.match_id "
            "JOIN players p ON p.id=h.player_id WHERE m.status='FINALIZADO' "
            "AND m.id=(SELECT id FROM matches WHERE status='FINALIZADO' "
            "ORDER BY match_date DESC, match_time DESC, id DESC LIMIT 1) "
            "ORDER BY h.points_awarded DESC, p.name LIMIT 1"
        )
        if latest_mvp:
            mvp = latest_mvp[0]
            st.success(
                f"🏅 Jugador más destacado de la última jornada procesada: **{mvp['name']}** "
                f"({mvp['jornada']} · vs {mvp['rival']}) — **{float(mvp['points_awarded']):.2f} puntos**."
            )

        if len(ranking) >= 2:
            st.divider()
            st.subheader("⚔️ Duelo entre managers")
            manager_options = {str(row["Manager"]): int(row["id"]) for _, row in ranking.iterrows()}
            d1, d2 = st.columns(2)
            names = list(manager_options.keys())
            manager_a = d1.selectbox("Manager A", names, index=0, key="duel_manager_a")
            default_b = 1 if len(names) > 1 else 0
            manager_b = d2.selectbox("Manager B", names, index=default_b, key="duel_manager_b")
            if manager_a == manager_b:
                st.info("Selecciona dos managers diferentes para comparar sus equipos.")
            else:
                ids = [manager_options[manager_a], manager_options[manager_b]]
                comparison = get_dataframe(
                    "SELECT u.username AS Manager, ROUND(u.total_points,2) AS Puntos, "
                    "ROUND(u.rupias,2) AS Rupias, COUNT(s.player_id) AS Jugadores "
                    "FROM users u LEFT JOIN user_squads s ON s.user_id=u.id "
                    "WHERE u.id IN (?, ?) GROUP BY u.id ORDER BY u.total_points DESC",
                    (ids[0], ids[1]),
                )
                st.dataframe(comparison, use_container_width=True, hide_index=True)
                squad_compare = get_dataframe(
                    "SELECT u.username AS Manager, p.name AS Jugador, p.position AS Posicion, "
                    "ROUND(p.points,2) AS Puntos, CASE WHEN s.is_starter=1 THEN 'Titular' ELSE 'Suplente' END AS Estado "
                    "FROM user_squads s JOIN users u ON u.id=s.user_id JOIN players p ON p.id=s.player_id "
                    "WHERE u.id IN (?, ?) ORDER BY u.username, s.is_starter DESC, p.position",
                    (ids[0], ids[1]),
                )
                st.dataframe(squad_compare, use_container_width=True, hide_index=True)


# ============================================================
# 7. APLICACIÓN PRINCIPAL
# ============================================================
def main() -> None:
    st.set_page_config(page_title="Fantasy Local", page_icon="⚽", layout="wide")
    apply_global_styles()
    try:
        init_db()
    except (sqlite3.Error, ValueError) as exc:
        st.error(f"No se pudo inicializar la base de datos: {exc}")
        if isinstance(exc, ValueError) and ADMIN_PASSWORD_ENV in str(exc):
            st.info(
                "Configura la variable de entorno FANTASY_ADMIN_PASSWORD con una contraseña "
                "de al menos 12 caracteres y reinicia Streamlit."
            )
        st.stop()

    market_scheduler_monitor()

    if MADRID_TZ is None:
        st.warning(
            "No se ha encontrado la base de zonas horarias. Instala las dependencias de "
            "requirements.txt para calcular las horas con Europe/Madrid."
        )

    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False

    if not st.session_state["logged_in"]:
        st.markdown('<div class="fantasy-kicker">La competición empieza aquí</div>', unsafe_allow_html=True)
        st.title("⚽ Fantasy Local")
        st.markdown('<div class="fantasy-subtitle">Construye tu plantilla · Gestiona tu presupuesto · Compite por el liderato</div>', unsafe_allow_html=True)
        if admin_setup_needed():
            st.warning(
                f"El administrador no está configurado de forma segura. Define la variable "
                f"{ADMIN_PASSWORD_ENV} con una contraseña de al menos 12 caracteres y reinicia la aplicación. "
                "No se crea ni se acepta una contraseña predeterminada."
            )
        tab_login, tab_register = st.tabs(["Entrar", "Registrarse"])
        with tab_login:
            with st.form("login_form"):
                login_name = st.text_input("Usuario")
                login_password = st.text_input("Contraseña", type="password")
                submitted = st.form_submit_button("Acceder")
            if submitted:
                login_user(login_name, login_password)
        with tab_register:
            with st.form("register_form"):
                register_name = st.text_input("Nuevo usuario")
                register_password = st.text_input("Nueva contraseña", type="password")
                submitted = st.form_submit_button("Crear cuenta")
            if submitted:
                register_user(register_name, register_password)
    else:
        # Comprueba que el usuario sigue existiendo y evita sesiones huérfanas.
        current_user = run_query(
            "SELECT id, username, role FROM users WHERE id=?",
            (int(st.session_state.get("user_id", -1)),),
        )
        if not current_user:
            st.session_state.clear()
            st.rerun()
            return
        # El rol se vuelve a leer de la base de datos; no se confía en un rol antiguo de sesión.
        st.session_state["role"] = current_user[0]["role"]
        st.session_state["username"] = current_user[0]["username"]

        if st.sidebar.button("Cerrar sesión"):
            st.session_state.clear()
            st.rerun()
        if st.session_state["role"] == "admin":
            admin_panel()
        else:
            user_panel()


if __name__ == "__main__":
    main()


