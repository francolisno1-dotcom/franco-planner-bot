import os
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS fechas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            evento TEXT NOT NULL,
            material TEXT
        )
    """)
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
    conn.commit()
    conn.close()


def get_fechas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        "SELECT id, fecha, evento, material FROM fechas ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def save_fecha(fecha: str, evento: str, material: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT INTO fechas (fecha, evento, material) VALUES (?, ?, ?)", (fecha, evento, material))
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


# ── Contexto hardcodeado ──────────────────────────────────────────────────────

RUTINA = """🗓 RUTINA SEMANAL

Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Cena 21:00 → Estudio 20:30-22:30 → Dormir 22:30
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
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Cena 21:00 → Estudio 20:30-22:30 → Dormir 22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
- Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30

PROYECTOS Y DEADLINES:
- OMA — deadline 2 julio. Ejercicios de exámenes pasados.
- MUN (ANU-AR) — 26, 27 y 28 de junio. Liberia en AG3. Tópicos, discursos, posición.
- Debate WSDC (ADA) — práctica continua. Formato WSDC.
- NASA ISSDC — DESLA — preparación continua.
- Materias IGCSE — al día. Material en Kognity.
- Marketing/Instagram — Real Estate, sin deadline.

FECHAS PRÓXIMAS CARGADAS:
{fechas_db}

{contexto_feriado}Hoy es {dia_semana} {fecha_hoy}. Generá el plan para mañana ({dia_manana} {fecha_manana}).

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
- Priorizá por urgencia (deadline más cercano primero).
- Si el día siguiente no tiene entrenamiento (domingo libre o similar), omitís 💪 y 🚿.
- Si hay DÍA ESPECIAL indicado arriba, omitís el bloque 🎓 colegio y adaptás el plan a día libre.
"""

PROMPT_SEMANA = """Sos el planificador personal de Franco, 15 años, Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: estudio disponible 22:00-22:30
- Martes: estudio disponible 22:00-22:30
- Miércoles: estudio disponible 22:00-22:30
- Jueves: estudio disponible 22:00-22:30
- Viernes: estudio disponible 20:30-22:30
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

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 7 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Distribuí por urgencia, deadline más cercano primero
- Respetá los slots de estudio según el día

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
- Viernes: estudio disponible 20:30-22:30
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

Hoy es {fecha_hoy}. Generá un plan de preparación para los próximos 30 días.

IMPORTANTE:
- Máximo 2 tareas por día
- Una línea por tarea, sin explicaciones ni justificaciones
- Distribuí por urgencia, deadline más cercano primero
- Respetá los slots de estudio según el día

FORMATO — sin markdown, sin símbolos extra:

📅 PRÓXIMOS 30 DÍAS

[Día] [DD/MM]
→ [Proyecto]: [tarea específica]
→ [Proyecto]: [tarea específica]

