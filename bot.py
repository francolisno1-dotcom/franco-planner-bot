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
    # Migración: agregar columna kcal a comidas si no existe (para DBs existentes)
    try:
        c.execute("ALTER TABLE comidas ADD COLUMN kcal INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # La columna ya existe

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS contexto_franco (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            categoria TEXT,
            clave TEXT,
            valor TEXT,
            actualizado_en TEXT
        )
    """)

    conn.commit()
    conn.close()

def _hoy_ar_iso() -> str:
    """Fecha de HOY en hora Argentina, formato YYYY-MM-DD (el servidor corre en UTC)."""
    return datetime.now(AR_TZ).strftime("%Y-%m-%d")

def _parse_ddmmyyyy(fecha_str: str):
    """DD/MM/AAAA -> date, o None si no parsea."""
    try:
        return datetime.strptime(fecha_str, "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None

def _etiqueta_relativa(fecha_str: str) -> str:
    """Etiqueta HOY/MAÑANA/en N días calculada en Python (nunca dejar que Claude la calcule)."""
    d = _parse_ddmmyyyy(fecha_str)
    if d is None:
        return ""
    delta = (d - datetime.now(AR_TZ).date()).days
    if delta < 0:
        return f"[hace {-delta} días — YA PASÓ]"
    if delta == 0:
        return "[HOY]"
    if delta == 1:
        return "[MAÑANA]"
    return f"[en {delta} días]"

def get_fechas():
    """Devuelve solo fechas cuyo dia NO paso todavia (comparado en hora Argentina)."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, fecha, evento, horario, material FROM fechas "
        "WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date(?) "
        "ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)",
        (_hoy_ar_iso(),)
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
        etiqueta = _etiqueta_relativa(r[1])
        linea = f"- {r[1]} {etiqueta}: {r[2]}" if etiqueta else f"- {r[1]}: {r[2]}"
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
    # Formato: (dia, momento, descripcion, hora, kcal aprox)
    # Regla: SIEMPRE platos específicos y concretos, nunca macros genéricos.
    # LUNES (día de fútbol)
    ("lunes", "desayuno", "2 huevos revueltos + 2 tostadas de pan integral + vaso de leche entera", "07:45", 480),
    ("lunes", "media_mañana", "1 banana + puñado de maní sin sal (30g) 🥜", "10:30", 280),
    ("lunes", "almuerzo", "Pechuga de pollo a la plancha + arroz blanco + ensalada de lechuga y tomate", "13:00", 750),
    ("lunes", "merienda", "Pre-fútbol: 2 tostadas con mantequilla de maní + 1 banana 🍌 (1h antes del entreno)", "17:15", 380),
    ("lunes", "cena", "Milanesa de carne al horno + puré de papa + ensalada", "21:00", 900),
    # MARTES (día de gym)
    ("martes", "desayuno", "Avena cocida con leche + 1 banana + 2 huevos duros 🥚", "07:45", 550),
    ("martes", "media_mañana", "Yogur natural + granola sin azúcar (3 cucharadas)", "10:30", 250),
    ("martes", "almuerzo", "Pasta con salsa de tomate y carne picada", "13:00", 800),
    ("martes", "merienda", "Pre-gym: 2 tostadas con queso fresco + 1 banana 🍌", "17:15", 350),
    ("martes", "cena", "Post-gym 💪: Pollo al horno + guiso de lentejas + ensalada verde", "21:00", 850),
    # MIÉRCOLES (día de gym)
    ("miércoles", "desayuno", "3 huevos revueltos + 2 tostadas de pan integral + jugo de naranja exprimido 🍊", "07:45", 560),
    ("miércoles", "media_mañana", "1 manzana + puñado de nueces (30g)", "10:30", 280),
    ("miércoles", "almuerzo", "Bife a la plancha + papas al horno + ensalada de zanahoria y huevo", "13:00", 800),
    ("miércoles", "merienda", "Pre-gym: 2 tostadas con queso + 1 banana 🍌", "17:15", 350),
    ("miércoles", "cena", "Merluza a la plancha + arroz integral + ensalada verde 🥗", "21:00", 750),
    # JUEVES (día de fútbol)
    ("jueves", "desayuno", "2 huevos revueltos + 2 tostadas de pan integral + yogur natural", "07:45", 480),
    ("jueves", "media_mañana", "1 banana + puñado de maní sin sal (30g)", "10:30", 280),
    ("jueves", "almuerzo", "Ñoquis con salsa bolognesa (plato grande — énfasis en carbohidratos)", "13:00", 850),
    ("jueves", "merienda", "Pre-fútbol: 2 tostadas con mantequilla de maní + 1 manzana 🍌 (1h antes del entreno)", "17:15", 380),
    ("jueves", "cena", "Pollo grillé + arroz con arvejas + ensalada", "21:00", 800),
    # VIERNES (día de tenis)
    ("viernes", "desayuno", "Avena cocida con leche + 1 manzana rallada + 1 cucharada de miel 🥣", "07:45", 500),
    ("viernes", "media_mañana", "Yogur natural + 1 banana", "10:30", 250),
    ("viernes", "almuerzo", "Tarta de atún y huevo (2 porciones) + ensalada mixta", "13:00", 750),
    ("viernes", "merienda", "Pre-tenis liviana: 1 banana + 1 tostada con queso 🎾", "17:00", 250),
    ("viernes", "cena", "Cena familiar: bife con puré o pastas caseras (porción normal, sin excederse) 🍽️", "21:00", 900),
    # SÁBADO (partido de fútbol 12:00)
    ("sábado", "desayuno", "Avena con leche + 1 banana + 2 huevos revueltos ⚽ (pre-partido)", "09:00", 600),
    ("sábado", "media_mañana", "1 tostada con miel + 1 mandarina (liviano, antes del partido)", "10:30", 180),
    ("sábado", "almuerzo", "Post-partido 💪: pechuga de pollo + papas al horno + ensalada", "14:30", 800),
    ("sábado", "merienda", "Yogur natural con granola", "17:00", 250),
    ("sábado", "cena", "Pizza casera (3 porciones) o empanadas de carne (4 unidades) — cena familiar", "21:00", 850),
    # DOMINGO (gym 11:00)
    ("domingo", "desayuno", "Avena con leche + 1 banana + 2 huevos duros 🏋️ (pre-gym)", "09:30", 600),
    ("domingo", "almuerzo", "Post-gym: asado familiar (vacío o pollo) + ensalada de papa + verduras a la parrilla", "13:00", 950),
    ("domingo", "merienda", "Yogur natural + 1 fruta", "17:00", 250),
    ("domingo", "cena", "Omelette de 3 huevos con queso y jamón + 2 tostadas + ensalada", "21:00", 750),
]

