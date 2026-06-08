import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID = int(os.environ["CHAT_ID"])

AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Database ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS fechas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            evento TEXT NOT NULL,
            horario TEXT,
            material TEXT
        )
    """)
    # Migración: agregar columna horario si no existe (para DBs existentes)
    try:
        c.execute("ALTER TABLE fechas ADD COLUMN horario TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # La columna ya existe

    c.execute("""
        CREATE TABLE IF NOT EXISTS feriados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS planes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            contenido TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rutina_modificada (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            descripcion TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rutina_permanente (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia TEXT NOT NULL,
            descripcion TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS comidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia TEXT NOT NULL,
            momento TEXT NOT NULL,
            descripcion TEXT NOT NULL,
            hora_recordatorio TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins_comidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            momento TEXT,
            descripcion TEXT,
            cumplido TEXT,
            nota TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins_tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            tarea TEXT,
            proyecto TEXT,
            cumplido TEXT,
            nota TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tareas_reprogramadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_original TEXT,
            fecha_nueva TEXT,
            tarea TEXT,
            proyecto TEXT,
            deadline TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS plan_semanal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            semana_inicio TEXT,
            generado_en TEXT,
            plan_json TEXT
        )
    """)

    conn.commit()
    conn.close()

def get_fechas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, fecha, evento, horario, material FROM fechas "
        "WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date('now', 'localtime') "
        "ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
    )
    rows = c.fetchall()
    conn.close()
    return rows

def save_fecha(fecha: str, evento: str, horario: str, material: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO fechas (fecha, evento, horario, material) VALUES (?, ?, ?, ?)",
        (fecha, evento, horario or None, material or None)
    )
    conn.commit()
    conn.close()

def delete_fecha_by_index(index: int) -> bool:
    rows = get_fechas()
    if index < 1 or index > len(rows):
        return False
    row_id = rows[index - 1][0]
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM fechas WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True

def _format_fechas_para_prompt(rows) -> str:
    """Formatea las fechas para incluir en el prompt de Claude."""
    if not rows:
        return "No hay fechas cargadas."
    lines = []
    for r in rows:
        # r = (id, fecha, evento, horario, material)
        linea = f"- {r[1]}: {r[2]}"
        if r[3]:  # horario
            linea += f" - Horario: {r[3]}"
        if r[4]:  # material
            linea += f" - Material: {r[4]}"
        lines.append(linea)
    return "\n".join(lines)

def save_feriado(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO feriados (fecha) VALUES (?)", (fecha,))
    conn.commit()
    conn.close()

def get_feriados():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id, fecha FROM feriados ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)")
    rows = c.fetchall()
    conn.close()
    return rows

def delete_feriado_by_index(index: int) -> bool:
    rows = get_feriados()
    if index < 1 or index > len(rows):
        return False
    row_id = rows[index - 1][0]
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM feriados WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True

def is_feriado(fecha: str) -> bool:
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM feriados WHERE fecha = ?", (fecha,))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_plan(fecha: str, contenido: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO planes (fecha, contenido) VALUES (?, ?)", (fecha, contenido))
    conn.commit()
    conn.close()

def get_plan(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT contenido FROM planes WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_rutina_modificada(fecha: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO rutina_modificada (fecha, descripcion) VALUES (?, ?)",
        (fecha, descripcion),
    )
    conn.commit()
    conn.close()

def get_rutina_modificada(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT descripcion FROM rutina_modificada WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_all_rutinas_modificadas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT fecha, descripcion FROM rutina_modificada ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
    )
    rows = c.fetchall()
    conn.close()
    return rows

def delete_rutina_modificada(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM rutina_modificada WHERE fecha = ?", (fecha,))
    conn.commit()
    conn.close()

def save_rutina_permanente(dia: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT INTO rutina_permanente (dia, descripcion) VALUES (?, ?)", (dia, descripcion))
    conn.commit()
    conn.close()

def get_rutinas_permanentes():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id, dia, descripcion FROM rutina_permanente ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def delete_rutina_permanente_by_id(row_id: int):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM rutina_permanente WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

# ── Dieta / Comidas ───────────────────────────────────────────────────────────


def get_plan_semanal(semana_inicio: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT plan_json, generado_en FROM plan_semanal WHERE semana_inicio = ? ORDER BY id DESC LIMIT 1", (semana_inicio,))
    row = c.fetchone()
    conn.close()
    return row

def save_plan_semanal(semana_inicio: str, plan_json_str: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    generado_en = datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M")
    c.execute("INSERT INTO plan_semanal (semana_inicio, generado_en, plan_json) VALUES (?, ?, ?)",
              (semana_inicio, generado_en, plan_json_str))
    conn.commit()
    conn.close()

def _lunes_semana_actual() -> str:
    ahora = datetime.now(AR_TZ)
    lunes = ahora - timedelta(days=ahora.weekday())
    return lunes.strftime("%Y-%m-%d")

def _lunes_semana_proxima() -> str:
    ahora = datetime.now(AR_TZ)
    weekday = ahora.weekday()
    if weekday == 6:
        days = 1
    else:
        days = 7 - weekday
    return (ahora + timedelta(days=days)).strftime("%Y-%m-%d")


COMIDAS_BASE = [
    # LUNES (día de fútbol)
    ("lunes", "desayuno", "2 huevos revueltos + 2 tostadas de pan integral + vaso de leche o yogur natural", "07:45"),
    ("lunes", "media_mañana", "Fruta (banana o manzana) + puñado de maní sin sal 🥜", "10:30"),
    ("lunes", "almuerzo", "Pollo o carne a la plancha + arroz o papas + ensalada. Evitar frituras.", "13:00"),
    ("lunes", "merienda", "Pre-fútbol: 2 tostadas con mantequilla de maní o queso + fruta 🍌 (comer 1h antes del entreno)", "17:15"),
    ("lunes", "cena", "Milanesa al horno o pollo + puré o arroz + ensalada", "21:00"),
    # MARTES (día de gym)
    ("martes", "desayuno", "Avena cocida con leche + banana + 2 huevos duros 🥚", "07:45"),
    ("martes", "media_mañana", "Yogur natural + granola o cereales sin azúcar", "10:30"),
    ("martes", "almuerzo", "Pasta con salsa de tomate y carne picada o atún. O arroz con pollo.", "13:00"),
    ("martes", "merienda", "Pre-gym: 2 tostadas con queso fresco o mantequilla de maní + fruta 🍌", "17:15"),
    ("martes", "cena", "Post-gym 💪: Pollo a la plancha o pescado + lentejas o porotos + ensalada", "21:00"),
    # MIÉRCOLES (día de gym)
    ("miércoles", "desayuno", "3 huevos revueltos + 2 tostadas pan integral + jugo de naranja natural 🍊", "07:45"),
    ("miércoles", "media_mañana", "Fruta + puñado de nueces o almendras", "10:30"),
    ("miércoles", "almuerzo", "Carne o pollo + carbohidrato (arroz, pasta, papa) + verdura", "13:00"),
    ("miércoles", "merienda", "Pre-gym: 2 tostadas con queso + banana 🍌", "17:15"),
    ("miércoles", "cena", "Bifes o pescado a la plancha + arroz integral + ensalada verde 🥗", "21:00"),
    # JUEVES (día de fútbol)
    ("jueves", "desayuno", "2 huevos revueltos + 2 tostadas de pan integral + vaso de leche o yogur natural", "07:45"),
    ("jueves", "media_mañana", "Banana + maní sin sal", "10:30"),
    ("jueves", "almuerzo", "Énfasis en carbohidratos: pasta, arroz o papa + proteína magra", "13:00"),
    ("jueves", "merienda", "Pre-fútbol: 2 tostadas con mantequilla de maní o queso + fruta 🍌 (comer 1h antes del entreno)", "17:15"),
    ("jueves", "cena", "Pollo o carne + pasta o arroz + ensalada", "21:00"),
    # VIERNES (día de tenis)
    ("viernes", "desayuno", "Avena con leche y fruta 🥣", "07:45"),
    ("viernes", "media_mañana", "Yogur + fruta", "10:30"),
    ("viernes", "almuerzo", "Variedad proteica (atún, pollo, huevo) + carbohidrato + ensalada", "13:00"),
    ("viernes", "merienda", "Pre-tenis: liviana — fruta + 1 tostada 🎾", "17:00"),
    ("viernes", "cena", "Libre — cena familiar, comer bien pero sin excederse 🍽️", "21:00"),
    # SÁBADO (partido de fútbol 12:00)
    ("sábado", "desayuno", "Avena + leche + banana + 2 huevos ⚽ (comida importante pre-partido)", "09:00"),
    ("sábado", "media_mañana", "Fruta o tostada ligera (antes del partido)", "10:30"),
    ("sábado", "almuerzo", "Post-partido: pollo/carne + arroz/papa — recuperación 💪", "14:30"),
    ("sábado", "merienda", "Yogur o fruta", "17:00"),
    ("sábado", "cena", "Lo que haga la familia, priorizando proteína y verdura", "21:00"),
    # DOMINGO (gym 11:00)
    ("domingo", "desayuno", "Avena + leche + banana + 2 huevos 🏋️ (pre-gym)", "09:30"),
    ("domingo", "almuerzo", "Post-gym: proteína + carbohidrato + verdura", "13:00"),
    ("domingo", "merienda", "Merienda liviana — yogur o fruta", "17:00"),
    ("domingo", "cena", "Cena familiar nutritiva 🍽️", "21:00"),
]


rt os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID = int(os.environ["CHAT_ID"])

AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Database ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS fechas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            evento TEXT NOT NULL,
            horario TEXT,
            material TEXT
        )
    """)
    # Migración: agregar columna horario si no existe (para DBs existentes)
    try:
        c.execute("ALTER TABLE fechas ADD COLUMN horario TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # La columna ya existe

    c.execute("""
        CREATE TABLE IF NOT EXISTS feriados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS planes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            contenido TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rutina_modificada (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            descripcion TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rutina_permanente (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia TEXT NOT NULL,
            descripcion TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS comidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia TEXT NOT NULL,
            momento TEXT NOT NULL,
            descripcion TEXT NOT NULL,
            hora_recordatorio TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins_comidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            momento TEXT,
            descripcion TEXT,
            cumplido TEXT,
            nota TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins_tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            tarea TEXT,
            proyecto TEXT,
            cumplido TEXT,
            nota TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tareas_reprogramadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_original TEXT,
            fecha_nueva TEXT,
            tarea TEXT,
            proyecto TEXT,
            deadline TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS plan_semanal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            semana_inicio TEXT,
            generado_en TEXT,
            plan_json TEXT
        )
    """)

    conn.commit()
    conn.close()

def get_fechas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, fecha, evento, horario, material FROM fechas "
        "WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date('now', 'localtime') "
        "ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
    )
    rows = c.fetchall()
    conn.close()
    return rows

def save_fecha(fecha: str, evento: str, horario: str, material: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO fechas (fecha, evento, horario, material) VALUES (?, ?, ?, ?)",
        (fecha, evento, horario or None, material or None)
    )
    conn.commit()
    conn.close()

def delete_fecha_by_index(index: int) -> bool:
    rows = get_fechas()
    if index < 1 or index > len(rows):
        return False
    row_id = rows[index - 1][0]
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM fechas WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True

