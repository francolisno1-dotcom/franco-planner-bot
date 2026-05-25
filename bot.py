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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS fechas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            evento TEXT NOT NULL,
            material TEXT
        )
        """
    )
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
    c.execute(
        "INSERT INTO fechas (fecha, evento, material) VALUES (?, ?, ?)",
        (fecha, evento, material),
    )
    conn.commit()
    conn.close()


def delete_fecha_by_index(index: int) -> bool:
    """Delete row by 1-based position in the sorted list. Returns True if deleted."""
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


def save_plan(fecha: str, contenido: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS planes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            contenido TEXT NOT NULL
        )
        """
    )
    c.execute(
        "INSERT OR REPLACE INTO planes (fecha, contenido) VALUES (?, ?)",
        (fecha, contenido),
    )
    conn.commit()
    conn.close()


def get_plan(fecha: str):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS planes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            contenido TEXT NOT NULL
        )
        """
    )
    c.execute("SELECT contenido FROM planes WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# ── Contexto hardcodeado ──────────────────────────────────────────────────────

RUTINA = """🗓 *RUTINA SEMANAL*

• *Lunes:* Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
• *Martes:* Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
• *Miércoles:* Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
• *Jueves:* Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
• *Viernes:* Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Cena 21:00 → Estudio 20:30-22:30 → Dormir 22:30
• *Sábado:* Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
• *Domingo:* Gym 11:00-12:30 → tarde libre → Dormir 22:30"""

PROYECTOS = """📁 *PROYECTOS ACTIVOS*

• *OMA* (Olimpiadas Matemáticas Argentinas) — Deadline: 2 julio. Preparación: ejercicios de exámenes pasados.
• *MUN (ANU-AR)* — 26, 27 y 28 de junio. Representa Liberia en AG3. Preparar: tópicos de ANU-AR, discursos, posición de Liberia.
• *Debate WSDC (ADA)* — Sin fecha fija, práctica continua. Formato WSDC, mociones variadas.
• *NASA ISSDC — DESLA* — Competencia anual, preparación continua.
• *Materias IGCSE* — Siempre al día. Material en Kognity.
• *Marketing/Instagram* — Real Estate, sin deadline."""

PROMPT_TEMPLATE = """Sos el planificador personal de Franco, 15 años, que vive en Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: Colegio 8:30-17:00 → Fútbol 18:00-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Martes: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Miércoles: Colegio 8:30-17:00 → Gym 18:30-20:00 → Casa 20:15 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Jueves: Colegio 8:30-17:00 → Fútbol 18:30-19:30 → Casa 19:45 → Baño 15min → Cena 21:00 → Estudio 22:00-22:30 → Dormir 22:30
- Viernes: Colegio 8:30-17:00 → Tenis 18:00-19:00 → Casa 19:15 → Baño 15min → Cena 21:00 → Estudio 20:30-22:30 → Dormir 22:30
- Sábado: Partido fútbol 12:00-14:30 → tarde libre → Dormir 22:30
- Domingo: Gym 11:00-12:30 → tarde libre → Dormir 22:30

PROYECTOS ACTIVOS Y DEADLINES:
- OMA (Olimpiadas Matemáticas Argentinas) — deadline 2 julio. Preparación: ejercicios de exámenes pasados.
- MUN (ANU-AR) — 26, 27 y 28 de junio. Representa Liberia en AG3. Preparar: tópicos de ANU-AR, discursos, posición de Liberia.
- Debate WSDC (ADA) — sin fecha fija, práctica continua. Formato WSDC, mociones variadas.
- NASA ISSDC — DESLA — competencia anual, preparación continua.
- Materias IGCSE — siempre al día. Material en Kognity.
- Marketing/Instagram — Real Estate, sin deadline.

FECHAS PRÓXIMAS CARGADAS:
{fechas_db}

Hoy es {dia_semana} {fecha_hoy}. Generá el plan detallado para mañana ({fecha_manana}).