# Tipo de día y objetivo calórico (todos los días de la rutina base tienen entrenamiento)
DIA_TIPO_OBJETIVO = {
    "lunes": ("día de fútbol", "2800-3000"),
    "martes": ("día de gym", "2800-3000"),
    "miércoles": ("día de gym", "2800-3000"),
    "jueves": ("día de fútbol", "2800-3000"),
    "viernes": ("día de tenis", "2800-3000"),
    "sábado": ("partido de fútbol", "2800-3000"),
    "domingo": ("día de gym", "2800-3000"),
}
# Nota: en un día SIN entrenamiento el objetivo es ~2500 kcal.
OBJETIVO_DIA_DESCANSO = "~2500"


def seed_comidas():
    """Dieta v3: platos específicos + kcal. Se resiembra una sola vez (cuando ninguna fila tiene kcal)."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM comidas WHERE kcal IS NOT NULL")
    if c.fetchone()[0] == 0:
        c.execute("DELETE FROM comidas")
        c.executemany(
            "INSERT INTO comidas (dia, momento, descripcion, hora_recordatorio, kcal) VALUES (?, ?, ?, ?, ?)",
            COMIDAS_BASE,
        )
        conn.commit()
        logger.info("Dieta actualizada a v3 con platos específicos y kcal.")
    conn.close()

def get_comidas_dia(dia: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, momento, descripcion, hora_recordatorio, kcal FROM comidas WHERE dia = ? ORDER BY hora_recordatorio",
        (dia,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

def update_comida(dia: str, momento: str, descripcion: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "UPDATE comidas SET descripcion = ?, kcal = NULL WHERE dia = ? AND momento = ?",
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

def refrescar_tareas_del_plan(fecha: str, plan_texto: str):
    """Reemplaza los checkins NO respondidos de la fecha por las tareas del plan nuevo (evita duplicados al regenerar)."""
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("DELETE FROM checkins_tareas WHERE fecha = ? AND cumplido IS NULL", (fecha,))
    conn.commit()
    conn.close()
    save_tareas_del_plan(fecha, plan_texto)

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


CONTEXTO_INICIAL = [
    # Materias — dificultad actual (puede cambiar)
    ("materia", "Italiano", "actualmente difícil, prioridad alta"),
    ("materia", "Literatura (Lengua)", "actualmente difícil, prioridad alta"),
    ("materia", "Maths", "dificultad media-alta"),
    ("materia", "Business Studies", "dificultad media-alta"),
    ("materia", "Introducción a la Química", "dificultad media-alta"),
    ("materia", "Biología", "dificultad media-alta"),
    ("materia", "Introducción a la Física", "dificultad media-alta"),
    ("materia", "Literature (inglés)", "dificultad media-alta"),
    ("materia", "Historia", "dificultad media"),
    ("materia", "Geografía", "dificultad media"),
    ("materia", "NTICX", "dificultad media"),
    ("materia", "Portugués", "dificultad media"),
    ("materia", "Salud y Adolescencia", "dificultad media"),
    ("materia", "Educación Física", "fácil, poco tiempo de estudio necesario"),
    ("materia", "Matemática Ciclo Superior", "fácil, poco tiempo de estudio necesario"),
    # Proyectos — estado actual
    ("proyecto", "MUN ANU-AR", "26-28 junio, ya preparado, solo repaso liviano"),
    ("proyecto", "OMA Zonal", "2 julio, aún no empezó preparación, tiene material de exámenes anteriores, prioridad alta"),
    ("proyecto", "Exámenes cuatrimestrales", "1-17 julio, todas las materias, dos años de material, evento más importante del año"),
    ("proyecto", "Debate WSDC", "práctica continua sin fecha fija, prioridad media"),
    ("proyecto", "Instagram Real Estate", "sin deadline, quiere publicaciones diarias, prioridad baja"),
    ("proyecto", "NASA ISSDC DESLA", "inactivo por ahora, no planificar"),
    ("proyecto", "Coding personal", "diseño de sistemas con IA, sin deadline, solo si sobra tiempo"),
    # Contexto general
    ("general", "colegio", "4° Año A Secundaria Sur, sistema por quincenas, ahora en quincena 7 de 9, cuatrimestre 1"),
    ("general", "gym", "martes pecho+trícep, miércoles espalda+bícep, domingo pecho+hombro+pierna, empezó en marzo"),
    ("general", "sueño", "se acuesta 22:30, a veces se queda leyendo hasta las 23:50"),
    ("general", "personalidad", "le cuesta arrancar tareas, se organiza bien bajo presión, lee libros de desarrollo personal"),
]


def get_contexto_franco():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT categoria, clave, valor FROM contexto_franco ORDER BY categoria, clave")
    rows = c.fetchall()
    conn.close()
    return rows


def upsert_contexto(categoria: str, clave: str, valor: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    ahora = datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M")
    c.execute("SELECT id FROM contexto_franco WHERE clave = ?", (clave,))
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE contexto_franco SET categoria=?, valor=?, actualizado_en=? WHERE clave=?",
            (categoria, valor, ahora, clave),
        )
    else:
        c.execute(
            "INSERT INTO contexto_franco (categoria, clave, valor, actualizado_en) VALUES (?, ?, ?, ?)",
            (categoria, clave, valor, ahora),
        )
    conn.commit()
    conn.close()


def seed_contexto():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM contexto_franco")
    count = c.fetchone()[0]
    if count == 0:
        ahora = datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M")
        for cat, clave, valor in CONTEXTO_INICIAL:
            c.execute(
                "INSERT INTO contexto_franco (categoria, clave, valor, actualizado_en) VALUES (?, ?, ?, ?)",
                (cat, clave, valor, ahora),
            )
        conn.commit()
        logger.info("Contexto inicial de Franco cargado.")
    conn.close()


def _build_contexto_prompt() -> str:
    rows = get_contexto_franco()
    if not rows:
        return ""
    by_cat = {}
    for cat, clave, valor in rows:
        by_cat.setdefault(cat, []).append((clave, valor))
    cat_labels = {"materia": "Materias", "proyecto": "Proyectos", "general": "General"}
    lines = ["CONTEXTO ACTUAL DE FRANCO:"]
    for cat in ["materia", "proyecto", "general"]:
        if cat not in by_cat:
            continue
        lines.append(f"[{cat_labels[cat]}]")
        for clave, valor in by_cat[cat]:
            lines.append(f"- {clave}: {valor}")
    for cat, items in by_cat.items():
        if cat not in cat_labels:
            lines.append(f"[{cat.capitalize()}]")
            for clave, valor in items:
                lines.append(f"- {clave}: {valor}")
    return "\n".join(lines) + "\n"



FECHAS_INICIALES = [
    # Formato: (DD/MM/YYYY, evento, horario, material)
    ("11/06/2026", "Oral Literatura", None, "La Casa de Bernarda Alba"),
    ("12/06/2026", "Presentación Química", None, "Bioetanol"),
    ("12/06/2026", "Examen Matemáticas", None, "Segunda parte Álgebra & Graphs"),
    ("12/06/2026", "Examen Física", None, "Dinámica y Leyes de Newton"),
    ("23/06/2026", "Entregar TP Matemática", None, "Vectors, Matrix & Transformation"),
    ("26/06/2026", "MUN ANU-AR Día 1", None, "Discurso apertura, posición Liberia AG3"),
    ("26/06/2026", "Español IGCSE Paper 1 y 2", None, "Todo el material IGCSE — resolver conflicto con directora"),
    ("27/06/2026", "MUN ANU-AR Día 2", None, "Debate General AG3"),
    ("28/06/2026", "MUN ANU-AR Día 3", None, "Resoluciones finales AG3"),
    ("29/06/2026", "Historia IGCSE", None, "Todo lo visto este cuatrimestre"),
    ("01/07/2026", "English Literature IGCSE Paper 1 y 2", None, "Material IGCSE dos años"),
    ("02/07/2026", "OMA Olimpiadas Matemáticas", None, "Ejercicios exámenes pasados"),
    ("03/07/2026", "English IGCSE Paper 1 y 2", None, "Material IGCSE dos años"),
    ("06/07/2026", "Literatura Paper 1 IGCSE", None, "Material IGCSE dos años"),
    ("07/07/2026", "Italiano IGCSE", None, "Material IGCSE dos años"),
    ("08/07/2026", "Biology IGCSE Paper 1 y 2", None, "Material IGCSE dos años"),
    ("13/07/2026", "Literatura Paper 2 IGCSE", None, "Material IGCSE dos años"),
    ("15/07/2026", "Business Studies IGCSE Paper 1 y 2", None, "Material IGCSE dos años"),
    ("16/07/2026", "Matemática IGCSE Paper 1 y 2", None, "Material IGCSE dos años"),
]


def seed_fechas_iniciales():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    inserted = 0
    for fecha, evento, horario, material in FECHAS_INICIALES:
        c.execute(
            "SELECT COUNT(*) FROM fechas WHERE fecha = ? AND evento = ?",
            (fecha, evento),
        )
        if c.fetchone()[0] == 0:
            c.execute(
                "INSERT INTO fechas (fecha, evento, horario, material) VALUES (?, ?, ?, ?)",
                (fecha, evento, horario, material),
            )
            inserted += 1
    conn.commit()
    conn.close()
    if inserted:
        logger.info(f"Fechas iniciales: {inserted} fechas nuevas cargadas.")




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
Marketing/Instagram — Real Estate, sin deadline."""