def _format_fechas_para_prompt(rows) -> str:
    """Formatea las fechas para incluir en el prompt de Claude."""
    if not rows:
        return "No hay fechas cargadas."
    lines = []
    for r in rows:
        # r = (id, fecha, evento, horario, material)
        linea = f"- {r[1]}: {r[2]}"
        if r[3]:  # horario
            linea += f" - Horario: {r[3]}"
        if r[4]:  # material
            linea += f" - Material: {r[4]}"
        lines.append(linea)
    return "\n".join(lines)

def save_feriado(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO feriados (fecha) VALUES (?)", (fecha,))
    conn.commit()
    conn.close()

def get_feriados():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id, fecha FROM feriados ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)")
    rows = c.fetchall()
    conn.close()
    return rows

def delete_feriado_by_index(index: int) -> bool:
    rows = get_feriados()
    if index < 1 or index > len(rows):
        return False
    row_id = rows[index - 1][0]
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM feriados WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True

def is_feriado(fecha: str) -> bool:
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM feriados WHERE fecha = ?", (fecha,))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_plan(fecha: str, contenido: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO planes (fecha, contenido) VALUES (?, ?)", (fecha, contenido))
    conn.commit()
    conn.close()

def get_plan(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT contenido FROM planes WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_rutina_modificada(fecha: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO rutina_modificada (fecha, descripcion) VALUES (?, ?)",
        (fecha, descripcion),
    )
    conn.commit()
    conn.close()

def get_rutina_modificada(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT descripcion FROM rutina_modificada WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_all_rutinas_modificadas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT fecha, descripcion FROM rutina_modificada ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
    )
    rows = c.fetchall()
    conn.close()
    return rows

def delete_rutina_modificada(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM rutina_modificada WHERE fecha = ?", (fecha,))
    conn.commit()
    conn.close()

def save_rutina_permanente(dia: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT INTO rutina_permanente (dia, descripcion) VALUES (?, ?)", (dia, descripcion))
    conn.commit()
    conn.close()

def get_rutinas_permanentes():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id, dia, descripcion FROM rutina_permanente ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def delete_rutina_permanente_by_id(row_id: int):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM rutina_permanente WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

# ── Dieta / Comidas ───────────────────────────────────────────────────────────


def get_plan_semanal(semana_inicio: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT plan_json, generado_en FROM plan_semanal WHERE semana_inicio = ? ORDER BY id DESC LIMIT 1", (semana_inicio,))
    row = c.fetchone()
    conn.close()
    return row

def save_plan_semanal(semana_inicio: str, plan_json_str: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    generado_en = datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M")
    c.execute("INSERT INTO plan_semanal (semana_inicio, generado_en, plan_json) VALUES (?, ?, ?)",
              (semana_inicio, generado_en, plan_json_str))
    conn.commit()
    conn.close()

def _lunes_semana_actual() -> str:
    ahora = datetime.now(AR_TZ)
    lunes = ahora - timedelta(days=ahora.weekday())
    return lunes.strftime("%Y-%m-%d")

def _lunes_semana_proxima() -> str:
    ahora = datetime.now(AR_TZ)
    weekday = ahora.weekday()
    if weekday == 6:
        days = 1
    else:
        days = 7 - weekday
    return (ahora + timedelta(days=days)).strftime("%Y-%m-%d")


COMIDAS_BASE = [
    # Lunes
    ("lunes", "desayuno", "3 huevos revueltos + 2 tostadas integrales + banana + vaso de leche", "07:30"),
    ("lunes", "almuerzo", "Milanesa con puré o arroz + fruta", "11:50"),
    ("lunes", "merienda", "2 tostadas con manteca de maní + jugo natural", "17:00"),
    ("lunes", "cena", "Pollo a la plancha + arroz + ensalada", "21:00"),
    # Martes
    ("martes", "desayuno", "Avena con leche + 2 huevos + fruta", "07:30"),
    ("martes", "almuerzo", "Pasta con salsa bolognesa + ensalada", "11:50"),
    ("martes", "merienda", "Yogur griego + banana", "17:00"),
    ("martes", "cena", "Carne picada magra + lentejas o papa + verdura", "21:00"),
    # Miércoles
    ("miércoles", "desayuno", "3 huevos revueltos + 2 tostadas integrales + vaso de leche", "07:30"),
    ("miércoles", "almuerzo", "Pollo con arroz o fideos + fruta", "11:50"),
    ("miércoles", "merienda", "2 tostadas con queso + jugo natural", "17:00"),
    ("miércoles", "cena", "Pescado al horno + puré de papa + ensalada", "21:00"),
    # Jueves
    ("jueves", "desayuno", "Avena con leche + banana + 2 huevos", "07:30"),
    ("jueves", "almuerzo", "Milanesa con ensalada + fruta", "11:50"),
    ("jueves", "merienda", "Banana + puñado de nueces", "17:00"),
    ("jueves", "cena", "Pollo o carne + arroz + verdura salteada", "21:00"),
    # Viernes
    ("viernes", "desayuno", "3 huevos + tostadas integrales + leche", "07:30"),
    ("viernes", "almuerzo", "Priorizar proteína — lo que haya en el colegio", "11:50"),
    ("viernes", "merienda", "Yogur + fruta", "17:00"),
    ("viernes", "cena", "Pasta con atún o pollo + ensalada", "21:00"),
    # Sábado
    ("sábado", "desayuno", "Avena con leche + 3 huevos + fruta", "09:00"),
    ("sábado", "merienda", "Banana + tostadas con manteca de maní (pre-partido)", "11:00"),
    ("sábado", "almuerzo", "Carne + arroz o papa + ensalada generosa", "14:30"),
    ("sábado", "cena", "Lo que haga la familia", "21:00"),
    # Domingo
    ("domingo", "desayuno", "Huevos + tostadas + leche + fruta", "09:00"),
    ("domingo", "merienda", "Banana (pre-gym)", "10:30"),
    ("domingo", "almuerzo", "Asado — priorizar vacío, cuadril o pollo. Agregar ensalada.", "13:00"),
    ("domingo", "cena", "Liviano — huevos, yogur, fruta", "21:00"),
]

def seed_comidas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM comidas WHERE momento = 'media_mañana'")
    media_count = c.fetchone()[0]
    if media_count == 0:
        c.execute("DELETE FROM comidas")
        c.executemany(
            "INSERT INTO comidas (dia, momento, descripcion, hora_recordatorio) VALUES (?, ?, ?, ?)",
            COMIDAS_BASE,
        )
        conn.commit()
        logger.info("Dieta actualizada a v2 con media mañana.")
    conn.close()

def get_comidas_dia(dia: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, momento, descripcion, hora_recordatorio FROM comidas WHERE dia = ? ORDER BY hora_recordatorio",
        (dia,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

def update_comida(dia: str, momento: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "UPDATE comidas SET descripcion = ? WHERE dia = ? AND momento = ?",
        (descripcion, dia, momento),
    )
    conn.commit()
    conn.close()

# ── Contexto hardcodeado
# ── Check-ins de comidas ────────────────────────────────────────────────────

def save_checkin_comida_cumplido(fecha: str, momento: str, cumplido: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "UPDATE checkins_comidas SET cumplido = ? WHERE fecha = ? AND momento = ?",
        (cumplido, fecha, momento),
    )
    conn.commit()
    conn.close()

def save_checkin_comida_nota(fecha: str, momento: str, nota: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "UPDATE checkins_comidas SET cumplido = 'parcial', nota = ? WHERE fecha = ? AND momento = ?",
        (nota, fecha, momento),
    )
    conn.commit()
    conn.close()

def marcar_checkins_comidas_sin_respuesta():
    """Marca como sin_respuesta los check-ins de comidas del día que quedaron sin responder."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    ahora_ar = datetime.now(AR_TZ)
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    c.execute(
        "UPDATE checkins_comidas SET cumplido = \'sin_respuesta\' WHERE fecha = ? AND cumplido IS NULL",
        (fecha_hoy,),
    )
    conn.commit()
    conn.close()
    logger.info("Check-ins de comidas sin respuesta marcados.")

# ── Check-ins de tareas ──────────────────────────────────────────────────────

def get_tareas_pendientes_ayer():
    """Retorna (rows, fecha_ayer) de tareas pendientes del día anterior."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    ahora_ar = datetime.now(AR_TZ)
    ayer = (ahora_ar - timedelta(days=1)).strftime("%d/%m/%Y")
    c.execute(
        "SELECT id, tarea, proyecto FROM checkins_tareas WHERE fecha = ? AND cumplido IS NULL",
        (ayer,),
    )
    rows = c.fetchall()
    conn.close()
    return rows, ayer

def save_checkin_tarea_cumplido(row_id: int, cumplido: str, nota: str = None):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "UPDATE checkins_tareas SET cumplido = ?, nota = ? WHERE id = ?",
        (cumplido, nota, row_id),
    )
    conn.commit()
    conn.close()

def save_tareas_del_plan(fecha: str, plan_texto: str):
    """Extrae las tareas del plan y las guarda en checkins_tareas."""
    proyectos_map = {
        "oma": "OMA", "mun": "MUN", "liberia": "MUN",
        "wsdc": "Debate WSDC", "debate": "Debate WSDC",
        "nasa": "NASA ISSDC", "issdc": "NASA ISSDC", "desla": "NASA ISSDC",
        "igcse": "IGCSE", "kognity": "IGCSE", "biology": "IGCSE",
        "instagram": "Marketing", "marketing": "Marketing",
    }
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    for line in plan_texto.splitlines():
        stripped = line.strip()
        if stripped.startswith("→"):
            tarea = stripped[1:].strip()
            proyecto = "Estudio"
            for kw, proj in proyectos_map.items():
                if kw in tarea.lower():
                    proyecto = proj
                    break
            c.execute(
                "INSERT INTO checkins_tareas (fecha, tarea, proyecto, cumplido) VALUES (?, ?, ?, NULL)",
                (fecha, tarea, proyecto),
            )
    conn.commit()
    conn.close()

def marcar_checkins_tareas_sin_respuesta():
    """Marca como sin_respuesta las tareas de ayer que no fueron respondidas."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    ahora_ar = datetime.now(AR_TZ)
    ayer = (ahora_ar - timedelta(days=1)).strftime("%d/%m/%Y")
    c.execute(
        "UPDATE checkins_tareas SET cumplido = \'sin_respuesta\' WHERE fecha = ? AND cumplido IS NULL",
        (ayer,),
    )
    conn.commit()
    conn.close()
    logger.info("Check-ins de tareas sin respuesta marcados.")

# ── Reprogramación de tareas ─────────────────────────────────────────────────

def _get_deadline_proyecto(proyecto: str):
    """Busca en fechas si hay un evento relacionado con el proyecto."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT fecha, evento FROM fechas ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)")
    rows = c.fetchall()
    conn.close()
    p_lower = proyecto.lower()
    kws = [p_lower] + p_lower.split()
    for fecha, evento in rows:
        if any(kw in evento.lower() for kw in kws if len(kw) > 2):
            return fecha
    return None

def reprogramar_tarea(tarea: str, proyecto: str, deadline: str = None):
    """
    Encuentra el próximo día hábil para la tarea.
    Returns (fecha_nueva, hay_tiempo):
      hay_tiempo=True  → dentro del deadline (o sin deadline)
      hay_tiempo=False → fuera del deadline, día más cercano igual
    """
    ahora_ar = datetime.now(AR_TZ)
    candidato = ahora_ar + timedelta(days=1)
    fecha_limite = None
    if deadline:
        try:
            d, m, y = deadline.split("/")
            deadline_dt = datetime(int(y), int(m), int(d), tzinfo=AR_TZ)
            fecha_limite = deadline_dt - timedelta(days=2)
        except Exception:
            pass
    for _ in range(14):
        if candidato.weekday() < 5:
            if fecha_limite and candidato.date() > fecha_limite.date():
                return candidato.strftime("%d/%m/%Y"), False
            return candidato.strftime("%d/%m/%Y"), True
        candidato += timedelta(days=1)
    return (ahora_ar + timedelta(days=1)).strftime("%d/%m/%Y"), False

def _guardar_tarea_reprogramada(tarea: str, proyecto: str, deadline: str, fecha_nueva: str):
    """Guarda la reprogramación en la DB."""
    ahora_ar = datetime.now(AR_TZ)
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO tareas_reprogramadas (fecha_original, fecha_nueva, tarea, proyecto, deadline) VALUES (?, ?, ?, ?, ?)",
        (ahora_ar.strftime("%d/%m/%Y"), fecha_nueva, tarea, proyecto, deadline or ""),
    )
    conn.commit()
    conn.close()
    logger.info(f"Tarea reprogramada: {tarea[:40]} → {fecha_nueva}")




RUTINA_TEXTO = """🗓 RUTINA SEMANAL

Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Estudio 20:30-21:00 → Cena 21:00-22:00 → Estudio 22:00-22:30 → Dormir 22:30
Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30"""

PROYECTOS = """📁 PROYECTOS ACTIVOS

OMA (Olimpiadas Matemáticas Argentinas) — Deadline: 2 julio. Preparación: ejercicios de exámenes pasados.
MUN (ANU-AR) — 26, 27 y 28 de junio. Representa Liberia en AG3. Preparar: tópicos de ANU-AR, discursos, posición de Liberia.
Debate WSDC (ADA) — Sin fecha fija, práctica continua. Formato WSDC, mociones variadas.
NASA ISSDC — DESLA — Competencia anual, preparación continua.
Materias IGCSE — Siempre al día. Material en Kognity.
Marketing/Instagram — Real Estate, sin deadline.
{contexto_plan_semanal}"""

PROMPT_DIA = """Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Estudio 20:30-21:00 → Cena 21:00-22:00 → Estudio 22:00-22:30 → Dormir 22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
- Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30

PROYECTOS Y DEADLINES:
- OMA — deadline 2 julio. Ejercicios de exámenes pasados.
- MUN (ANU-AR) — 26, 27 y 28 de junio. Liberia en AG3. Tópicos, discursos, posición.
- Debate WSDC (ADA) — práctica continua. Formato WSDC.
- NASA ISSDC — DESLA — preparación continua.
- Materias IGCSE — al día. Material en Kognity.
- Marketing/Instagram — Real Estate, sin deadline.

{contexto_rutina_fija}FECHAS PRÓXIMAS CARGADAS:
{fechas_db}

{contexto_feriado}{contexto_rutina}Hoy es {dia_semana} {fecha_hoy}. Generá el plan para mañana ({dia_manana} {fecha_manana}).

REGLAS DE FORMATO — seguí esto de forma ESTRICTA, sin excepciones:
- No uses markdown: sin #, ##, **, ni ---
- Usá emojis como separadores visuales
- Estructura EXACTA (una línea por bloque, en este orden):

📅 PLAN {dia_manana_upper} {fecha_manana} — FRANCO
🎓 8:30-17:00 Colegio
💪 [hora inicio según día]-[hora fin] [entrenamiento del día: Fútbol / Gym / Tenis / Partido fútbol]
🚿 [hora llegada a casa]-[hora llegada+45min] Baño + llegada
🍽️ 21:00 Cena familiar
⭐ [hora inicio]-[hora fin] Estudio
→ [tarea específica 1 — nombre real del tema, no genérico]
→ [tarea específica 2 — priorizada por deadline más cercano]
🔥 [frase motivadora, una sola línea, corta]

- Nada más. Sin secciones extra, sin justificaciones, sin resumen.
- Las tareas de estudio deben ser ESPECÍFICAS: no "estudiar MUN" sino "redactar posición de Liberia sobre financiamiento de misiones de paz".
- Priorizá por orgencia (deadline más cercano primero).
- Si el día siguiente no tiene entrenamiento (domingo libre o similar), omitís 💪 y 🚿.
- Si hay DÍA ESPECIAL indicado arriba, omitís el bloque 🎓 colegio y adaptás el plan a día libre.

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

REGLA DE DISTRIBUCIÓN DEL TIEMPO DE ESTUDIO:
Si hay más de una tarea asignada al bloque de estudio, dividí el tiempo disponible equitativamente entre ellas y especificá el horario exacto de cada una.

Ejemplo para un bloque de 30 minutos con 2 tareas:
⭐ 22:00-22:15 Estudio
→ MUN: Redactar posición de Liberia sobre financiamiento de misiones de paz

⭐ 22:15-22:30 Estudio
→ OMA: Resolver ejercicio 3 del examen 2024

Ejemplo para un bloque de 2 horas (viernes) con 3 tareas:
⭐ 20:30-21:00 Estudio
→ MUN: Preparar discurso de apertura

⭐ 22:00-22:20 Estudio
→ OMA: Resolver ejercicio del examen 2023

⭐ 22:20-22:30 Estudio
→ Debate WSDC: Leer moción y armar argumentos

Nunca agrupes dos tareas distintas en el mismo bloque sin dividir el tiempo.
"""

PROMPT_SEMANA = """Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: estudio disponible 22:00-22:30
- Martes: estudio disponible 22:00-22:30
- Miércoles: estudio disponible 22:00-22:30
- Jueves: estudio disponible 22:00-22:30
- Viernes: estudio disponible 20:30-21:00 y 22:00-22:30 (cena 21:00-22:00 en el medio)
- Sábado: tarde libre
- Domingo: tarde libre

PROYECTOS ACTIVOS:
- OMA — deadline 2 julio. Preparación: ejercicios de exámenes pasados.
- MUN ANU-AR — 26, 27 y 28 de junio. Representa Liberia en AG3.
- Debate WSDC — práctica continua.
- NASA ISSDC DESLA — preparación continua.
- Materias IGCSE — siempre al día.
- Marketing/Instagram — sin deadline.

FECHAS CARGADAS:
{fechas_db}

CALENDARIO EXACTO — usá ÚNICAMENTE estos nombres de día para cada fecha, sin modificarlos:
{calendario_semana}

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 7 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Distribuí por orgencia, deadline más cercano primero
- Respetá los slots de estudio según el día

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

FORMATO — sin markdown, sin símbolos extra:

📅 SEMANA DEL {fecha_hoy} AL {fecha_fin}

[Día] [DD/MM]
→ [Proyecto]: [tarea específica]
→ [Proyecto]: [tarea específica]

(Solo incluir días que tengan algo asignado)
"""

PROMPT_MES = """Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: estudio disponible 22:00-22:30
- Martes: estudio disponible 22:00-22:30
- Miércoles: estudio disponible 22:00-22:30
- Jueves: estudio disponible 22:00-22:30
- Viernes: estudio disponible 20:30-21:00 y 22:00-22:30 (cena 21:00-22:00 en el medio)
- Sábado: tarde libre
- Domingo: tarde libre

PROYECTOS ACTIVOS:
- OMA — deadline 2 julio. Preparación: ejercicios de exámenes pasados.
- MUN ANU-AR — 26, 27 y 28 de junio. Representa Liberia en AG3.
- Debate WSDC — práctica continua.
- NASA ISSDC DESLA — preparación continua.
- Materias IGCSE — siempre al día.
- Marketing/Instagram — sin deadline.

FECHAS CARGADAS:
{fechas_db}

CALENDARIO EXACTO — usá ÚNICAMENTE estos nombres de día para cada fecha, sin modificarlos:
{calendario_mes}

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 30 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Distribuí por orgencia, deadline más cercano primero
- Respetá los slots de estudio según el día

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

FORMATO — sin markdown, sin símbolos extra:

📅 PRÓXIMOS 30 DÍAS

[Día] [DD/MM]
→ [Proyecto]: [tarea específica]
→ [Proyecto]: [tarea específica]

(Solo incluir días que tengan algo asignado)
"""

PROMPT_PLAN_SEMANAL = """Sos el planificador semanal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA FIJA:
- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Miércoles: igual que Martes
- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño → Cena 21:00 → Estudio 20:30-22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre
- Domingo: Gym 11:00-12:30 → tarde libre

PROYECTOS ACTIVOS:
- OMA — deadline 2 julio
- MUN ANU-AR — 26, 27 y 28 de junio. Representa Liberia en AG3
- Debate WSDC (ADA) — práctica continua
- NASA ISSDC DESLA — preparación continua
- Materias IGCSE — siempre al día
- Marketing/Instagram — Real Estate, sin deadline
- Coding/desarrollo personal — sin deadline fijo

FECHAS PRÓXIMAS ESTA SEMANA:
{fechas_db}

MODIFICACIONES PERMANENTES:
{rutina_permanente}

Generá un plan de estudio y preparación para cada día de la semana que empieza el {fecha_lunes}.
Para cada día indicá:
- Bloque de estudio principal (tema + proyecto)
- Tarea secundaria si hay tiempo (puede ser coding, Instagram, o proyecto menor)
- Prioridad del día (qué es lo más urgente)

Respondé ÚNICAMENTE en JSON con este formato exacto, sin texto adicional:
{{"lunes": {{"prioridad": "...", "estudio_principal": "...", "secundario": "...", "nota": "..."}}, "martes": {{}}, "miércoles": {{}}, "jueves": {{}}, "viernes": {{}}, "sábado": {{}}, "domingo": {{}}}}"""

# ── Helpers de calendario ─────────────────────────────────────────────────────

def _calendario_dias(fecha_inicio: datetime, n_dias: int) -> str:
    """Genera lista de días con nombre de día correcto (calculado por Python) para el prompt."""
    dias_es = {
        0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves",
        4: "Viernes", 5: "Sábado", 6: "Domingo",
    }
    lineas = []
    for i in range(n_dias):
        dia = fecha_inicio + timedelta(days=i)
        lineas.append(f"- {dias_es[dia.weekday()]} {dia.strftime('%d/%m/%Y')}")
    return "\n".join(lineas)

# ── Claude ────────────────────────────────────────────────────────────────────

def generar_plan_texto():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)

    ahora_ar = datetime.now(AR_TZ)
    manana_ar = ahora_ar + timedelta(days=1)

    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }
    dia_semana = dias_es[ahora_ar.weekday()].capitalize()
    dia_manana = dias_es[manana_ar.weekday()].capitalize()
    dia_manana_upper = dia_manana.upper()
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    fecha_manana = manana_ar.strftime("%d/%m/%Y")

    rows = get_fechas()
    fechas_str = _format_fechas_para_prompt(rows)

    if is_feriado(fecha_manana):
        contexto_feriado = (
            "DÍA ESPECIAL: Mañana es feriado o no hay colegio. No incluyas bloque de colegio "
            "ni horario de levantarse a las 7:30. Tratalo como día libre — Franco puede organizar "
            "su tiempo desde cuando quiera. Mantené los entrenamientos si corresponde al día de la semana.\n\n"
        )
    else:
        contexto_feriado = ""

    rutina_mod = get_rutina_modificada(fecha_manana)
    if rutina_mod:
        contexto_rutina = (
            f"CAMBIO DE RUTINA PARA MAÑANA: {rutina_mod}\n"
            "Tené esto en cuenta al armar el plan y ajustá los horarios accordingly.\n\n"
        )
    else:
        contexto_rutina = ""

    # Rutina permanente (eventos fijos adicionales)
    rutina_perm_rows = get_rutinas_permanentes()
    if rutina_perm_rows:
        lineas_perm = []
        for _, dia, desc in rutina_perm_rows:
            dia_label = "todos los días" if dia == "multiple" else dia
            lineas_perm.append(f"- {desc} ({dia_label})")
        contexto_rutina_fija = "EVENTOS FIJOS ADICIONALES EN RUTINA:\n" + "\n".join(lineas_perm) + "\n\n"
    else:
        contexto_rutina_fija = ""

    # Contexto del plan semanal para mañana
    semana_inicio = _lunes_semana_actual()
    plan_semanal_row = get_plan_semanal(semana_inicio)
    contexto_plan_semanal = ""
    if plan_semanal_row:
        try:
            plan_json = json.loads(plan_semanal_row[0])
            dias_map = {0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
                        4: "viernes", 5: "sábado", 6: "domingo"}
            dia_key = dias_map.get(manana_ar.weekday(), "")
            if dia_key in plan_json:
                dp = plan_json[dia_key]
                contexto_plan_semanal = (
                    f"\nPLAN SEMANAL PARA MAÑANA:\n"
                    f"- Prioridad: {dp.get('prioridad', '')}\n"
                    f"- Estudio principal: {dp.get('estudio_principal', '')}\n"
                    f"- Secundario: {dp.get('secundario', '')}\n"
                    f"- Nota: {dp.get('nota', '')}\n"
                )
        except Exception:
            pass

    prompt = PROMPT_DIA.format(
        fechas_db=fechas_str,
        dia_semana=dia_semana,
        fecha_hoy=fecha_hoy,
        dia_manana=dia_manana,
        dia_manana_upper=dia_manana_upper,
        fecha_manana=fecha_manana,
        contexto_feriado=contexto_feriado,
        contexto_rutina=contexto_rutina,
        contexto_rutina_fija=contexto_rutina_fija,
        contexto_plan_semanal=contexto_plan_semanal,
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    plan = message.content[0].text

    # El modelo a veces recalcula el día de la semana incorrectamente para fechas futuras.
    # Forzamos la primera línea con los valores calculados por Python (siempre correctos).
    lineas_plan = plan.split("\n")
    primera_linea_correcta = f"📅 PLAN {dia_manana_upper} {fecha_manana} — FRANCO"
    if lineas_plan and lineas_plan[0].strip().startswith("📅"):
        lineas_plan[0] = primera_linea_correcta
        plan = "\n".join(lineas_plan)

    save_plan(fecha_manana, plan)
    return plan, fecha_manana


async def generar_plan_semanal(app):
    """Genera y guarda el plan semanal. Se ejecuta automáticamente los domingos a las 21:00."""
    logger.info("Generando plan semanal...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=180.0)
        semana_inicio = _lunes_semana_proxima()
        fecha_lunes_dt = datetime.strptime(semana_inicio, "%Y-%m-%d")
        fecha_lunes_str = fecha_lunes_dt.strftime("%d/%m/%Y")

        rows = get_fechas()
        fechas_semana = [
            f"{f} — {e}" + (f" ({h})" if h else "") + (f" | Material: {m}" if m else "")
            for f, e, h, m in rows
        ]
        fechas_str = "\n".join(fechas_semana) if fechas_semana else "Sin fechas próximas."

        rows_perm = get_rutinas_permanentes()
        rutina_perm_str = "\n".join(f"- {d}" for _, _, d in rows_perm) if rows_perm else "Sin modificaciones permanentes."

        prompt = PROMPT_PLAN_SEMANAL.format(
            fechas_db=fechas_str,
            rutina_permanente=rutina_perm_str,
            fecha_lunes=fecha_lunes_str,
        )
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        plan_text = message.content[0].text.strip()
        json_start = plan_text.find('{')
        json_end = plan_text.rfind('}') + 1
        if json_start != -1 and json_end > json_start:
            plan_json_str = plan_text[json_start:json_end]
            json.loads(plan_json_str)  # validate
            save_plan_semanal(semana_inicio, plan_json_str)
            logger.info(f"Plan semanal guardado para semana {semana_inicio}.")
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=f"📆 Plan semanal generado para la semana del {fecha_lunes_str}.\nTocá 'Esta semana' para verlo."
            )
        else:
            raise ValueError("No se encontró JSON válido en la respuesta.")
    except Exception as e:
        logger.error(f"Error generando plan semanal: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al generar plan semanal: {e}")


async def _enviar_plan_multipartes(reply_obj, texto: str):
    """Envía el texto en partes si supera 4096 chars. reply_obj es update.message o query.message."""
    if len(texto) <= 4096:
        await reply_obj.reply_text(texto)
        return

    lineas = texto.split("\n")
    dias_nombres = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

    indices_dias = []
    for i, linea in enumerate(lineas):
        for nombre in dias_nombres:
            if linea.strip().startswith(nombre):
                indices_dias.append(i)
                break

    if len(indices_dias) >= 2:
        corte = indices_dias[min(4, len(indices_dias) - 1)]
        parte1 = "\n".join(lineas[:corte]).strip()
        parte2 = "\n".join(lineas[corte:]).strip()
        if parte1:
            await reply_obj.reply_text(parte1)
        if parte2:
            await reply_obj.reply_text(parte2)
    else:
        chunk1 = texto[:4000].rsplit("\n", 1)[0]
        chunk2 = texto[len(chunk1):].strip()
        await reply_obj.reply_text(chunk1)
        if chunk2:
            await reply_obj.reply_text(chunk2)

# ── Helpers de parseo de fecha ────────────────────────────────────────────────

def _parsear_entrada_fecha(texto: str):
    """
    Acepta estos formatos:
      DD/MM/AAAA | Evento | HH:MM | Material   (4 partes)
      DD/MM/AAAA | Evento | | Material          (4 partes, horario vacío)
      DD/MM/AAAA | Evento | Material            (3 partes — sin horario)
      DD/MM/AAAA | Evento                       (2 partes — solo fecha y evento)

    Devuelve (fecha, evento, horario, material) o lanza ValueError.
    """
    partes = [p.strip() for p in texto.split("|")]
    if len(partes) < 2:
        raise ValueError("formato_incorrecto")

    fecha = partes[0]
    evento = partes[1]

    if len(partes) == 2:
        horario = ""
        material = ""
    elif len(partes) == 3:
        # Caso general: la tercera parte es material (sin horario)
        horario = ""
        material = partes[2]
    else:
        # 4+ partes: fecha | evento | horario (puede ser vacío) | material
        horario = partes[2]
        material = partes[3]

    # Validar fecha
    datetime.strptime(fecha, "%d/%m/%Y")

    return fecha, evento, horario, material

# ── Keyboards ─────────────────────────────────────────────────────────────────

def _build_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Plan de hoy", callback_data="plan"),
            InlineKeyboardButton("⚡ Generar plan", callback_data="generar"),
        ],
        [
            InlineKeyboardButton("📆 Esta semana", callback_data="semana"),
            InlineKeyboardButton("🗓️ Este mes", callback_data="mes"),
        ],
        [
            InlineKeyboardButton("➕ Cargar fecha", callback_data="cargar_fecha"),
            InlineKeyboardButton("📋 Ver fechas", callback_data="fechas"),
        ],
        [
            InlineKeyboardButton("🔄 Cambiar rutina", callback_data="cambiar_rutina"),
            InlineKeyboardButton("📚 Proyectos", callback_data="proyectos"),
        ],
        [
            InlineKeyboardButton("⚡ ¿Qué hago ahora?", callback_data="ahora"),
            InlineKeyboardButton("🥗 Mi dieta", callback_data="dieta"),
        ],
        [
            InlineKeyboardButton("⚙️ Editar rutina fija", callback_data="rutina_fija"),
        ],
    ])