El plan debe:
- Respetar estrictamente los horarios de la rutina
- Asignar tareas de estudio al slot disponible según el día
- Priorizar por urgencia (deadline más cercano primero)
- Ser específico: no "estudiar MUN" sino "leer posición de Liberia sobre tópico X"
- Formato claro con emojis y horarios
- Terminar con una frase motivadora corta
"""

# ── Claude ────────────────────────────────────────────────────────────────────

def generar_plan_texto() -> str:
    """Llama a Claude y devuelve el plan generado como string."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    ahora_ar = datetime.now(AR_TZ)
    manana_ar = ahora_ar + timedelta(days=1)

    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }
    dia_semana = dias_es[ahora_ar.weekday()].capitalize()
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    fecha_manana = manana_ar.strftime("%d/%m/%Y")

    rows = get_fechas()
    if rows:
        fechas_str = "\n".join(
            f"- {r[1]}: {r[2]}" + (f" (Material: {r[3]})" if r[3] else "")
            for r in rows
        )
    else:
        fechas_str = "No hay fechas cargadas."

    prompt = PROMPT_TEMPLATE.format(
        fechas_db=fechas_str,
        dia_semana=dia_semana,
        fecha_hoy=fecha_hoy,
        fecha_manana=fecha_manana,
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    plan = message.content[0].text
    save_plan(fecha_manana, plan)
    return plan, fecha_manana


# ── Comandos Telegram ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola Franco! Soy tu planificador personal.\n\n"
        "Comandos disponibles:\n"
        "/fecha DD/MM/AAAA | Evento | Material — guardar una fecha\n"
        "/fechas — ver fechas próximas\n"
        "/borrar N — borrar la fecha número N\n"
        "/plan — ver el plan del día actual\n"
        "/generar — generar el plan de mañana ahora\n"
        "/proyectos — ver proyectos activos\n"
        "/rutina — ver rutina semanal\n\n"
        "El plan del día siguiente se genera automáticamente a las 22:00 🕙"
    )


async def cmd_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args)
    partes = [p.strip() for p in texto.split("|")]
    if len(partes) < 2:
        await update.message.reply_text(
            "❌ Formato: /fecha DD/MM/AAAA | Evento | Material (el material es opcional)"
        )
        return
    fecha = partes[0]
    evento = partes[1]
    material = partes[2] if len(partes) >= 3 else ""
    # Validate date format
    try:
        datetime.strptime(fecha, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("❌ Fecha inválida. Usá el formato DD/MM/AAAA")
        return
    save_fecha(fecha, evento, material)
    await update.message.reply_text(f"✅ Fecha guardada: *{fecha}* — {evento}", parse_mode="Markdown")


async def cmd_fechas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_fechas()
    if not rows:
        await update.message.reply_text("📭 No hay fechas cargadas.")
        return
    lines = []
    for i, (_, fecha, evento, material) in enumerate(rows, 1):
        mat = f" _(Material: {material})_" if material else ""
        lines.append(f"{i}. *{fecha}* — {evento}{mat}")
    await update.message.reply_text(
        "📅 *FECHAS PRÓXIMAS:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Uso: /borrar N (el número de la fecha en la lista)")
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


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now(AR_TZ).strftime("%d/%m/%Y")
    plan = get_plan(hoy)
    if plan:
        await update.message.reply_text(f"📋 *Plan para hoy ({hoy}):*\n\n{plan}", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"📭 No hay plan guardado para hoy ({hoy}).\n"
            "Usá /generar para crear el plan de mañana."
        )


async def cmd_generar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando plan con Claude, esperá un momento...")
    try:
        plan, fecha = generar_plan_texto()
        await update.message.reply_text(
            f"✅ *Plan generado para {fecha}:*\n\n{plan}", parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error generando plan: {e}")
        await update.message.reply_text(f"❌ Error al generar el plan: {e}")


async def cmd_proyectos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PROYECTOS, parse_mode="Markdown")


async def cmd_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RUTINA, parse_mode="Markdown")


# ── Cron job ──────────────────────────────────────────────────────────────────

async def job_noche(app):
    """Cron job que corre a las 22:00 hora Argentina."""
    logger.info("Ejecutando cron job nocturno...")
    try:
        plan, fecha = generar_plan_texto()
        mensaje = f"🌙 *Plan automático para mañana ({fecha}):*\n\n{plan}"
        await app.bot.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")
        logger.info("Plan enviado exitosamente.")
    except Exception as e:
        logger.error(f"Error en cron job: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error al generar el plan automático: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fecha", cmd_fecha))
    app.add_handler(CommandHandler("fechas", cmd_fechas))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("generar", cmd_generar))
    app.add_handler(CommandHandler("proyectos", cmd_proyectos))
    app.add_handler(CommandHandler("rutina", cmd_rutina))

    # Scheduler — 22:00 hora Argentina
    scheduler = AsyncIOScheduler(timezone=AR_TZ)
    scheduler.add_job(
        job_noche,
        trigger="cron",
        hour=22,
        minute=0,
        args=[app],
    )
    scheduler.start()
    logger.info("Scheduler iniciado. Cron job programado para las 22:00 AR.")

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