PROMPT_DIA = """Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Estudio 20:30-21:00 → Cena 21:00-22:00 → Estudio 22:00-22:30 → Dormir 22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
- Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30

{contexto_franco}

{contexto_rutina_fija}{eventos_hoy}FECHAS PRÓXIMAS CARGADAS (todas son FUTURAS — ninguna es hoy; la etiqueta [MAÑANA] / [en N días] ya viene calculada):
{fechas_db}

{contexto_feriado}{contexto_rutina}{contexto_checkins}{contexto_plan_semanal}Hoy es {dia_semana} {fecha_hoy}, son las 06:00 de la mañana. Generá el plan para HOY ({dia_plan} {fecha_plan}).

REGLAS DE FORMATO — seguí esto de forma ESTRICTA, sin excepciones:
- No uses markdown: sin #, ##, **, ni ---
- Usá emojis como separadores visuales
- Estructura EXACTA (una línea por bloque, en este orden):

📅 PLAN {dia_plan_upper} {fecha_plan} — FRANCO
🎓 8:30-17:00 Colegio
💪 [hora inicio según día]-[hora fin] [entrenamiento del día: Fútbol / Gym / Tenis / Partido fútbol]
🚿 [hora llegada a casa]-[hora llegada+45min] Baño + llegada
🍽️ 21:00 Cena familiar
⭐ [hora inicio]-[hora fin] Estudio
→ [tarea específica 1 — nombre real del tema, no genérico]
→ [tarea específica 2 — priorizada por deadline más cercano]
📖 [hora inicio]-[hora fin] Tiempo libre — lectura o coding personal (SOLO si sobra tiempo real, ver regla abajo)
🔥 [frase motivadora, una sola línea, corta]

- Nada más. Sin secciones extra, sin justificaciones, sin resumen.
- Las tareas de estudio deben ser ESPECÍFICAS: no "estudiar MUN" sino "redactar posición de Liberia sobre financiamiento de misiones de paz".
- No priorices por cercanía de deadline: aplicá estudio distribuido (ver regla abajo).
- Si el día no tiene entrenamiento (domingo libre o similar), omitís 💪 y 🚿.
- Si hay DÍA ESPECIAL indicado arriba, omitís el bloque 🎓 colegio y adaptás el plan a día libre.

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

REGLA DE FIDELIDAD — NO INVENTAR NADA:
- Solo planificá con la información que te doy explícitamente en este prompt. No inventes actividades, horarios ni tareas que no estén en las fechas, rutina o contexto provisto.
- Nunca inventes horarios que Franco no especificó. Si una fecha dice "todo el día", bloqueá el día completo. Si no da horario, no asumas uno — usá la descripción tal cual la cargó.
- Si un evento ya ocurrió hoy más temprano (ej: un examen que se rindió a la mañana en el colegio), no lo incluyas como pendiente de preparar esta noche.
- Cuando menciones cuándo es un examen o evento, copiá la etiqueta relativa que viene en la lista ([MAÑANA], [en N días]) — NUNCA calcules vos si algo es "hoy" o "mañana". Está prohibido escribir "examen mañana" salvo que la fecha tenga la etiqueta [MAÑANA].
- Si mencionás una comida, siempre indicá un plato concreto y específico (ej: "Milanesa de carne con puré", "Pollo al horno con arroz"). Nunca uses descripciones genéricas de macronutrientes.

REGLA DE ESTUDIO DISTRIBUIDO (spaced repetition) — MUY IMPORTANTE:
- Para fechas con más de 7 días de anticipación ([en N días] con N mayor a 7), NO esperes a último momento para planificar. Asigná una tarea CHICA y concreta (ej: 1 ejercicio, 1 pregunta de past paper, 20 min de repaso) por día o cada pocos días, distribuida en el tiempo, en vez de concentrar todo el estudio cerca de la fecha.
- El repaso intensivo se reserva SOLO para el último día o los últimos dos días antes del evento — y ya no es la única preparación, es el cierre de algo que se vino trabajando de a poco.
- La tarea distribuida es chica y sostenible (10-20 min) y convive con las otras prioridades del día (otras materias, proyectos, deportes). No es un bloque entero.
- No uses la lógica de "cuanto más cerca la fecha, más prioridad": usá "empezar temprano con dosis chicas, sostenido en el tiempo, e intensificar recién al final".

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

REGLA DE TIEMPO LIBRE — HOBBY (lectura / coding personal):
- Si después de cubrir TODAS las tareas académicas prioritarias sobra tiempo real en el día, asigná un bloque explícito de hobby (lectura o coding personal) en vez de dejar ese tiempo vacío o llenarlo con más estudio forzado.
- El hobby SIEMPRE va después de todo lo académico prioritario, nunca antes.
- Si sobra tiempo real, el bloque 📖 DEBE aparecer en el plan — no lo omitas. Si no sobra tiempo, omitilo.
- Ejemplo: 📖 21:00-21:30 Tiempo libre — lectura o coding personal
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

{contexto_franco}

FECHAS CARGADAS:
{fechas_db}

CALENDARIO EXACTO — usá ÚNICAMENTE estos nombres de día para cada fecha, sin modificarlos:
{calendario_semana}

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 7 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Estudio distribuido: para fechas a más de 7 días, tareas chicas y recurrentes repartidas en los días (no cramming); el repaso intensivo solo el último día o los dos últimos antes del evento
- Respetá los slots de estudio según el día

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

REGLA DE FIDELIDAD — NO INVENTAR NADA:
- Solo planificá con la información que te doy explícitamente en este prompt. No inventes actividades, horarios ni tareas que no estén en las fechas, rutina o contexto provisto.
- Nunca inventes horarios que Franco no especificó. Si una fecha dice "todo el día", bloqueá el día completo. Si no da horario, no asumas uno — usá la descripción tal cual la cargó.
- Si un evento ya ocurrió hoy más temprano (ej: un examen que se rindió a la mañana en el colegio), no lo incluyas como pendiente de preparar esta noche.

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

{contexto_franco}

FECHAS CARGADAS:
{fechas_db}

CALENDARIO EXACTO — usá ÚNICAMENTE estos nombres de día para cada fecha, sin modificarlos:
{calendario_mes}

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 30 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Estudio distribuido: para fechas a más de 7 días, tareas chicas y recurrentes repartidas en los días (no cramming); el repaso intensivo solo el último día o los dos últimos antes del evento
- Respetá los slots de estudio según el día

REGLA IMPORTANTE — EXÁMENES: Si una fecha es un examen, Franco lo da en el colegio en horario escolar. NO asignes tareas de "rendir el examen" ni "dar el examen" en el plan — eso pasa solo en el colegio. Lo único que podés asignar es preparación o repaso ANTES de la fecha del examen, no el día del examen en sí. El día del examen simplemente no aparece como tarea de estudio.

REGLA DE FIDELIDAD — NO INVENTAR NADA:
- Solo planificá con la información que te doy explícitamente en este prompt. No inventes actividades, horarios ni tareas que no estén en las fechas, rutina o contexto provisto.
- Nunca inventes horarios que Franco no especificó. Si una fecha dice "todo el día", bloqueá el día completo. Si no da horario, no asumas uno — usá la descripción tal cual la cargó.
- Si un evento ya ocurrió hoy más temprano (ej: un examen que se rindió a la mañana en el colegio), no lo incluyas como pendiente de preparar esta noche.

FORMATO — sin markdown, sin símbolos extra:

📅 PRÓXIMOS 30 DÍAS

[Día] [DD/MM]
→ [Proyecto]: [tarea específica]
→ [Proyecto]: [tarea específica]

(Solo incluir días que tengan algo asignado)
"""