(Solo incluir días que tengan algo asignado)
"""

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
    fechas_str = "\n".join(
        f"- {r[1]}: {r[2]}" + (f" (Material: {r[3]})" if r[3] else "")
        for r in rows
    ) if rows else "No hay fechas cargadas."

    if is_feriado(fecha_manana):
        contexto_feriado = "DÍA ESPECIAL: Mañana es feriado o no hay colegio. No incluyas bloque de colegio ni horario de levantarse a las 7:30. Tratalo como día libre — Franco puede organizar su tiempo desde cuando quiera. Mantené los entrenamientos si corresponde al día de la semana.\n\n"
    else:
        contexto_feriado = ""

    prompt = PROMPT_DIA.format(
        fechas_db=fechas_str,
        dia_semana=dia_semana,
        fecha_hoy=fecha_hoy,
        dia_manana=dia_manana,
        dia_manana_upper=dia_manana_upper,
        fecha_manana=fecha_manana,
        contexto_feriado=contexto_feriado,
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    plan = message.content[0].text
    save_plan(fecha_manana, plan)
    return plan, fecha_manana


async def _enviar_plan_multipartes(update, texto: str):
    """Envía el texto en partes si supera 4096 chars. Divide por líneas de días."""
    if len(texto) <= 4096:
        await update.message.reply_text(texto)
        return

    # Intentar dividir en bloques de días (líneas que empiezan con nombre de día)
    lineas = texto.split("\n")
    dias_nombres = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

    # Encontrar índices donde empiezan los días
    indices_dias = []
    for i, linea in enumerate(lineas):
        for nombre in dias_nombres:
            if linea.strip().startswith(nombre):
                indices_dias.append(i)
                break

    if len(indices_dias) >= 2:
        # Dividir: días 1-4 en primera parte, resto en segunda
        corte = indices_dias[min(4, len(indices_dias) - 1)]
        parte1 = "\n".join(lineas[:corte]).strip()
        parte2 = "\n".join(lineas[corte:]).strip()
        if parte1:
            await update.message.reply_text(parte1)
        if parte2:
            await update.message.reply_text(parte2)
    else:
        # Fallback: cortar en 4096 chars en un salto de línea
        chunk1 = texto[:4000].rsplit("\n", 1)[0]
        chunk2 = texto[len(chunk1):].strip()
        await update.message.reply_text(chunk1)
        if chunk2:
            await update.message.reply_text(chunk2)


# ── Comandos Telegram ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola Franco! Soy tu planificador personal.\n\n"
        "Comandos disponibles:\n"
        "/fecha DD/MM/AAAA | Evento | Material — guardar una fecha\n"
        "/fechas — ver fechas próximas\n"
        "/borrar N — borrar la fecha número N\n"
        "/feriado DD/MM/AAAA — marcar un día como feriado\n"
        "/feriados — ver feriados cargados\n"
        "/borrarf N — borrar el feriado número N\n"
        "/plan — ver el plan de hoy\n"
        "/generar — generar el plan de mañana ahora\n"
        "/semana — plan de estudio para los próximos 7 días\n"
        "/mes — plan de estudio para los próximos 30 días\n"
        "/proyectos — ver proyectos activos\n"
        "/rutina — ver rutina semanal\n\n"
        "El plan del día siguiente se genera automáticamente a las 22:00 🕙"
    )


async def cmd_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args)
    partes = [p.strip() for p in texto.split("|")]
    if len(partes) < 2:
        await update.message.reply_text("❌ Formato: /fecha DD/MM/AAAA | Evento | Material (el material es opcional)")
        return
    fecha = partes[0]
    evento = partes[1]
    material = partes[2] if len(partes) >= 3 else ""
    try:
        datetime.strptime(fecha, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("❌ Fecha inválida. Usá el formato DD/MM/AAAA")
        return
    save_fecha(fecha, evento, material)
    await update.message.reply_text(f"✅ Fecha guardada: {fecha} — {evento}")


async def cmd_fechas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_fechas()
    if not rows:
        await update.message.reply_text("📭 No hay fechas cargadas.")
        return
    lines = []
    for i, (_, fecha, evento, material) in enumerate(rows, 1):
        mat = f" (Material: {material})" if material else ""
        lines.append(f"{i}. {fecha} — {evento}{mat}")
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
        fecha_fin = (ahora_ar + timedelta(days=7)).strftime("%d/%m/%Y")

        rows = get_fechas()
        fechas_str = "\n".join(
            f"- {r[1]}: {r[2]}" + (f" (Material: {r[3]})" if r[3] else "")
            for r in rows
        ) if rows else "No hay fechas cargadas."

        prompt = PROMPT_SEMANA.format(
            fechas_db=fechas_str,
            fecha_hoy=fecha_hoy,
            fecha_fin=fecha_fin,
        )

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = message.content[0].text
        await _enviar_plan_multipartes(update, texto)
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
        fechas_str = "\n".join(
            f"- {r[1]}: {r[2]}" + (f" (Material: {r[3]})" if r[3] else "")
            for r in rows
        ) if rows else "No hay fechas cargadas."

        prompt = PROMPT_MES.format(
            fechas_db=fechas_str,
            fecha_hoy=fecha_hoy,
        )

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = message.content[0].text
        await _enviar_plan_multipartes(update, texto)
    except Exception as e:
        logger.error(f"Error en /mes: {e}")
        await update.message.reply_text(f"❌ Error al generar el plan mensual: {e}")


async def cmd_proyectos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PROYECTOS)


async def cmd_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RUTINA)


# ── Cron job ──────────────────────────────────────────────────────────────────

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


# ── Main ──────────────────────────────────────────────────────────────────────

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

    # Scheduler — 22:00 hora Argentina
    scheduler = AsyncIOScheduler(timezone=AR_TZ)
    scheduler.add_job(job_noche, trigger="cron", hour=22, minute=0, args=[app])
    scheduler.start()
    logger.info("Scheduler iniciado. Cron job programado para las 22:00 AR.")

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
