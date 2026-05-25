import os
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Config
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID = int(os.environ["CHAT_ID"])

AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
    conn.commit()
    conn.close()

def get_fechas():
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("SELECT id, fecha, evento, material FROM fechas ORDER BY substr(fecha,7,4)||substr(fecha,4,2)||substr(fecha,1,2)")
    rows = c.fetchall()
    conn.close()
    return rows

def save_fecha(fecha, evento, material):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("INSERT INTO fechas (fecha, evento, material) VALUES (?, ?, ?)", (fecha, evento, material))
    conn.commit()
    conn.close()

def delete_fecha_by_index(index):
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

def save_plan(fecha, contenido):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS planes (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL UNIQUE, contenido TEXT NOT NULL)")
    c.execute("INSERT OR REPLACE INTO planes (fecha, contenido) VALUES (?, ?)", (fecha, contenido))
    conn.commit()
    conn.close()

def get_plan(fecha):
    conn = sqlite3.connect("planner.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS planes (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL UNIQUE, contenido TEXT NOT NULL)")
    c.execute("SELECT contenido FROM planes WHERE fecha = ?", (fecha,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

RUTINA = """RUTINA SEMANAL:
- Lunes: Colegio 8:30-17:00 -> Futbol 18:00-19:30 -> Casa 19:45 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Martes: Colegio 8:30-17:00 -> Gym 18:30-20:00 -> Casa 20:15 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Miercoles: Colegio 8:30-17:00 -> Gym 18:30-20:00 -> Casa 20:15 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Jueves: Colegio 8:30-17:00 -> Futbol 18:30-19:30 -> Casa 19:45 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Viernes: Colegio 8:30-17:00 -> Tenis 18:00-19:00 -> Casa 19:15 -> Bano 15min -> Cena 21:00 -> Estudio 20:30-22:30 -> Dormir 22:30
- Sabado: Partido futbol 12:00-14:30 -> tarde libre -> Dormir 22:30
- Domingo: Gym 11:00-12:30 -> tarde libre -> Dormir 22:30"""

PROYECTOS_MSG = """PROYECTOS ACTIVOS:
- OMA (Olimpiadas Matematicas Argentinas) - Deadline: 2 julio
- MUN (ANU-AR) - 26, 27 y 28 de junio. Liberia en AG3.
- Debate WSDC (ADA) - practica continua
- NASA ISSDC - DESLA - preparacion continua
- Materias IGCSE - siempre al dia
- Marketing/Instagram - Real Estate"""

PROMPT_TEMPLATE = """Sos el planificador personal de Franco, 15 anios, que vive en Hudson, Buenos Aires.

RUTINA SEMANAL:
- Lunes: Colegio 8:30-17:00 -> Futbol 18:00-19:30 -> Casa 19:45 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Martes: Colegio 8:30-17:00 -> Gym 18:30-20:00 -> Casa 20:15 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Miercoles: Colegio 8:30-17:00 -> Gym 18:30-20:00 -> Casa 20:15 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Jueves: Colegio 8:30-17:00 -> Futbol 18:30-19:30 -> Casa 19:45 -> Bano 15min -> Cena 21:00 -> Estudio 22:00-22:30 -> Dormir 22:30
- Viernes: Colegio 8:30-17:00 -> Tenis 18:00-19:00 -> Casa 19:15 -> Bano 15min -> Cena 21:00 -> Estudio 20:30-22:30 -> Dormir 22:30
- Sabado: Partido futbol 12:00-14:30 -> tarde libre -> Dormir 22:30
- Domingo: Gym 11:00-12:30 -> tarde libre -> Dormir 22:30

PROYECTOS ACTIVOS Y DEADLINES:
- OMA deadline 2 julio. Preparacion: ejercicios de examenes pasados.
- MUN (ANU-AR) 26, 27 y 28 de junio. Representa Liberia en AG3.
- Debate WSDC (ADA) sin fecha fija, practica continua.
- NASA ISSDC DESLA competencia anual, preparacion continua.
- Materias IGCSE siempre al dia. Material en Kognity.
- Marketing/Instagram Real Estate, sin deadline.

FECHAS PROXIMAS CARGADAS:
{fechas_db}

Hoy es {dia_semana} {fecha_hoy}. Genera el plan detallado para maniana ({fecha_manana}).

El plan debe:
- Respetar estrictamente los horarios de la rutina
- Priorizar por urgencia (deadline mas cercano primero)
- Ser especifico con cada tarea
- Formato claro con emojis y horarios
- Terminar con una frase motivadora corta
"""

def generar_plan_texto():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    ahora_ar = datetime.now(AR_TZ)
    manana_ar = ahora_ar + timedelta(days=1)
    dias_es = {0:"lunes",1:"martes",2:"miercoles",3:"jueves",4:"viernes",5:"sabado",6:"domingo"}
    dia_semana = dias_es[ahora_ar.weekday()].capitalize()
    fecha_hoy = ahora_ar.strftime("%d/%m/%Y")
    fecha_manana = manana_ar.strftime("%d/%m/%Y")
    rows = get_fechas()
    fechas_str = "\n".join(f"- {r[1]}: {r[2]}" + (f" (Material: {r[3]})" if r[3] else "") for r in rows) if rows else "No hay fechas cargadas."
    prompt = PROMPT_TEMPLATE.format(fechas_db=fechas_str, dia_semana=dia_semana, fecha_hoy=fecha_hoy, fecha_manana=fecha_manana)
    message = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1500, messages=[{"role":"user","content":prompt}])
    plan = message.content[0].text
    save_plan(fecha_manana, plan)
    return plan, fecha_manana

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola Franco! Soy tu planificador personal.\n\n"
        "Comandos:\n/fecha DD/MM/AAAA | Evento | Material\n"
        "/fechas\n/borrar N\n/plan\n/generar\n/proyectos\n/rutina\n\n"
        "El plan se genera automaticamente a las 22:00"
    )