def _build_rutina_fija_keyboard(rows):
    """Keyboard para la pantalla principal de rutina fija."""
    dias_buttons = [
        [
            InlineKeyboardButton("Lunes", callback_data="rf_ver_Lunes"),
            InlineKeyboardButton("Martes", callback_data="rf_ver_Martes"),
            InlineKeyboardButton("Miércoles", callback_data="rf_ver_Miercoles"),
        ],
        [
            InlineKeyboardButton("Jueves", callback_data="rf_ver_Jueves"),
            InlineKeyboardButton("Viernes", callback_data="rf_ver_Viernes"),
        ],
        [
            InlineKeyboardButton("Sábado", callback_data="rf_ver_Sabado"),
            InlineKeyboardButton("Domingo", callback_data="rf_ver_Domingo"),
        ],
        [InlineKeyboardButton("➕ Agregar evento fijo", callback_data="rf_agregar")],
    ]
    del_buttons = []
    for row_id, dia, desc in rows:
        dia_label = "Todos" if dia == "multiple" else dia
        del_buttons.append([
            InlineKeyboardButton(
                f"🗑️ [{dia_label}] {desc[:35]}",
                callback_data=f"rf_del|{row_id}"
            )
        ])
    return InlineKeyboardMarkup(del_buttons + dias_buttons)