PROMPT_PLAN_SEMANAL = """Respondé ÚNICAMENTE con el JSON. Sin texto antes ni después. Sin comillas tipográficas. Sin markdown. Sin bloques de código.

Sos el planificador semanal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA FIJA:
- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Miércoles: igual que Martes
- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño → Cena 21:00 → Estudio 22:00-22:30
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño → Cena 21:00 → Estudio 20:30-22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre
- Domingo: Gym 11:00-12:30 → tarde libre

{contexto_franco}

FECHAS PRÓXIMAS ESTA SEMANA:
{fechas_db}

MODIFICACIONES PERMANENTES:
{rutina_permanente}

Hoy es {fecha_hoy}. Generá un plan de estudio y preparación para cada día de la semana que empieza el {fecha_lunes}.
Si {fecha_hoy} cae dentro de esa semana (regeneración a mitad de semana), NO asignes tareas a días anteriores a hoy — dejá esos días con campos vacíos.
Para cada día indicá:
- Bloque de estudio principal (tema + proyecto)
- Tarea secundaria si hay tiempo (puede ser coding, Instagram, o proyecto menor; si no hay nada académico urgente, puede ser tiempo libre de hobby: lectura o coding personal)
- Prioridad del día (qué es lo más urgente)

REGLA DE FIDELIDAD — NO INVENTAR NADA:
- Solo planificá con la información que te doy explícitamente en este prompt. No inventes actividades, horarios ni tareas que no estén en las fechas, rutina o contexto provisto.
- Nunca inventes horarios que Franco no especificó. Si una fecha dice "todo el día", bloqueá el día completo. Si no da horario, no asumas uno — usá la descripción tal cual la cargó.
- Si un evento ya ocurrió hoy más temprano (ej: un examen que se rindió a la mañana en el colegio), no lo incluyas como pendiente de preparar esta noche.
- Si una fecha es un examen, Franco lo rinde en el colegio: solo asigná preparación ANTES del examen, nunca el día del examen ni después.

REGLA DE ESTUDIO DISTRIBUIDO (spaced repetition) — MUY IMPORTANTE:
- Para fechas con más de 7 días de anticipación, NO esperes a último momento. Asigná una tarea CHICA y concreta (ej: 1 ejercicio, 1 pregunta de past paper, 20 min de repaso) por día o cada pocos días, distribuida a lo largo de la semana, en vez de concentrar todo cerca de la fecha.
- El repaso intensivo se reserva solo para el último día o los dos últimos antes del evento.
- Las tareas distribuidas son chicas (10-20 min) y conviven con las demás prioridades — no llenes un día con una sola materia.
- Reemplazá "cuanto más cerca la fecha, más prioridad" por "empezar temprano con dosis chicas, sostenido, e intensificar recién al final".

PREPARACIÓN IGCSE (examen final a mediados de octubre 2026) — arrancá YA con dosis chicas:
- Materias IGCSE con examen final en octubre: Biology, Español-Literatura (unificada, es una sola materia), English Literature, English Language, Business Studies, Maths IGCSE.
- Cada semana repartí tareas chicas y concretas de estas materias a lo largo de los días disponibles. NO pongas las 6 el mismo día: distribuilas (ej. lunes Biology, martes Maths IGCSE, miércoles Business, etc.) según el espacio de cada día.
- Ejemplos de tarea chica: "1 past paper question de Maths IGCSE", "leer y anotar 2 páginas de Biology", "1 análisis corto de un texto de English Literature".
- Dentro de estas 6, priorizá las que el CONTEXTO ACTUAL de Franco marque como más difíciles o que necesiten más refuerzo.

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

def _build_contexto_checkins() -> str:
    """Tareas de ayer no cumplidas/parciales y tareas reprogramadas para hoy, para el prompt del plan diario."""
    ahora_ar = datetime.now(AR_TZ)
    hoy = ahora_ar.strftime("%d/%m/%Y")
    ayer = (ahora_ar - timedelta(days=1)).strftime("%d/%m/%Y")
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT tarea, proyecto, cumplido, nota FROM checkins_tareas "
        "WHERE fecha = ? AND (cumplido IS NULL OR cumplido != 'si')",
        (ayer,),
    )
    pendientes = c.fetchall()
    c.execute(
        "SELECT tarea, proyecto FROM tareas_reprogramadas WHERE fecha_nueva = ?",
        (hoy,),
    )
    reprogramadas = c.fetchall()
    conn.close()
    partes = []
    if pendientes:
        lineas = []
        for tarea, proyecto, cumplido, nota in pendientes:
            extra = f" (llegó hasta: {nota})" if cumplido == "parcial" and nota else ""
            lineas.append(f"- {tarea} [{proyecto}]{extra}")
        partes.append("TAREAS DE AYER NO CUMPLIDAS (consideralas para hoy si siguen siendo relevantes):\n" + "\n".join(lineas))
    if reprogramadas:
        lineas = [f"- {t} [{p}]" for t, p in reprogramadas]
        partes.append("TAREAS REPROGRAMADAS PARA HOY (incluilas en el plan):\n" + "\n".join(lineas))
    if not partes:
        return ""
    return "\n\n".join(partes) + "\n\n"

def generar_plan_texto():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)

    ahora_ar = datetime.now(AR_TZ)
    # El plan diario es para HOY: se genera a las 06:00 del mismo dia que aplica.
    dia_plan_dt = ahora_ar

    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }
    dia_semana = dias_es[ahora_ar.weekday()].capitalize()
    dia_plan = dias_es[dia_plan_dt.weekday()].capitalize()
    dia_plan_upper = dia_plan.upper()
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    fecha_plan = dia_plan_dt.strftime("%d/%m/%Y")

    rows = get_fechas()
    # Separar eventos de HOY: pasan el filtro >= hoy (a las 06:00 aún no ocurrieron),
    # pero NO deben llegar como "fechas próximas" estudiables — ocurren durante el día
    # (horario escolar) y a la noche ya habrán pasado.
    hoy_date = ahora_ar.date()
    # Endurecido: eventos de HOY SIN horario (examenes/eventos en horario escolar) se
    # descartan por completo — no llegan ni como texto al prompt. Solo se muestran
    # los de hoy CON horario propio (ej: "todo el día", "18:00"), que afectan la
    # estructura del día.
    rows_hoy = [r for r in rows if _parse_ddmmyyyy(r[1]) == hoy_date and r[3]]
    rows_futuras = [r for r in rows if _parse_ddmmyyyy(r[1]) is not None and _parse_ddmmyyyy(r[1]) > hoy_date]
    fechas_str = _format_fechas_para_prompt(rows_futuras) if rows_futuras else "No hay fechas cargadas."

    if rows_hoy:
        lineas_hoy = []
        for r in rows_hoy:
            lineas_hoy.append(f"- {r[2]} - Horario: {r[3]}")
        eventos_hoy = (
            "EVENTOS DE HOY (ocurren durante el día de hoy):\n" + "\n".join(lineas_hoy) + "\n"
            "REGLA CRÍTICA sobre estos eventos: cuando llegue el bloque de estudio de esta noche YA HABRÁN OCURRIDO. "
            "NO asignes estudio, repaso ni preparación para ellos — ni esta noche ni en ningún bloque del plan. "
            "Solo tenelos en cuenta si cambian la estructura del día (ej: un evento de 'todo el día' bloquea el día completo).\n\n"
        )
    else:
        eventos_hoy = ""

    if is_feriado(fecha_plan):
        contexto_feriado = (
            "DÍA ESPECIAL: Hoy es feriado o no hay colegio. No incluyas bloque de colegio "
            "ni horario de levantarse a las 7:30. Tratalo como día libre — Franco puede organizar "
            "su tiempo desde cuando quiera. Mantené los entrenamientos si corresponde al día de la semana.\n\n"
        )
    else:
        contexto_feriado = ""

    rutina_mod = get_rutina_modificada(fecha_plan)
    if rutina_mod:
        contexto_rutina = (
            f"CAMBIO DE RUTINA PARA HOY: {rutina_mod}\n"
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
            dia_key = dias_map.get(dia_plan_dt.weekday(), "")
            if dia_key in plan_json:
                dp = plan_json[dia_key]
                contexto_plan_semanal = (
                    f"\nPLAN SEMANAL PARA HOY:\n"
                    f"- Prioridad: {dp.get('prioridad', '')}\n"
                    f"- Estudio principal: {dp.get('estudio_principal', '')}\n"
                    f"- Secundario: {dp.get('secundario', '')}\n"
                    f"- Nota: {dp.get('nota', '')}\n"
                )
        except Exception:
            pass

    prompt = PROMPT_DIA.format(
        fechas_db=fechas_str,
        eventos_hoy=eventos_hoy,
        contexto_franco=_build_contexto_prompt(),
        dia_semana=dia_semana,
        fecha_hoy=fecha_hoy,
        dia_plan=dia_plan,
        dia_plan_upper=dia_plan_upper,
        fecha_plan=fecha_plan,
        contexto_feriado=contexto_feriado,
        contexto_rutina=contexto_rutina,
        contexto_rutina_fija=contexto_rutina_fija,
        contexto_checkins=_build_contexto_checkins(),
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
    primera_linea_correcta = f"📅 PLAN {dia_plan_upper} {fecha_plan} — FRANCO"
    if lineas_plan and lineas_plan[0].strip().startswith("📅"):
        lineas_plan[0] = primera_linea_correcta
        plan = "\n".join(lineas_plan)

    save_plan(fecha_plan, plan)
    return plan, fecha_plan


async def generar_plan_semanal(app, semana_inicio: str = None):
    """Genera y guarda el plan semanal.
    - Sin argumento: semana próxima (job automático de los domingos 21:00).
    - Con semana_inicio (YYYY-MM-DD): esa semana (usado para regenerar la semana en curso)."""
    logger.info(f"Generando plan semanal (semana_inicio={semana_inicio})...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=180.0)
        if semana_inicio is None:
            semana_inicio = _lunes_semana_proxima()
        fecha_lunes_dt = datetime.strptime(semana_inicio, "%Y-%m-%d")
        fecha_lunes_str = fecha_lunes_dt.strftime("%d/%m/%Y")

        rows = get_fechas()
        fechas_str = _format_fechas_para_prompt(rows)

        rows_perm = get_rutinas_permanentes()
        rutina_perm_str = "\n".join(f"- {d}" for _, _, d in rows_perm) if rows_perm else "Sin modificaciones permanentes."

        prompt = PROMPT_PLAN_SEMANAL.format(
            fechas_db=fechas_str,
            contexto_franco=_build_contexto_prompt(),
            rutina_permanente=rutina_perm_str,
            fecha_lunes=fecha_lunes_str,
            fecha_hoy=datetime.now(AR_TZ).strftime("%d/%m/%Y"),
        )
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        plan_text = message.content[0].text.strip()
        # Extraer bloque JSON (primer { al último })
        json_start = plan_text.find('{')
        json_end = plan_text.rfind('}') + 1
        if json_start == -1 or json_end <= json_start:
            raise ValueError("No se encontró bloque JSON en la respuesta de Claude.")
        plan_json_str = plan_text[json_start:json_end]
        # Limpiar caracteres problemáticos y trailing commas
        plan_json_str = (
            plan_json_str
            .replace('\u201c', '"').replace('\u201d', '"')
            .replace('\u2018', "'").replace('\u2019', "'")
            .replace('\u2026', '...')
        )
        # Eliminar trailing commas antes de } o ] (error frecuente de modelos)
        import re as _re
        plan_json_str = _re.sub(r',\s*([\}\]])', r'\1', plan_json_str)
        # Intentar parsear; si falla, avisar con mensaje claro
        try:
            json.loads(plan_json_str)
        except json.JSONDecodeError as json_err:
            logger.error(f"JSON inválido en plan semanal: {json_err}\nTexto: {plan_json_str[:400]}")
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⚠️ El plan se generó pero Claude devolvió JSON con formato incorrecto.\n"
                    f"Error: {json_err}\n\n"
                    f"Intentá de nuevo con /plansemanal."
                )
            )
            return
        save_plan_semanal(semana_inicio, plan_json_str)
        logger.info(f"Plan semanal guardado para semana {semana_inicio}.")
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"📆 Plan semanal generado para la semana del {fecha_lunes_str}.\nTocá 'Esta semana' para verlo."
        )
    except Exception as e:
        logger.error(f"Error generando plan semanal: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al generar plan semanal: {e}")


def _fecha_cae_esta_semana(fecha_ddmmyyyy: str) -> bool:
    """True si la fecha cae entre hoy y el domingo de la semana en curso (inclusive)."""
    try:
        d, m, y = fecha_ddmmyyyy.split("/")
        fecha_d = datetime(int(y), int(m), int(d)).date()
    except Exception:
        return False
    ahora = datetime.now(AR_TZ)
    hoy = ahora.date()
    domingo = (ahora + timedelta(days=6 - ahora.weekday())).date()
    return hoy <= fecha_d <= domingo


async def regenerar_por_fecha_nueva(app):
    """Regenera el plan semanal de la semana en curso y el plan diario de hoy.
    Se llama cuando Franco carga una fecha nueva que cae dentro de la semana en curso.
    (Franco prefiere precisión sobre ahorro de tokens.)"""
    await generar_plan_semanal(app, semana_inicio=_lunes_semana_actual())
    try:
        plan, fecha_plan = generar_plan_texto()
        refrescar_tareas_del_plan(fecha_plan, plan)
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"📅 Plan de hoy actualizado ({fecha_plan}):\n\n{plan}",
        )
    except Exception as e:
        logger.error(f"Error regenerando plan diario tras fecha nueva: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al regenerar el plan de hoy: {e}")


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
            InlineKeyboardButton("📝 Mi contexto", callback_data="contexto"),
        ],
        [
            InlineKeyboardButton("⚙️ Editar rutina fija", callback_data="rutina_fija"),
        ],
    ])

def _build_contexto_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 Materia", callback_data="ctx_cat|materia"),
            InlineKeyboardButton("🎯 Proyecto", callback_data="ctx_cat|proyecto"),
        ],
        [
            InlineKeyboardButton("🗓️ General", callback_data="ctx_cat|general"),
            InlineKeyboardButton("➕ Nueva categoría", callback_data="ctx_cat_nueva"),
        ],
        [InlineKeyboardButton("📖 Ver mi contexto", callback_data="ctx_ver")],
    ])

# Preguntas de texto libre por categoría
_CTX_PROMPTS = {
    "materia": "Contame qué materia y qué cambió\n(ej: Biology — necesito reforzar mucho para el IGCSE de octubre)",
    "proyecto": "Contame qué proyecto y su estado\n(ej: OMA — arranco preparación, prioridad alta)",
    "general": "Contame el dato general\n(ej: sueño — durmiendo mejor esta semana)",
}

def _extraer_clave_valor(texto: str):
    """Extrae (clave, valor) de texto libre. Separadores: — – - : |. Si no hay, primera palabra = clave."""
    for sep in ["—", "–", " - ", ":", "|"]:
        if sep in texto:
            clave, _, valor = texto.partition(sep)
            clave, valor = clave.strip(), valor.strip()
            if clave and valor:
                return clave, valor
    partes = texto.strip().split(None, 1)
    if len(partes) == 2:
        return partes[0].strip(), partes[1].strip()
    return texto.strip(), texto.strip()

def _canonicalizar_clave(clave: str) -> str:
    """Si la clave coincide (exacta o parcial, case-insensitive) con una materia/proyecto ya conocido, usa el nombre canónico."""
    conocidas = [e[1] for e in CONTEXTO_INICIAL] + [r[1] for r in get_contexto_franco()]
    cl = clave.lower().strip()
    if not cl:
        return clave
    for nombre in conocidas:
        if nombre.lower() == cl:
            return nombre
    for nombre in conocidas:
        nl = nombre.lower()
        if cl in nl or nl in cl:
            return nombre
    return clave

def _texto_contexto_actual() -> str:
    rows = get_contexto_franco()
    if not rows:
        return "📖 CONTEXTO ACTUAL\n\nℹ️ Sin contexto cargado todavía. Tocá una categoría para agregar."
    by_cat = {}
    for cat, clave, valor in rows:
        by_cat.setdefault(cat, []).append((clave, valor))
    cat_labels = {"materia": "📚 Materias", "proyecto": "🎯 Proyectos", "general": "🗓️ General"}
    lines = ["📖 CONTEXTO ACTUAL\n"]
    for cat in ["materia", "proyecto", "general"]:
        if cat not in by_cat:
            continue
        lines.append(cat_labels[cat])
        for clave, valor in by_cat[cat]:
            lines.append(f"  • {clave}: {valor}")
        lines.append("")
    for cat, items in by_cat.items():
        if cat not in cat_labels:
            lines.append(f"  {cat.capitalize()}")
            for clave, valor in items:
                lines.append(f"  • {clave}: {valor}")
            lines.append("")
    return "\n".join(lines)[:4000]

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
    "desayuno": "🍳",
    "media_mañana": "🍎",
    "almuerzo": "☀️",
    "merienda": "🌤️",
    "cena": "🌙",
}

def _texto_dieta_hoy(dia: str, rows) -> str:
    tipo, objetivo = DIA_TIPO_OBJETIVO.get(dia, ("", "2800-3000"))
    titulo = dia.capitalize()
    texto = f"🥗 DIETA DE HOY — {titulo}" + (f" ({tipo})" if tipo else "") + "\n"
    total = 0
    hay_sin_kcal = False
    for _, momento, descripcion, hora, kcal in rows:
        emoji = _MOMENTO_EMOJI.get(momento, "🍽️")
        if kcal:
            kcal_str = f" — {kcal} kcal"
            total += kcal
        else:
            kcal_str = " — kcal s/d"
            hay_sin_kcal = True
        texto += f"\n{emoji} {momento.replace('_', ' ').capitalize()} ({hora}){kcal_str}\n{descripcion}\n"
    aprox = "~" if hay_sin_kcal else ""
    texto += f"\n📊 Total del día: {aprox}{total} / {objetivo} kcal objetivo"
    if hay_sin_kcal:
        texto += "\n(s/d: comida editada a mano, kcal no calculada)"
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
        c.execute("SELECT momento, descripcion, hora_recordatorio, kcal FROM comidas WHERE dia = ? ORDER BY hora_recordatorio", (dia,))
        rows = c.fetchall()
        if not rows:
            continue
        label = dias_labels.get(dia, dia.upper())
        texto += f"📅 {label}\n"
        total = 0
        for momento, descripcion, hora, kcal in rows:
            emoji = _MOMENTO_EMOJI.get(momento, "🍽️")
            kcal_str = f" — {kcal} kcal" if kcal else ""
            if kcal:
                total += kcal
            texto += f"{emoji} {momento.replace('_', ' ').capitalize()} ({hora}): {descripcion}{kcal_str}\n"
        texto += f"📊 Total: {total} kcal\n\n"
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
    if _fecha_cae_esta_semana(fecha):
        await update.message.reply_text(
            "🔄 La fecha cae en la semana en curso — regenerando plan semanal y plan de hoy..."
        )
        await regenerar_por_fecha_nueva(context.application)

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
            "El plan de hoy se genera automáticamente a las 06:00.\n"
            "Usá /generar para crearlo ahora."
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
            contexto_franco=_build_contexto_prompt(),
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


async def cmd_contexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /contexto [clave] [descripción libre]"""
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "📝 MI CONTEXTO\n\n¿Qué querés cargar o revisar?\n"
            "(atajo rápido: /contexto [clave] [descripción])",
            reply_markup=_build_contexto_menu_keyboard(),
        )
        return
    clave = args[0]
    valor = " ".join(args[1:])
    # Auto-detect category
    materias = {e[1] for e in CONTEXTO_INICIAL if e[0] == "materia"}
    proyectos = {e[1] for e in CONTEXTO_INICIAL if e[0] == "proyecto"}
    # Also check existing DB keys
    rows_db = get_contexto_franco()
    cat_map = {r[1]: r[0] for r in rows_db}
    if clave in cat_map:
        categoria = cat_map[clave]
    elif clave in materias:
        categoria = "materia"
    elif clave in proyectos:
        categoria = "proyecto"
    else:
        # Try partial match (case-insensitive)
        clave_lower = clave.lower()
        matched_cat = None
        for m in materias:
            if m.lower() == clave_lower or clave_lower in m.lower():
                clave = m
                matched_cat = "materia"
                break
        if not matched_cat:
            for p in proyectos:
                if p.lower() == clave_lower or clave_lower in p.lower():
                    clave = p
                    matched_cat = "proyecto"
                    break
        categoria = matched_cat or "general"
    upsert_contexto(categoria, clave, valor)
    await update.message.reply_text(f"✅ Contexto actualizado: {clave} → {valor}")


