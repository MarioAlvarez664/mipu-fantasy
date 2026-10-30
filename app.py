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
MARKET_UPDATE_INTERVAL_DAYS = 4
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS market_updates (
                update_key TEXT PRIMARY KEY,
                executed_at TEXT NOT NULL
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
    """Actualiza el mercado al iniciar/ejecutar la app si han pasado cuatro días.

    Streamlit Community Cloud no mantiene procesos de fondo cuando la app está inactiva.
    Por ello, la revisión se realiza en cada ejecución de la aplicación y la actualización
    se aplica en la primera visita/ejecución después de cumplirse el plazo.
    """
    with db_transaction() as conn:
        now = now_local()
        last_row = conn.execute(
            "SELECT executed_at FROM market_updates ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()

        # Primera ejecución de esta instalación: inicia el contador de cuatro días.
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
                    "SELECT COUNT(DISTINCT user_id) FROM user_squads WHERE player_id=?",
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
        conn.execute(
            "INSERT INTO players(name, position, price, points) VALUES (?, ?, ?, 0)",
            (clean_name, position, float(amount)),
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
        if abs(float(player["points"] or 0)) > 0.0001 or has_history or has_final_rating or has_user_history:
            raise ValueError(
                "No se puede borrar un jugador con puntuaciones o historial de partidos. "
                "Conserva el registro para poder revertir jornadas correctamente."
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
    """Calcula y guarda la jornada completa dentro de una única transacción."""
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

            starters = conn.execute(
                "SELECT s.user_id FROM user_squads s JOIN users u ON u.id=s.user_id "
                "WHERE s.player_id=? AND s.is_starter=1 AND u.role='user'",
                (player_id,),
            ).fetchall()
            for starter in starters:
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
        conn.execute(
            "UPDATE matches SET status='PENDIENTE' WHERE id=?", (int(match_id),)
        )


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
    st.header("👑 Panel de Control — Administrador")
    t1, t2, t3, t4 = st.tabs(
        ["📅 Partidos", "⚽ Jugadores", "💸 Economía y Mercado", "📊 Puntuaciones"]
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
            "El mercado se actualiza automáticamente cada 4 días. La variación depende del "
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
            "vuelve a ejecutarse después de cumplirse los 4 días; no hay un proceso de "
            "fondo garantizado mientras la app está inactiva."
        )

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
                "El mercado y las plantillas vuelven a quedar sujetos al estado de partido pendiente."
            )
            if st.button("⚠️ Revertir los puntos de este partido", key="reverse_match_button"):
                if _show_operation_error(lambda: reverse_match(reverse_id)):
                    st.success("Puntuaciones revertidas; el partido vuelve a estar pendiente.")
                    st.rerun()


# ============================================================
# 6. PANEL DEL MANAGER
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
    next_matches = get_dataframe(
        "SELECT id, jornada, rival, match_date, match_time FROM matches "
        "WHERE status='PENDIENTE' ORDER BY match_date, match_time, id LIMIT 1"
    )
    if locked:
        if lock_reason:
            st.error(f"🔒 {lock_reason} Contacta con el administrador.")
        elif locked_match is not None:
            st.error(
                f"🔒 **PARTIDO PENDIENTE DE PROCESAR**: {locked_match['jornada']} vs "
                f"{locked_match['rival']}. Plantilla y mercado cerrados hasta procesarlo."
            )
    elif not next_matches.empty:
        upcoming = next_matches.iloc[0]
        st.info(
            f"⚽ **Próximo partido:** {upcoming['jornada']} vs **{upcoming['rival']}** | "
            f"{upcoming['match_date']} a las {upcoming['match_time']}"
        )

    st.sidebar.title(f"👤 {username.capitalize()}")
    st.sidebar.subheader(f"💰 Rupias: {format_rupias(rupias)}")
    menu = st.sidebar.radio(
        "Menú", ["📋 Mi Plantilla", "🛒 Mercado", "⭐ Votar", "🏆 Clasificación"]
    )

    if menu == "📋 Mi Plantilla":
        st.header("Mi plantilla")
        squad = get_dataframe(
            "SELECT p.id, p.name, p.position, p.price, p.points, s.is_starter "
            "FROM players p JOIN user_squads s ON p.id=s.player_id "
            "WHERE s.user_id=? ORDER BY s.is_starter DESC, p.position, p.name",
            (user_id,),
        )
        if squad.empty:
            st.info("Ve al mercado para fichar jugadores.")
            return

        starters_df = squad[squad["is_starter"] == 1]
        substitutes_df = squad[squad["is_starter"] == 0]
        shirt_svg = (
            '<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">'
            '<path fill="#000080" d="M12 2C10.3 2 9 3.3 9 5v1H5v7h2v9h10v-9h2V6h-4V5c0-1.7-1.3-3-3-3z"/>'
            "</svg>"
        )
        st.markdown("### 🏟️ Titulares")
        pitch = (
            '<div style="position:relative;width:100%;height:350px;background:#2e8b57;'
            'border:4px solid white;border-bottom:none;border-top-left-radius:15px;'
            'border-top-right-radius:15px;overflow:hidden;margin-bottom:20px;">'
            '<div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:white;"></div>'
            '<div style="position:absolute;bottom:-40px;left:50%;transform:translateX(-50%);'
            'width:80px;height:80px;border:3px solid white;border-radius:50%;"></div>'
            '<div style="position:absolute;top:0;left:20%;right:20%;height:30%;border:3px solid white;border-top:none;"></div>'
            '<div style="position:absolute;top:0;left:38%;right:38%;height:12%;border:3px solid white;border-top:none;"></div>'
        )
        positions_css = [
            "top:20px;left:20px;",
            "top:20px;right:20px;",
            "bottom:30px;left:20px;",
            "bottom:30px;right:20px;",
        ]
        for idx, (_, player) in enumerate(starters_df.iterrows()):
            css = positions_css[idx] if idx < len(positions_css) else "top:45%;left:45%;"
            position_text = html.escape(str(player["position"]))
            player_name = html.escape(str(player["name"]))
            pitch += (
                f'<div style="position:absolute;{css}width:80px;text-align:center;color:white;'
                'text-shadow:1px 1px 3px black;display:flex;flex-direction:column;align-items:center;">'
                f"{shirt_svg}"
                f'<div style="font-size:10px;background:rgba(0,0,0,.5);padding:2px 4px;border-radius:4px;margin-top:2px;">{position_text}</div>'
                f'<div style="font-size:12px;font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;">{player_name}</div>'
                "</div>"
            )
        st.markdown(pitch + "</div>", unsafe_allow_html=True)

        if not substitutes_df.empty:
            bench = (
                '<div style="background:#e0e0e0;padding:10px;border-radius:10px;text-align:center;">'
                '<h5>🪑 BANQUILLO</h5><div style="display:flex;justify-content:center;gap:15px;flex-wrap:wrap;">'
            )
            for _, player in substitutes_df.iterrows():
                position_text = html.escape(str(player["position"]))
                player_name = html.escape(str(player["name"]))
                bench += (
                    f'<div style="text-align:center;">{shirt_svg}<br>'
                    f'<span style="font-size:10px;color:black;font-weight:bold;">{position_text}</span><br>'
                    f'<span style="color:black;font-size:12px;">{player_name}</span></div>'
                )
            st.markdown(bench + "</div></div>", unsafe_allow_html=True)

        st.markdown("### Gestionar plantilla")
        if locked:
            st.error("No se pueden realizar cambios mientras haya un partido pendiente de procesar.")
        else:
            for _, player in squad.iterrows():
                player_id = int(player["id"])
                c1, c2, c3 = st.columns([3, 2, 2])
                c1.write(f"{player['name']} ({player['position']})")
                c2.write("Titular" if bool(player["is_starter"]) else "Suplente")
                action_label = "Pasar a suplente" if bool(player["is_starter"]) else "Hacer titular"
                if c2.button(action_label, key=f"starter_{player_id}"):
                    if _show_operation_error(
                        lambda: set_starter(user_id, player_id, not bool(player["is_starter"]))
                    ):
                        st.rerun()
                if c3.button(f"Vender ({format_rupias(player['price'])} rupias)", key=f"sell_{player_id}"):
                    try:
                        price = sell_player(user_id, player_id)
                        st.success(f"Jugador vendido por {format_rupias(price)} rupias.")
                        st.rerun()
                    except (ValueError, sqlite3.Error) as exc:
                        st.error(str(exc))

    elif menu == "🛒 Mercado":
        st.header("Mercado de fichajes")
        if locked:
            st.error("🔒 Mercado cerrado hasta que el administrador procese el partido pendiente.")
            return
        market_df = get_dataframe(
            "SELECT id, name, position, price, points FROM players "
            "WHERE id NOT IN (SELECT player_id FROM user_squads WHERE user_id=?) "
            "ORDER BY position, name",
            (user_id,),
        )
        if market_df.empty:
            st.info("No hay jugadores disponibles para fichar.")
        else:
            for _, player in market_df.iterrows():
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.write(f"**{player['name']}** ({player['position']}) — Puntos: {round(float(player['points']), 2)}")
                c2.write(f"🪙 {format_rupias(player['price'])}")
                if c3.button("Fichar", key=f"buy_{int(player['id'])}"):
                    try:
                        buy_player(user_id, int(player["id"]))
                        st.success(f"Has fichado a {player['name']}.")
                        st.rerun()
                    except (ValueError, sqlite3.Error) as exc:
                        st.error(str(exc))

    elif menu == "⭐ Votar":
        st.header("Votación")
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
        label = st.selectbox("Partido", list(options.keys()), key="vote_match_select")
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
        if now < opens_at:
            st.warning(f"⏳ La votación abre el {opens_at.strftime('%d/%m/%Y a las %H:%M')}.")
            st.caption(f"Tiempo restante: {format_countdown(opens_at - now)}")
            return
        if now >= closes_at:
            st.error("🔒 La ventana de votación ha finalizado.")
            return
        previous_votes = run_query(
            "SELECT COUNT(*) AS n FROM ratings WHERE user_id=? AND match_id=?",
            (user_id, match_id),
        )[0]["n"]
        if previous_votes:
            st.success("Ya has votado en este partido.")
            st.caption(f"La votación cierra el {closes_at.strftime('%d/%m/%Y a las %H:%M')}.")
            return

        vote_players = get_dataframe("SELECT id, name, position FROM players ORDER BY position, name")
        if vote_players.empty:
            st.info("No hay jugadores disponibles para votar.")
            return
        with st.form(f"vote_form_{match_id}"):
            vote_inputs = {
                int(player["id"]): st.slider(
                    f"{player['name']} ({player['position']})", min_value=1, max_value=5,
                    value=3, key=f"vote_{match_id}_{int(player['id'])}"
                )
                for _, player in vote_players.iterrows()
            }
            submitted = st.form_submit_button("Enviar votación")
        if submitted:
            try:
                submit_ratings(user_id, match_id, vote_inputs)
                st.success("Votación guardada.")
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(str(exc))

    elif menu == "🏆 Clasificación":
        st.header("🏆 Clasificación general")
        ranking = get_dataframe(
            "SELECT username AS Manager, ROUND(total_points, 2) AS 'Puntos Totales' "
            "FROM users WHERE role='user' ORDER BY total_points DESC, username ASC"
        )
        if ranking.empty:
            st.info("Todavía no hay managers registrados.")
        else:
            ranking.index = range(1, len(ranking) + 1)
            ranking.index.name = "Posición"
            st.table(ranking)


# ============================================================
# 7. APLICACIÓN PRINCIPAL
# ============================================================
def main() -> None:
    st.set_page_config(page_title="Fantasy Local", page_icon="⚽", layout="wide")
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

    try:
        market_was_updated, market_update_message = automatic_market_update_if_due()
        if market_was_updated:
            st.session_state["market_update_notice"] = market_update_message
    except (sqlite3.Error, ValueError) as exc:
        st.warning(f"No se pudo comprobar/actualizar automáticamente el mercado: {exc}")

    if st.session_state.get("market_update_notice"):
        st.success(st.session_state.pop("market_update_notice"))

    if MADRID_TZ is None:
        st.warning(
            "No se ha encontrado la base de zonas horarias. Instala las dependencias de "
            "requirements.txt para calcular las horas con Europe/Madrid."
        )

    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False

    if not st.session_state["logged_in"]:
        st.title("⚽ Fantasy Local")
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