def _build_dias_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Lunes", callback_data="rutina_dia_lunes"),
            InlineKeyboardButton("Martes", callback_data="rutina_dia_martes"),
            InlineKeyboardButton("Miércoles", callback_data="rutina_dia_miercoles"),
        ],
        [
            InlineKeyboardButton("Jueves", callback_data="rutina_dia_jueves"),
            InlineKeyboardButton("Viernes", callback_data="rutina_dia_viernes"),
        ],
        [
            InlineKeyboardButton("Sábado", callback_data="rutina_dia_sabado"),
            InlineKeyboardButton("Domingo", callback_data="rutina_dia_domingo"),
        ],
    ])

def _build_cambio_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Sin entrenamiento", callback_data="rutina_sin_entreno")],
        [InlineKeyboardButton("🕐 Cambiar horario", callback_data="rutina_cambiar_horario")],
        [InlineKeyboardButton("📝 Escribir manualmente", callback_data="rutina_manual")],
    ])

# ── Helpers de rutina fija ────────────────────────────────────────────────────

# Mapa callback → nombre con tilde para mostrar en pantalla
_RF_DIA_MAP = {
    "rf_ver_Lunes":     "Lunes",
    "rf_ver_Martes":    "Martes",
    "rf_ver_Miercoles": "Miércoles",
    "rf_ver_Jueves":    "Jueves",
    "rf_ver_Viernes":   "Viernes",
    "rf_ver_Sabado":    "Sábado",
    "rf_ver_Domingo":   "Domingo",
}