async def cmd_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args)
    partes = [p.strip() for p in texto.split("|")]
    if len(partes) < 2:
        await update.message.reply_text("Formato: /fecha DD/MM/AAAA | Evento | Material")
        return
    fecha, evento = partes[0], partes[1]
    material = partes[2] if len(partes) >= 3 else ""
    try:
        datetime.strptime(fecha, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("Fecha invalida. Usa DD/MM/AAAA")
        return
    save_fecha(fecha, evento, material)
    await update.message.reply_text(f"Fecha guardada: {fecha} - {evento}")

async def cmd_fechas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_fechas()
    if not rows:
        await update.message.reply_text("No hay fechas cargadas.")
        return
    lines = [f"{i}. {r[1]} - {r[2]}" + (f" (Material: {r[3]})" if r[3] else "") for i, r in enumerate(rows, 1)]
    await update.message.reply_text("FECHAS PROXIMAS:\n\n" + "\n".join(lines))

async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /borrar N")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El argumento debe ser un numero.")
        return
    if delete_fecha_by_index(n):
        await update.message.reply_text(f"Fecha #{n} eliminada.")
    else:
        await update.message.reply_text(f"No existe la fecha numero {n}.")

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now(AR_TZ).strftime("%d/%m/%Y")
    plan = get_plan(hoy)
    if plan:
        await update.message.reply_text(f"Plan para hoy ({hoy}):\n\n{plan}")
    else:
        await update.message.reply_text(f"No hay plan para hoy ({hoy}). Usa /generar.")

async def cmd_generar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generando plan con Claude...")
    try:
        plan, fecha = generar_plan_texto()
        await update.message.reply_text(f"Plan para {fecha}:\n\n{plan}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Error al generar el plan: {e}")

async def cmd_proyectos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PROYECTOS_MSG)

async def cmd_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RUTINA)

async def job_noche(app):
    logger.info("Ejecutando cron job nocturno...")
    try:
        plan, fecha = generar_plan_texto()
        await app.bot.send_message(chat_id=CHAT_ID, text=f"Plan para maniana ({fecha}):\n\n{plan}")
    except Exception as e:
        logger.error(f"Error en cron: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"Error generando plan: {e}")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fecha", cmd_fecha))
    app.add_handler(CommandHandler("fechas", cmd_fechas))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("generar", cmd_generar))
    app.add_handler(CommandHandler("proyectos", cmd_proyectos))
    app.add_handler(CommandHandler("rutina", cmd_rutina))
    scheduler = AsyncIOScheduler(timezone=AR_TZ)
    # TEST: cron a las 00:39 AR — revertir a hour=22, minute=0 despues
    scheduler.add_job(job_noche, trigger="cron", hour=0, minute=39, args=[app])
    scheduler.start()
    logger.info("Bot y scheduler iniciados.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