async def cmd_plansemanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan semanal con Claude, esperá un momento...")
    try:
        # A mitad de semana regenera la semana EN CURSO; los domingos genera la próxima (como el job).
        if datetime.now(AR_TZ).weekday() == 6:
            await generar_plan_semanal(context.application)
        else:
            await generar_plan_semanal(context.application, semana_inicio=_lunes_semana_actual())
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
            contexto_franco=_build_contexto_prompt(),
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
                "El plan de hoy se genera automáticamente a las 06:00.\n"
                "Tocá ⚡ Generar plan para crearlo ahora."
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
                "Sé muy específico y directo. Sin introducción, sin explicaciones largas.\n"
                "Usá SOLO la información provista arriba: no inventes actividades, horarios ni tareas. "
                "Si un evento ya ocurrió hoy más temprano, no lo menciones como pendiente.\n\n"
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
        ahora_ar = datetime.now(AR_TZ)
        dias_es = {
            0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
            4: "viernes", 5: "sábado", 6: "domingo",
        }
        dia_hoy = dias_es[ahora_ar.weekday()]
        rows = get_comidas_dia(dia_hoy)
        texto = _texto_dieta_hoy(dia_hoy, rows)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Ver semana completa", callback_data="dieta_semana")],
            [InlineKeyboardButton("✏️ Editar una comida", callback_data="editar_comida")],
        ])
        await query.edit_message_text(texto, reply_markup=kb)

    elif data == "dieta_semana":
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


    elif data == "contexto":
        await query.edit_message_text(
            "📝 MI CONTEXTO\n\n¿Qué querés cargar o revisar?",
            reply_markup=_build_contexto_menu_keyboard(),
        )

    elif data == "ctx_ver":
        await query.edit_message_text(
            _texto_contexto_actual(),
            reply_markup=_build_contexto_menu_keyboard(),
        )

    elif data.startswith("ctx_cat|"):
        categoria = data.split("|", 1)[1]
        context.user_data["ctx_categoria"] = categoria
        context.user_data["estado"] = "esperando_ctx_valor"
        pregunta = _CTX_PROMPTS.get(categoria, f"Contame qué cargar en '{categoria}' (ej: clave — descripción)")
        await query.edit_message_text(f"📝 {pregunta}")

    elif data == "ctx_cat_nueva":
        context.user_data["estado"] = "esperando_ctx_nueva_cat"
        await query.edit_message_text(
            "➕ ¿Cómo se llama la nueva categoría?\n(una palabra, ej: Salud, Lectura, Familia)"
        )

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
        if _fecha_cae_esta_semana(fecha):
            await update.message.reply_text(
                "🔄 La fecha cae en la semana en curso — regenerando plan semanal y plan de hoy..."
            )
            await regenerar_por_fecha_nueva(context.application)

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


    elif estado == 'esperando_nota_tarea':
        row_id = context.user_data.get('ci_tarea_row_id')
        if row_id:
            save_checkin_tarea_cumplido(int(row_id), "parcial", texto)
        context.user_data.clear()
        await update.message.reply_text(f"📍 Anotado: {texto}")

    elif estado == 'esperando_ctx_nueva_cat':
        categoria = texto.strip().lower().split()[0] if texto.strip() else "general"
        context.user_data["ctx_categoria"] = categoria
        context.user_data["estado"] = "esperando_ctx_valor"
        await update.message.reply_text(
            f"📝 Categoría '{categoria}'. Ahora contame qué cargar\n(ej: clave — descripción)"
        )

    elif estado == 'esperando_ctx_valor':
        categoria = context.user_data.get("ctx_categoria", "general")
        clave, valor = _extraer_clave_valor(texto)
        clave = _canonicalizar_clave(clave)
        upsert_contexto(categoria, clave, valor)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Contexto guardado en {categoria}:\n{clave} → {valor}",
            reply_markup=_build_contexto_menu_keyboard(),
        )

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
        WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) < date(?)
    """, (_hoy_ar_iso(),))
    conn.commit()
    conn.close()
    logger.info("Limpieza de fechas pasadas completada.")

async def job_plan_diario(app):
    """Genera y envia el plan de HOY — corre todos los dias a las 06:00 AR."""
    logger.info("Ejecutando cron job del plan diario (06:00)...")
    try:
        plan, fecha = generar_plan_texto()
        save_tareas_del_plan(fecha, plan)
        mensaje = f"🌅 Plan para hoy ({fecha}):\n\n{plan}"
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
        WHERE date(substr(fecha,7,4)||'-'||substr(fecha,4,2)||'-'||substr(fecha,1,2)) >= date(?)
        ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)
        LIMIT 10
    """, (_hoy_ar_iso(),))
    fechas = c.fetchall()
    conn.close()
    fechas_texto = "\n".join([
        f"{f[0]} {_etiqueta_relativa(f[0])} - {f[1]}"
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
        "Mencioná el material específico y para cuándo vence. "
        "Usá SOLO las fechas listadas arriba: no inventes eventos ni material. "
        "Si un evento es HOY, ya ocurrió en el colegio — no lo incluyas.\n\n"
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
    seed_contexto()
    seed_fechas_iniciales()

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
    app.add_handler(CommandHandler("contexto", cmd_contexto))
    app.add_handler(CommandHandler("plansemanal", cmd_plansemanal))
    app.add_handler(CommandHandler("mes", cmd_mes))
    app.add_handler(CommandHandler("proyectos", cmd_proyectos))
    app.add_handler(CommandHandler("rutina", cmd_rutina))

    # Botones inline
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Mensajes de texto (hola/menu + estados de conversación)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mensaje))

    # Scheduler — plan diario 06:00 hora Argentina (el plan del dia se genera esa misma manana)
    scheduler = AsyncIOScheduler(timezone=AR_TZ)
    scheduler.add_job(job_plan_diario, trigger="cron", hour=6, minute=0, args=[app])
    scheduler.add_job(limpiar_fechas_pasadas, trigger="cron", hour=0, minute=0)
    scheduler.add_job(recordatorio_uniforme, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=7, minute=25, args=[app])

    scheduler.add_job(marcar_checkins_tareas_sin_respuesta, trigger="cron", hour=10, minute=0)
    scheduler.add_job(generar_plan_semanal, trigger="cron", day_of_week="sun", hour=21, minute=0, args=[app])
    scheduler.add_job(resumen_semanal, trigger="cron", day_of_week="sun", hour=21, minute=0, args=[app])
    scheduler.add_job(recordatorio_mochila, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=16, minute=35, args=[app])
    _schedule_meal_reminders(scheduler, app)
    scheduler.start()
    logger.info("Scheduler iniciado. Plan diario programado para las 06:00 AR.")

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    