# Rutina base hardcodeada por día (para mostrar en "Editar rutina fija")
_RF_RUTINA_BASE = {
    "Lunes":     "Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30",
    "Martes":    "Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30",
    "Miércoles": "Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30",
    "Jueves":    "Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30",
    "Viernes":   "Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Estudio 20:30-21:00 → Cena 21:00-22:00 → Estudio 22:00-22:30 → Dormir 22:30",
    "Sábado":    "Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30",
    "Domingo":   "Gym 11:00-12:30 → tarde libre → Dormir 22:30",
}

def _texto_rutina_fija(rows):
    if rows:
        lineas = []
        for _, dia, desc in rows:
            dia_label = "Todos los días" if dia == "multiple" else dia
            lineas.append(f"• [{dia_label}] {desc}")
        return "⚙️ RUTINA FIJA\n\n" + "\n".join(lineas)
    return "⚙️ RUTINA FIJA\n\nNo hay eventos fijos cargados."

_MOMENTO_EMOJI = {
    "desayuno": "🌅",
    "media_mañana": "🍎",
    "almuerzo": "☀️",
    "merienda": "🌤️",
    "cena": "🌙",
}

def _texto_dieta_hoy(dia: str, rows) -> str:
    dias_upper = {
        "lunes": "LUNES", "martes": "MARTES", "miércoles": "MIÉRCOLES",
        "jueves": "JUEVES", "viernes": "VIERNES", "sábado": "SÁBADO", "domingo": "DOMINGO",
    }
    titulo = dias_upper.get(dia, dia.upper())
    texto = f"🥗 DIETA DE HOY — {titulo}\n"
    for _, momento, descripcion, hora in rows:
        emoji = _MOMENTO_EMOJI.get(momento, "🍽️")
        texto += f"\n{emoji} {momento.capitalize()} ({hora})\n→ {descripcion}\n"
    return texto


def _texto_dieta_semana() -> str:
    dias_orden = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    dias_labels = {
        "lunes": "LUNES ⚽ (fútbol)",
        "martes": "MARTES 🏋️ (gym)",
        "miércoles": "MIÉRCOLES 🏋️ (gym)",
        "jueves": "JUEVES ⚽ (fútbol)",
        "viernes": "VIERNES 🎾 (tenis)",
        "sábado": "SÁBADO ⚽ (partido)",
        "domingo": "DOMINGO 🏋️ (gym)",
    }
    texto = "🥗 DIETA SEMANAL — FRANCO\n"
    texto += "🎯 Meta: ~2800-3000 kcal | 115-128g proteína | 320-384g carbos\n"
    texto += "💧 Mínimo 2.5L agua/día\n\n"

    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    for dia in dias_orden:
        c.execute("SELECT momento, descripcion, hora_recordatorio FROM comidas WHERE dia = ? ORDER BY hora_recordatorio", (dia,))
        rows = c.fetchall()
        if not rows:
            continue
        label = dias_labels.get(dia, dia.upper())
        texto += f"📅 {label}\n"
        for momento, descripcion, hora in rows:
            emoji = _MOMENTO_EMOJI.get(momento, "🍽️")
            texto += f"{emoji} {momento.replace('_', ' ').capitalize()} ({hora}): {descripcion}\n"
        texto += "\n"
    conn.close()
    texto += "✅ Está bien: asado familiar, comida casera, flexibilidad fin de semana\n"
    texto += "❌ Evitar: frituras, gaseosas, ultraprocesados, exceso azúcar"
    return texto


# ── Comandos Telegram ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Hola Franco! ¿Qué hacemos?",
        reply_markup=_build_main_keyboard(),
    )

async def cmd_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args)
    try:
        fecha, evento, horario, material = _parsear_entrada_fecha(texto)
    except ValueError:
        await update.message.reply_text(
            "❌ Formato incorrecto. Usá:\n"
            "/fecha DD/MM/AAAA | Evento | Hora (opcional) | Material\n\n"
            "Ejemplos:\n"
            "06/06/2026 | ONU | 12:00 | Preparar discurso\n"
            "15/06/2026 | Examen Biology | | Chapter 4 Kognity\n"
            "02/07/2026 | OMA | Ejercicios exámenes pasados"
        )
        return
    save_fecha(fecha, evento, horario, material)
    extra = f" a las {horario}" if horario else ""
    await update.message.reply_text(f"✅ Fecha guardada: {fecha} — {evento}{extra}")

async def cmd_fechas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_fechas()
    if not rows:
        await update.message.reply_text("📭 No hay fechas cargadas.")
        return
    lines = []
    for i, (_, fecha, evento, horario, material) in enumerate(rows, 1):
        hora_str = f" {horario}" if horario else ""
        mat_str = f" (Material: {material})" if material else ""
        lines.append(f"{i}. {fecha} — {evento}{hora_str}{mat_str}")
    await update.message.reply_text("📅 FECHAS PRÓXIMAS:\n\n" + "\n".join(lines))

async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Uso: /borrar N")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El argumento debe ser un número.")
        return
    ok = delete_fecha_by_index(n)
    if ok:
        await update.message.reply_text(f"🗑 Fecha #{n} eliminada.")
    else:
        await update.message.reply_text(f"❌ No existe la fecha número {n}.")

async def cmd_feriado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Uso: /feriado DD/MM/AAAA")
        return
    fecha = context.args[0].strip()
    try:
        datetime.strptime(fecha, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("❌ Fecha inválida. Usá el formato DD/MM/AAAA")
        return
    save_feriado(fecha)
    await update.message.reply_text(f"🏖 Feriado guardado: {fecha}")

async def cmd_feriados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_feriados()
    if not rows:
        await update.message.reply_text("📭 No hay feriados cargados.")
        return
    lines = [f"{i}. {fecha}" for i, (_, fecha) in enumerate(rows, 1)]
    await update.message.reply_text("🏖 FERIADOS:\n\n" + "\n".join(lines))

async def cmd_borrarf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Uso: /borrarf N")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El argumento debe ser un número.")
        return
    ok = delete_feriado_by_index(n)
    if ok:
        await update.message.reply_text(f"🗑 Feriado #{n} eliminado.")
    else:
        await update.message.reply_text(f"❌ No existe el feriado número {n}.")

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now(AR_TZ).strftime("%d/%m/%Y")
    plan = get_plan(hoy)
    if plan:
        await update.message.reply_text(f"📋 Plan para hoy ({hoy}):\n\n{plan}")
    else:
        await update.message.reply_text(
            f"📭 No hay plan guardado para hoy ({hoy}).\n"
            "Usá /generar para crear el plan de mañana."
        )

async def cmd_generar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan con Claude, esperá un momento...")
    try:
        plan, fecha = generar_plan_texto()
        await update.message.reply_text(f"Plan generado para {fecha}:\n\n{plan}")
    except Exception as e:
        logger.error(f"Error generando plan: {e}")
        await update.message.reply_text(f"❌ Error al generar el plan: {e}")

async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan, esperá un momento...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
        ahora_ar = datetime.now(AR_TZ)
        fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
        fecha_fin = (ahora_ar + timedelta(days=6)).strftime("%d/%m/%Y")
        rows = get_fechas()
        fechas_str = _format_fechas_para_prompt(rows)
        calendario_semana = _calendario_dias(ahora_ar, 7)
        prompt = PROMPT_SEMANA.format(
            fechas_db=fechas_str,
            fecha_hoy=fecha_hoy,
            fecha_fin=fecha_fin,
            calendario_semana=calendario_semana,
        )
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        await _enviar_plan_multipartes(update.message, message.content[0].text)
    except Exception as e:
        logger.error(f"Error en /semana: {e}")
        await update.message.reply_text(f"❌ Error al generar el plan semanal: {e}")


async def cmd_plansemanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan semanal con Claude, esperá un momento...")
    try:
        await generar_plan_semanal(context.application)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan, esperá un momento...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
        ahora_ar = datetime.now(AR_TZ)
        fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
        rows = get_fechas()
        fechas_str = _format_fechas_para_prompt(rows)
        calendario_mes = _calendario_dias(ahora_ar, 30)
        prompt = PROMPT_MES.format(
            fechas_db=fechas_str,
            fecha_hoy=fecha_hoy,
            calendario_mes=calendario_mes,
        )
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        await _enviar_plan_multipartes(update.message, message.content[0].text)
    except Exception as e:
        logger.error(f"Error en /mes: {e}")
        await update.message.reply_text(f"❌ Error al generar el plan mensual: {e}")

async def cmd_proyectos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PROYECTOS)

async def cmd_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mods = get_all_rutinas_modificadas()
    texto = RUTINA_TEXTO
    if mods:
        texto += "\n\n📝 CAMBIOS GUARDADOS:"
        keyboard_rows = []
        for fecha, descripcion in mods:
            texto += f"\n• {fecha}: {descripcion}"
            keyboard_rows.append([
                InlineKeyboardButton(f"🗑️ Borrar cambio {fecha}", callback_data=f"del_rutina|{fecha}")
            ])
        await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard_rows))
    else:
        await update.message.reply_text(texto)

