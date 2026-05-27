import os
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
    conn.commit()
    conn.close()

def get_fechas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, fecha, evento, horario, material FROM fechas ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
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

# ── Contexto hardcodeado ──────────────────────────────────────────────────────

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
        await query.edit_message_text("⏳ Generando plan semanal, esperá un momento...")
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
            await _enviar_plan_multipartes(query.message, message.content[0].text)
        except Exception as e:
            logger.error(f"Error en semana callback: {e}")
            await query.message.reply_text(f"❌ Error al generar el plan semanal: {e}")

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

    elif texto.lower() in ['hola', 'menu', 'menú']:
        context.user_data.clear()
        await update.message.reply_text(
            "👋 Hola Franco! ¿Qué hacemos?",
            reply_markup=_build_main_keyboard(),
        )

# ── Cron job ──────────────────────────────────────────────────────

async def job_noche(app):
    logger.info("Ejecutando cron job nocturno...")
    try:
        plan, fecha = generar_plan_texto()
        mensaje = f"🌙 Plan para mañana ({fecha}):\n\n{plan}"
        await app.bot.send_message(chat_id=CHAT_ID, text=mensaje)
        logger.info("Plan enviado exitosamente.")
    except Exception as e:
        logger.error(f"Error en cron job: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al generar el plan automático: {e}")

# ── Main ────────────────────────────

def main():
    init_db()

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
    scheduler.start()
    logger.info("Scheduler iniciado. Cron job programado para las 22:00 AR.")

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