# ── Callback Handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "plan":
        hoy = datetime.now(AR_TZ).strftime("%d/%m/%Y")
        plan = get_plan(hoy)
        if plan:
            await query.edit_message_text(f"📋 Plan para hoy ({hoy}):\n\n{plan}")
        else:
            await query.edit_message_text(
                f"📭 No hay plan guardado para hoy ({hoy}).\n"
                "Tocá ⚡ Generar plan para crear el plan de mañana."
            )

    elif data == "generar":
        await query.edit_message_text("⏳ Generando plan con Claude, esperá un momento...")
        try:
            plan, fecha = generar_plan_texto()
            await query.message.reply_text(f"Plan generado para {fecha}:\n\n{plan}")
        except Exception as e:
            logger.error(f"Error generando plan: {e}")
            await query.message.reply_text(f"❌ Error al generar el plan: {e}")

    elif data == "semana":
        semana_inicio = _lunes_semana_actual()
        row = get_plan_semanal(semana_inicio)
        if not row:
            await query.edit_message_text(
                "📆 No hay plan semanal guardado para esta semana.\n"
                "El plan semanal se genera automáticamente los domingos a las 21:00.\n"
                "Podés generarlo ahora con /plansemanal"
            )
            return
        plan_json_str, generado_en = row
        try:
            plan = json.loads(plan_json_str)
            dias_orden = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
            texto = f"📆 PLAN SEMANAL — semana del {semana_inicio}\n(Generado: {generado_en})\n\n"
            for dia in dias_orden:
                if dia in plan:
                    dp = plan[dia]
                    texto += f"📅 {dia.upper()}\n"
                    texto += f"⭐ {dp.get('estudio_principal', '—')}\n"
                    texto += f"🔸 Secundario: {dp.get('secundario', '—')}\n"
                    texto += f"🔥 Prioridad: {dp.get('prioridad', '—')}\n"
                    if dp.get('nota'):
                        texto += f"📝 {dp.get('nota')}\n"
                    texto += "\n"
            await _enviar_plan_multipartes(query.message, texto)
        except Exception as e:
            await query.edit_message_text(f"❌ Error al mostrar plan semanal: {e}")

    elif data == "mes":
        await query.edit_message_text("⏳ Generando plan mensual, esperá un momento...")
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
            ahora_ar = datetime.now(AR_TZ)
            fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
            rows = get_fechas()
            fechas_str = _format_fechas_para_prompt(rows)
            calendario_mes = _calendario_dias(ahora_ar, 30)
            prompt = PROMPT_MES.format(
                fechas_db=fechas_str,
                fecha_hoy=fecha_hoy,
                calendario_mes=calendario_mes,
            )
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            await _enviar_plan_multipartes(query.message, message.content[0].text)
        except Exception as e:
            logger.error(f"Error en mes callback: {e}")
            await query.message.reply_text(f"❌ Error al generar el plan mensual: {e}")

    elif data == "cargar_fecha":
        context.user_data['estado'] = 'esperando_fecha_nueva'
        await query.edit_message_text(
            "Mandame la fecha en este formato:\n"
            "📝 DD/MM/AAAA | Evento | Hora (opcional) | Material\n\n"
            "Ejemplos:\n"
            "06/06/2026 | ONU | 12:00 | Preparar discurso\n"
            "15/06/2026 | Examen Biology | | Chapter 4 Kognity\n"
            "02/07/2026 | OMA | Ejercicios exámenes pasados"
        )

    elif data == "fechas":
        rows = get_fechas()
        if not rows:
            await query.edit_message_text("📭 No hay fechas cargadas.")
        else:
            lines = []
            for i, (_, fecha, evento, horario, material) in enumerate(rows, 1):
                hora_str = f" {horario}" if horario else ""
                mat_str = f" (Material: {material})" if material else ""
                lines.append(f"{i}. {fecha} — {evento}{hora_str}{mat_str}")
            await query.edit_message_text("📅 FECHAS PRÓXIMAS:\n\n" + "\n".join(lines))

    elif data == "proyectos":
        await query.edit_message_text(PROYECTOS)

    elif data == "cambiar_rutina":
        await query.edit_message_text(
            "¿Para qué día querés cambiar la rutina?",
            reply_markup=_build_dias_keyboard(),
        )

    elif data.startswith("rutina_dia_"):
        dia_map = {
            "rutina_dia_lunes": "Lunes",
            "rutina_dia_martes": "Martes",
            "rutina_dia_miercoles": "Miércoles",
            "rutina_dia_jueves": "Jueves",
            "rutina_dia_viernes": "Viernes",
            "rutina_dia_sabado": "Sábado",
            "rutina_dia_domingo": "Domingo",
        }
        dia = dia_map.get(data, "ese día")
        context.user_data['estado'] = 'esperando_fecha_rutina'
        context.user_data['rutina_dia'] = dia
        await query.edit_message_text(
            f"¿Para qué fecha querés cambiar el {dia}?\n"
            "Mandame la fecha: DD/MM/AAAA"
        )

    elif data == "rutina_sin_entreno":
        fecha = context.user_data.get('rutina_fecha', '')
        if not fecha:
            await query.edit_message_text("❌ Error: no tengo la fecha guardada. Volvé a empezar desde el menú.")
            return
        save_rutina_modificada(fecha, "Sin entrenamiento ese día. El tiempo del entreno queda libre.")
        context.user_data.clear()
        await query.edit_message_text(
            f"✅ Guardado: el {fecha} no hay entrenamiento. Lo voy a tener en cuenta al generar el plan."
        )

    elif data == "rutina_cambiar_horario":
        fecha = context.user_data.get('rutina_fecha', '')
        if not fecha:
            await query.edit_message_text("❌ Error: no tengo la fecha guardada. Volvé a empezar desde el menú.")
            return
        context.user_data['estado'] = 'esperando_horario_rutina'
        await query.edit_message_text("¿A qué horario? (ejemplo: 20:00-21:00)")

    elif data == "rutina_manual":
        fecha = context.user_data.get('rutina_fecha', '')
        if not fecha:
            await query.edit_message_text("❌ Error: no tengo la fecha guardada. Volvé a empezar desde el menú.")
            return
        context.user_data['estado'] = 'esperando_descripcion_rutina'
        await query.edit_message_text(
            "Describí el cambio:\n"
            "(ej: Me corto el pelo de 18:00 a 19:00, llego a casa a las 19:30)"
        )

    elif data.startswith("del_rutina|"):
        fecha = data.split("|", 1)[1]
        delete_rutina_modificada(fecha)
        await query.edit_message_text(f"🗑️ Cambio de rutina para {fecha} eliminado.")

    elif data == "ahora":
        await query.edit_message_text("⏳ Pensando...")
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            ahora_ar = datetime.now(AR_TZ)
            hora_actual = ahora_ar.strftime("%H:%M")
            dias_es = {
                0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
                4: "viernes", 5: "sábado", 6: "domingo",
            }
            dia_semana = dias_es[ahora_ar.weekday()]
            rows = get_fechas()
            fechas_str = _format_fechas_para_prompt(rows)
            prompt = (
                "Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.\n\n"
                "RUTINA SEMANAL:\n"
                "- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30\n"
                "- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30\n"
                "- Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30\n"
                "- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30\n"
                "- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Cena 21:00 → Estudio 20:30-22:30 → Dormir 22:30\n"
                "- Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30\n"
                "- Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30\n\n"
                "PROYECTOS ACTIVOS:\n"
                "- OMA — deadline 2 julio\n"
                "- MUN ANU-AR — 26, 27 y 28 de junio\n"
                "- Debate WSDC — práctica continua\n"
                "- NASA ISSDC DESLA — preparación continua\n"
                "- Materias IGCSE — siempre al día\n"
                "- Marketing/Instagram — sin deadline\n\n"
                f"FECHAS PRÓXIMAS:\n{fechas_str}\n\n"
                f"Ahora son las {hora_actual} del {dia_semana}.\n\n"
                "Respondé en 3 líneas máximo qué debería estar haciendo Franco en este momento o qué debería hacer a continuación. "
                "Sé muy específico y directo. Sin introducción, sin explicaciones largas.\n\n"
                "FORMATO:\n"
                f"🕐 Son las {hora_actual}\n"
                "→ [qué hacer ahora mismo]\n"
                "→ [qué viene después]"
            )
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            await query.message.reply_text(message.content[0].text)
        except Exception as e:
            logger.error(f"Error en ¿Qué hago ahora?: {e}")
            await query.message.reply_text(f"❌ Error: {e}")

    # ── Rutina fija ───────────────────────────────────────────────────────────

    elif data == "rutina_fija":
        rows = get_rutinas_permanentes()
        await query.edit_message_text(
            _texto_rutina_fija(rows),
            reply_markup=_build_rutina_fija_keyboard(rows),
        )

    elif data in _RF_DIA_MAP:
        dia_nombre = _RF_DIA_MAP[data]
        all_rows = get_rutinas_permanentes()
        rows_dia = [r for r in all_rows if r[1] == dia_nombre or r[1] == "multiple"]

        # Siempre mostrar la rutina base del día
        rutina_base = _RF_RUTINA_BASE.get(dia_nombre, "—")
        texto = f"📅 {dia_nombre.upper()}\n\nRUTINA BASE:\n{rutina_base}\n\nMODIFICACIONES FIJAS:\n"
        if rows_dia:
            lineas_mod = []
            for _, r_dia, r_desc in rows_dia:
                tag = " (todos los días)" if r_dia == "multiple" else ""
                lineas_mod.append(f"• {r_desc}{tag}")
            texto += "\n".join(lineas_mod)
        else:
            texto += "(ninguna todavía)"
        texto += "\n\n¿Qué querés cambiar?"

        del_buttons = []
        for r_id, r_dia, r_desc in rows_dia:
            del_buttons.append([
                InlineKeyboardButton(f"🗑️ {r_desc[:40]}", callback_data=f"rf_del|{r_id}")
            ])
        keyboard = InlineKeyboardMarkup(
            del_buttons + [
                [InlineKeyboardButton(f"➕ Agregar modificación fija", callback_data=f"rf_add|{dia_nombre}")],
                [InlineKeyboardButton("← Volver", callback_data="rutina_fija")],
            ]
        )
        await query.edit_message_text(texto, reply_markup=keyboard)

    elif data == "rf_agregar":
        context.user_data['estado'] = 'esperando_desc_rutina_fija'
        context.user_data['rf_dia'] = 'multiple'
        await query.edit_message_text(
            "Describí el evento fijo (aplica a todos los días):\n\n"
            "Ejemplo: Tomar vitaminas a las 8:00"
        )

    elif data.startswith("rf_add|"):
        dia_nombre = data.split("|", 1)[1]
        context.user_data['estado'] = 'esperando_desc_rutina_fija'
        context.user_data['rf_dia'] = dia_nombre
        await query.edit_message_text(
            f"Describí el evento fijo para {dia_nombre}:\n\n"
            "Ejemplo: Clase de piano 17:00-18:00"
        )

    elif data.startswith("rf_del|"):
        row_id = int(data.split("|", 1)[1])
        delete_rutina_permanente_by_id(row_id)
        rows = get_rutinas_permanentes()
        await query.edit_message_text(
            _texto_rutina_fija(rows),
            reply_markup=_build_rutina_fija_keyboard(rows),
        )

    # ── Dieta ─────────────────────────────────────────────────────────────────

    elif data == "dieta":
        texto = _texto_dieta_semana()
        if len(texto) > 4000:
            mid = texto[:4000].rfind('\n📅')
            if mid == -1:
                mid = 4000
            await query.edit_message_text(texto[:mid])
            await query.message.reply_text(texto[mid:])
        else:
            await query.edit_message_text(texto)

    elif data == "editar_comida":
        ahora_ar = datetime.now(AR_TZ)
        dias_es = {
            0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
            4: "viernes", 5: "sábado", 6: "domingo",
        }
        dia_hoy = dias_es[ahora_ar.weekday()]
        context.user_data["editar_dia"] = dia_hoy
        rows = get_comidas_dia(dia_hoy)
        buttons = [
            [InlineKeyboardButton(f"{r[1].capitalize()} ({r[3]})", callback_data=f"ec_momento|{r[1]}")]
            for r in rows
        ]
        await query.edit_message_text(
            f"¿Qué comida querés editar del {dia_hoy}?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("ec_momento|"):
        momento = data.split("|", 1)[1]
        dia = context.user_data.get("editar_dia", "")
        context.user_data["editar_momento"] = momento
        context.user_data["estado"] = "esperando_desc_comida"
        await query.edit_message_text(
            f"Mandame la nueva descripción para {momento} del {dia}:"
        )

    # ── Reprogramación ──────────────────────────────────────────────────────

    elif data == "reprogram_si":
        pending = context.user_data.pop("reprogram_pending", None)
        if pending:
            _guardar_tarea_reprogramada(
                pending["tarea"], pending["proyecto"],
                pending.get("deadline") or "", pending["fecha_nueva"],
            )
            fecha_n = pending["fecha_nueva"]
            tarea_n = pending["tarea"][:50]
            await query.edit_message_text(
                f"✅ Reprogramado para el {fecha_n}:\n→ {tarea_n}"
            )
        else:
            await query.edit_message_text("✅ Reprogramado.")

    elif data == "reprogram_no":
        context.user_data.pop("reprogram_pending", None)
        await query.edit_message_text("❌ No reprogramado. La tarea queda cancelada.")


    # ── Check-ins de tareas ─────────────────────────────────────────────────

    elif data.startswith("cit_todo|"):
        fecha_ayer = data.split("|", 1)[1]
        conn = sqlite3.connect("planner.db")
        c = conn.cursor()
        c.execute("UPDATE checkins_tareas SET cumplido = \'si\' WHERE fecha = ? AND cumplido IS NULL", (fecha_ayer,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Perfecto, todas las tareas marcadas como cumplidas.")

    elif data.startswith("cit_nada|"):
        fecha_ayer = data.split("|", 1)[1]
        conn = sqlite3.connect("planner.db")
        c = conn.cursor()
        c.execute("SELECT id, tarea, proyecto FROM checkins_tareas WHERE fecha = ? AND cumplido IS NULL", (fecha_ayer,))
        tareas_no = c.fetchall()
        c.execute("UPDATE checkins_tareas SET cumplido = \'no\' WHERE fecha = ? AND cumplido IS NULL", (fecha_ayer,))
        conn.commit()
        conn.close()
        reprogramadas = []
        for row_id, tarea, proyecto in tareas_no:
            deadline = _get_deadline_proyecto(proyecto)
            fecha_nueva, hay_tiempo = reprogramar_tarea(tarea, proyecto, deadline)
            if hay_tiempo:
                _guardar_tarea_reprogramada(tarea, proyecto, deadline or "", fecha_nueva)
                reprogramadas.append(f"→ {tarea[:40]} → {fecha_nueva}")
        if reprogramadas:
            await query.edit_message_text(
                "❌ Ninguna cumplida.\n\n🔄 Reprogramadas:\n" + "\n".join(reprogramadas)
            )
        else:
            await query.edit_message_text("❌ Ninguna cumplida. Se reprogramarán cuando haya tiempo disponible.")

    elif data.startswith("cit_parcial|"):
        fecha_ayer = data.split("|", 1)[1]
        conn = sqlite3.connect("planner.db")
        c = conn.cursor()
        c.execute("SELECT id, tarea FROM checkins_tareas WHERE fecha = ? AND cumplido IS NULL", (fecha_ayer,))
        tareas = c.fetchall()
        conn.close()
        if not tareas:
            await query.edit_message_text("✅ No quedan tareas pendientes.")
        else:
            await query.edit_message_text("⚡ Parcial. Revisemos tarea por tarea:")
            for row_id, tarea in tareas:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Cumplí", callback_data=f"cit_si|{row_id}"),
                    InlineKeyboardButton("❌ No cumplí", callback_data=f"cit_no|{row_id}"),
                    InlineKeyboardButton("📍 Llegué hasta...", callback_data=f"cit_hasta|{row_id}"),
                ]])
                await context.bot.send_message(chat_id=query.message.chat_id, text=f"→ {tarea}", reply_markup=kb)

    elif data.startswith("cit_si|"):
        row_id = int(data.split("|", 1)[1])
        save_checkin_tarea_cumplido(row_id, "si")
        await query.edit_message_text("✅ Tarea cumplida.")

    elif data.startswith("cit_no|"):
        row_id = int(data.split("|", 1)[1])
        save_checkin_tarea_cumplido(row_id, "no")
        conn = sqlite3.connect("planner.db")
        c = conn.cursor()
        c.execute("SELECT tarea, proyecto FROM checkins_tareas WHERE id = ?", (row_id,))
        row_tarea = c.fetchone()
        conn.close()
        if row_tarea:
            tarea, proyecto = row_tarea
            deadline = _get_deadline_proyecto(proyecto)
            fecha_nueva, hay_tiempo = reprogramar_tarea(tarea, proyecto, deadline)
            if hay_tiempo:
                _guardar_tarea_reprogramada(tarea, proyecto, deadline or "", fecha_nueva)
                dl_txt = f"\n→ Deadline: {deadline}" if deadline else ""
                await query.edit_message_text(
                    f"🔄 Reprogramado:\n→ {tarea[:50]}\n→ movida al {fecha_nueva}{dl_txt}"
                )
            else:
                context.user_data["reprogram_pending"] = {
                    "tarea": tarea, "proyecto": proyecto,
                    "deadline": deadline, "fecha_nueva": fecha_nueva,
                }
                dl_txt = f"del {deadline}" if deadline else "disponible"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Sí", callback_data="reprogram_si"),
                    InlineKeyboardButton("❌ No", callback_data="reprogram_no"),
                ]])
                await query.edit_message_text(
                    f"⚠️ No hay tiempo disponible antes del deadline {dl_txt}.\n"
                    f"¿Igualmente reprogramar para el {fecha_nueva}?\n→ {tarea[:40]}",
                    reply_markup=kb,
                )
        else:
            await query.edit_message_text("❌ Tarea no cumplida.")

    elif data.startswith("cit_hasta|"):
        row_id = int(data.split("|", 1)[1])
        context.user_data["ci_tarea_row_id"] = row_id
        context.user_data["estado"] = "esperando_nota_tarea"
        await query.edit_message_text("📍 ¿Hasta dónde llegaste?")


    # ── Check-ins de comidas ────────────────────────────────────────────────

    elif data.startswith("cic_si|"):
        _, fecha, momento = data.split("|", 2)
        save_checkin_comida_cumplido(fecha, momento, "si")
        await query.edit_message_text(f"✅ {momento.capitalize()} registrado. ¡Bien!")

    elif data.startswith("cic_no|"):
        _, fecha, momento = data.split("|", 2)
        save_checkin_comida_cumplido(fecha, momento, "no")
        await query.edit_message_text(
            f"❌ {momento.capitalize()} no cumplido.\nAnotado. Intentá compensar en la próxima comida con más proteína."
        )

    elif data.startswith("cic_parcial|"):
        _, fecha, momento = data.split("|", 2)
        context.user_data["ci_fecha"] = fecha
        context.user_data["ci_momento"] = momento
        context.user_data["estado"] = "esperando_nota_comida"
        await query.edit_message_text(f"⚡ {momento.capitalize()} parcial. ¿Qué comiste?")


# ── Message Handler ──────────────────────────────────────────────────────

async def handle_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    estado = context.user_data.get('estado')

    if estado == 'esperando_fecha_nueva':
        try:
            fecha, evento, horario, material = _parsear_entrada_fecha(texto)
        except ValueError:
            await update.message.reply_text(
                "❌ Formato incorrecto. Usá:\n"
                "DD/MM/AAAA | Evento | Hora (opcional) | Material\n\n"
                "Ejemplos:\n"
                "06/06/2026 | ONU | 12:00 | Preparar discurso\n"
                "15/06/2026 | Examen Biology | | Chapter 4 Kognity\n"
                "02/07/2026 | OMA | Ejercicios exámenes pasados"
            )
            return
        save_fecha(fecha, evento, horario, material)
        context.user_data.clear()
        extra = f" a las {horario}" if horario else ""
        await update.message.reply_text(f"✅ Fecha guardada: {fecha} — {evento}{extra}")

    elif estado == 'esperando_fecha_rutina':
        try:
            datetime.strptime(texto, "%d/%m/%Y")
        except ValueError:
            await update.message.reply_text(
                "❌ Fecha inválida. Mandamé la fecha en formato DD/MM/AAAA"
            )
            return
        context.user_data['rutina_fecha'] = texto
        context.user_data['estado'] = None
        dia = context.user_data.get('rutina_dia', 'ese día')
        await update.message.reply_text(
            f"Perfecto, ¿qué cambia el {dia} {texto}?",
            reply_markup=_build_cambio_keyboard(),
        )

    elif estado == 'esperando_horario_rutina':
        fecha = context.user_data.get('rutina_fecha', '')
        dia = context.user_data.get('rutina_dia', '')
        descripcion = f"El entrenamiento de {dia} cambia de horario: {texto}"
        save_rutina_modificada(fecha, descripcion)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Guardado: el {fecha} el entrenamiento es a las {texto}. Lo voy a tener en cuenta al generar el plan."
        )

    elif estado == 'esperando_descripcion_rutina':
        fecha = context.user_data.get('rutina_fecha', '')
        save_rutina_modificada(fecha, texto)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Guardado para el {fecha}: {texto}. Lo voy a tener en cuenta al generar el plan."
        )

    elif estado == 'esperando_desc_rutina_fija':
        dia = context.user_data.get('rf_dia', 'multiple')
        save_rutina_permanente(dia, texto)
        context.user_data.clear()
        dia_label = "todos los días" if dia == "multiple" else dia
        await update.message.reply_text(
            f"✅ Evento fijo guardado para {dia_label}:\n{texto}"
        )

    elif estado == 'esperando_desc_comida':
        dia = context.user_data.get('editar_dia', '')
        momento = context.user_data.get('editar_momento', '')
        update_comida(dia, momento, texto)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ {momento.capitalize()} del {dia} actualizado:\n→ {texto}"
        )

    elif estado == 'esperando_nota_comida':
        fecha = context.user_data.get('ci_fecha', '')
        momento = context.user_data.get('ci_momento', '')
        save_checkin_comida_nota(fecha, momento, texto)
        context.user_data.clear()
        await update.message.reply_text(f"✅ Anotado para {momento}: {texto}")

    elif estado == 'esperando_nota_tarea':
        row_id = context.user_data.get('ci_tarea_row_id')
        if row_id:
            save_checkin_tarea_cumplido(int(row_id), "parcial", texto)
        context.user_data.clear()
        await update.message.reply_text(f"📍 Anotado: {texto}")

    elif texto.lower() in ['hola', 'menu', 'menú']:
        context.user_data.clear()
        await update.message.reply_text(
            "👋 Hola Franco! ¿Qué hacemos?",
            reply_markup=_build_main_keyboard(),
        )

# ── Cron job ──────────────────────────────────────────────────────

def limpiar_fechas_pasadas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        DELETE FROM fechas
        WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) < date('now')
    """)
    conn.commit()
    conn.close()
    logger.info("Limpieza de fechas pasadas completada.")

async def job_noche(app):
    logger.info("Ejecutando cron job nocturno...")
    try:
        plan, fecha = generar_plan_texto()
        save_tareas_del_plan(fecha, plan)
        mensaje = f"🌙 Plan para mañana ({fecha}):\n\n{plan}"
        await app.bot.send_message(chat_id=CHAT_ID, text=mensaje)
        logger.info("Plan enviado exitosamente.")
    except Exception as e:
        logger.error(f"Error en cron job: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al generar el plan automático: {e}")

async def recordatorio_uniforme(app):
    ahora = datetime.now(AR_TZ)
    dia = ahora.weekday()
    uniformes = {
        0: "👔 Hoy es FORMAL",
        1: "👟 Hoy es ED. FÍSICA",
        2: "👔 Hoy es FORMAL",
        3: "👔 Hoy es FORMAL",
        4: "👟 Hoy es ED. FÍSICA",
    }
    if dia not in uniformes:
        return
    uniform_texto = uniformes[dia]
    tareas_ayer, fecha_ayer = get_tareas_pendientes_ayer()
    if tareas_ayer:
        lineas = "\n".join(f"→ {r[1]} — {r[2]}" for r in tareas_ayer)
        texto = (
            f"🌅 Buenos días Franco!\n\n"
            f"{uniform_texto}\n\n"
            f"📚 Ayer tenías pendiente:\n{lineas}\n\n"
            f"¿Cumpliste?"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Todo", callback_data=f"cit_todo|{fecha_ayer}"),
            InlineKeyboardButton("❌ Nada", callback_data=f"cit_nada|{fecha_ayer}"),
            InlineKeyboardButton("⚡ Parcial", callback_data=f"cit_parcial|{fecha_ayer}"),
        ]])
        await app.bot.send_message(chat_id=CHAT_ID, text=texto, reply_markup=keyboard)
    else:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🌅 Buenos días Franco!\n\n{uniform_texto}",
        )
    logger.info(f"Recordatorio uniforme enviado: {uniform_texto}")

async def _send_checkin_comida(app, momento: str):
    """Envía check-in de comida con botones Sí/No/Parcial."""
    ahora_ar = datetime.now(AR_TZ)
    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }
    dia_hoy = dias_es[ahora_ar.weekday()]
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    rows = get_comidas_dia(dia_hoy)
    descripcion = next((r[2] for r in rows if r[1] == momento), "—")
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id FROM checkins_comidas WHERE fecha = ? AND momento = ?", (fecha_hoy, momento))
    if not c.fetchone():
        c.execute(
            "INSERT INTO checkins_comidas (fecha, momento, descripcion, cumplido) VALUES (?, ?, ?, NULL)",
            (fecha_hoy, momento, descripcion),
        )
        conn.commit()
    conn.close()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data=f"cic_si|{fecha_hoy}|{momento}"),
        InlineKeyboardButton("❌ No", callback_data=f"cic_no|{fecha_hoy}|{momento}"),
        InlineKeyboardButton("⚡ Parcial", callback_data=f"cic_parcial|{fecha_hoy}|{momento}"),
    ]])
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=f"🍽️ ¿Cumpliste con {momento}?\n→ {descripcion}",
        reply_markup=keyboard,
    )
    logger.info(f"Check-in comida enviado: {momento}")

async def _send_meal_reminder(app, momento: str, descripcion: str):
    mensaje = f"🍽️ En un rato toca {momento}:\n→ {descripcion}"
    await app.bot.send_message(chat_id=CHAT_ID, text=mensaje)
    logger.info(f"Recordatorio de comida enviado: {momento}")

def _schedule_meal_reminders(scheduler, app):
    """Programa recordatorios 90 min antes de cada comida (almuerzo lun-vie siempre a las 11:50)."""
    dia_to_cron = {
        "lunes": "mon", "martes": "tue", "miércoles": "wed",
        "jueves": "thu", "viernes": "fri", "sábado": "sat", "domingo": "sun",
    }
    dias_semana = {"lunes", "martes", "miércoles", "jueves", "viernes"}
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT dia, momento, descripcion, hora_recordatorio FROM comidas")
    rows = c.fetchall()
    conn.close()
    for dia, momento, descripcion, hora_comida in rows:
        h, m = map(int, hora_comida.split(":"))
        # Almuerzo lun-vie: recordatorio en el momento exacto de la comida
        if momento == "almuerzo" and dia in dias_semana:
            reminder_h, reminder_m = h, m
        else:
            total = h * 60 + m - 90
            if total < 0:
                continue
            reminder_h, reminder_m = divmod(total, 60)
        cron_dia = dia_to_cron.get(dia)
        if not cron_dia:
            continue
        scheduler.add_job(
            _send_meal_reminder,
            trigger="cron",
            day_of_week=cron_dia,
            hour=reminder_h,
            minute=reminder_m,
            args=[app, momento, descripcion],
        )
    logger.info(f"Recordatorios de comidas programados para {len(rows)} entradas.")

async def resumen_semanal(app):
    """Genera y envía el resumen de la semana — corre los domingos a las 21:00."""
    ahora_ar = datetime.now(AR_TZ)
    hace_7_dias = (ahora_ar - timedelta(days=7)).strftime("%Y-%m-%d")
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        SELECT fecha, momento, cumplido FROM checkins_comidas
        WHERE cumplido != 'sin_respuesta'
        AND date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date(?)
    """, (hace_7_dias,))
    comidas = c.fetchall()
    c.execute("""
        SELECT fecha, tarea, proyecto, cumplido FROM checkins_tareas
        WHERE cumplido != 'sin_respuesta'
        AND date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date(?)
    """, (hace_7_dias,))
    tareas = c.fetchall()
    c.execute("""
        SELECT fecha_original, fecha_nueva, tarea FROM tareas_reprogramadas
        WHERE date(substr(fecha_original,7,4)||'-'||substr(fecha_original,4,2)||'-'||substr(fecha_original,1,2)) >= date(?)
    """, (hace_7_dias,))
    reprogramadas = c.fetchall()
    conn.close()
    if not comidas and not tareas:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="📊 Primera semana completa. ¡Seguí así para ver tu progreso!",
        )
        return
    tareas_si  = [(f, t) for f, t, p, cu in tareas if cu == "si"]
    tareas_no  = [(f, t) for f, t, p, cu in tareas if cu in ("no", "parcial")]
    comidas_si = [(f, m) for f, m, cu in comidas if cu == "si"]
    comidas_no = [(f, m) for f, m, cu in comidas if cu in ("no", "parcial")]
    lines = ["📊 RESUMEN DE LA SEMANA\n"]
    if tareas_si or comidas_si:
        lines.append("✅ CUMPLISTE:")
        for f, t in tareas_si:
            lines.append(f"→ {t[:45]} — {f}")
        for f, m in comidas_si[:5]:
            lines.append(f"→ {m.capitalize()} — {f}")
        lines.append("")
    if tareas_no or comidas_no:
        lines.append("❌ NO CUMPLISTE:")
        for f, t in tareas_no:
            lines.append(f"→ {t[:45]} — {f}")
        for f, m in comidas_no[:5]:
            lines.append(f"→ {m.capitalize()} — {f}")
        lines.append("")
    if reprogramadas:
        lines.append("🔄 REPROGRAMADO:")
        for fo, fn, t in reprogramadas:
            lines.append(f"→ {t[:45]} — movida al {fn}")
        lines.append("")
    total_t = len(tareas_si) + len(tareas_no)
    total_c = len(comidas_si) + len(comidas_no)
    tasa_t = f"{int(len(tareas_si) / total_t * 100)}%" if total_t > 0 else "—"
    tasa_c = f"{int(len(comidas_si) / total_c * 100)}%" if total_c > 0 else "—"
    lines.append(f"📈 Tasa de cumplimiento: {tasa_t} tareas / {tasa_c} dieta")
    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))
    logger.info("Resumen semanal enviado.")

async def recordatorio_mochila(app):
    """Recordatorio de mochila a las 16:35 lun-vie — qué meter para estudiar esta noche."""
    ahora_ar = datetime.now(AR_TZ)
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        SELECT fecha, evento, horario, material
        FROM fechas
        WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date('now', 'localtime')
        ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)
        LIMIT 10
    """)
    fechas = c.fetchall()
    conn.close()
    fechas_texto = "\n".join([
        f"{f[0]} - {f[1]}"
        + (f" - Horario: {f[2]}" if f[2] else "")
        + (f" - Material: {f[3]}" if f[3] else "")
        for f in fechas
    ]) if fechas else "Sin fechas cargadas"
    dia = ahora_ar.weekday()  # 4 = viernes
    tiempo_estudio = "2 horas (20:30-22:30)" if dia == 4 else "30 minutos (22:00-22:30)"
    prompt = (
        "Sos el planificador personal de Franco, 15 años, que está por salir del colegio.\n\n"
        f"FECHAS PRÓXIMAS:\n{fechas_texto}\n\n"
        f"Son las 16:35. Franco está por irse a casa. Esta noche tiene {tiempo_estudio} de estudio.\n\n"
        "Decile exactamente qué tiene que meter en la mochila para estudiar esta noche, basándote en las fechas más urgentes. "
        "Mencioná el material específico y para cuándo vence.\n\n"
        "FORMATO — sin markdown, máximo 5 líneas:\n\n"
        "🎒 Metete en la mochila:\n"
        "→ [material específico] — para [evento] ([fecha])\n"
        "→ [material específico] — para [evento] ([fecha])\n\n"
        "Si no hay nada urgente para estudiar esta noche respondé exactamente:\n"
        "🎒 Esta noche no hay nada urgente. Descansá tranquilo."
    )
    try:
        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        mensaje = response.content[0].text
        await app.bot.send_message(chat_id=CHAT_ID, text=mensaje)
        logger.info("Recordatorio de mochila enviado.")
    except Exception as e:
        logger.error(f"Error en recordatorio_mochila: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error en recordatorio de mochila: {e}")

# ── Main ────────────────────────────

def main():
    init_db()
    seed_comidas()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fecha", cmd_fecha))
    app.add_handler(CommandHandler("fechas", cmd_fechas))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("feriado", cmd_feriado))
    app.add_handler(CommandHandler("feriados", cmd_feriados))
    app.add_handler(CommandHandler("borrarf", cmd_borrarf))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("generar", cmd_generar))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("plansemanal", cmd_plansemanal))
    app.add_handler(CommandHandler("mes", cmd_mes))
    app.add_handler(CommandHandler("proyectos", cmd_proyectos))
    app.add_handler(CommandHandler("rutina", cmd_rutina))

    # Botones inline
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Mensajes de texto (hola/menu + estados de conversación)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mensaje))

    # Scheduler — 22:00 hora Argentina
    scheduler = AsyncIOScheduler(timezone=AR_TZ)
    scheduler.add_job(job_noche, trigger="cron", hour=22, minute=0, args=[app])
    scheduler.add_job(limpiar_fechas_pasadas, trigger="cron", hour=0, minute=0)
    scheduler.add_job(recordatorio_uniforme, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=7, minute=25, args=[app])
    # Check-ins de comidas — después de cada comida
    scheduler.add_job(_send_checkin_comida, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=8, minute=0, args=[app, "desayuno"])
    scheduler.add_job(_send_checkin_comida, trigger="cron", hour=13, minute=30, args=[app, "almuerzo"])
    scheduler.add_job(_send_checkin_comida, trigger="cron", hour=18, minute=30, args=[app, "merienda"])
    scheduler.add_job(_send_checkin_comida, trigger="cron", hour=22, minute=0, args=[app, "cena"])
    scheduler.add_job(marcar_checkins_comidas_sin_respuesta, trigger="cron", hour=23, minute=55)
    scheduler.add_job(marcar_checkins_tareas_sin_respuesta, trigger="cron", hour=10, minute=0)
    scheduler.add_job(generar_plan_semanal, trigger="cron", day_of_week="sun", hour=21, minute=0, args=[app])
    scheduler.add_job(resumen_semanal, trigger="cron", day_of_week="sun", hour=21, minute=0, args=[app])
    scheduler.add_job(recordatorio_mochila, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=16, minute=35, args=[app])
    _schedule_meal_reminders(scheduler, app)
    scheduler.start()
    logger.info("Scheduler iniciado. Cron job programado para las 22:00 AR.")

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    
