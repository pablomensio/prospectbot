"""
Worker de Envío Asíncrono.
Corre en background mientras FastAPI está levantado.
Principios anti-ban: delays aleatorios, límite diario, horario restringido.
"""
import asyncio
import logging
import random
from datetime import datetime, date

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CampanaLead, EstadoEnvio, ContadorDiario
from app.llm_client import generar_mensaje_inicial
from app.evolution_client import (
    enviar_mensaje_texto,
    EvolutionAPIError,
    NumeroInvalidoError,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Estado global del worker (consultable desde el endpoint /status)
worker_estado = {
    "corriendo": False,
    "ultimo_envio": None,
    "enviados_hoy": 0,
    "pausado_por_admin": False,
}


async def _obtener_enviados_hoy(db: AsyncSession) -> int:
    """Consulta cuántos mensajes se enviaron en el día actual."""
    hoy = date.today().isoformat()
    result = await db.execute(
        select(ContadorDiario.total_enviados).where(ContadorDiario.fecha == hoy)
    )
    row = result.scalar_one_or_none()
    return row or 0


async def _incrementar_contador_diario(db: AsyncSession):
    """Incrementa el contador diario de mensajes enviados."""
    hoy = date.today().isoformat()

    # Intentar update primero
    result = await db.execute(
        update(ContadorDiario)
        .where(ContadorDiario.fecha == hoy)
        .values(total_enviados=ContadorDiario.total_enviados + 1)
        .returning(ContadorDiario.total_enviados)
    )
    if result.rowcount == 0:
        # Si no existe el registro de hoy, crearlo
        db.add(ContadorDiario(fecha=hoy, total_enviados=1))

    await db.commit()


async def _esta_en_horario_permitido() -> bool:
    """Verifica si estamos dentro del horario de envío configurado."""
    hora_actual = datetime.now().hour
    return settings.WORKER_HORA_INICIO <= hora_actual < settings.WORKER_HORA_FIN


async def _procesar_un_lead(lead: CampanaLead, db: AsyncSession):
    """
    Procesa un único lead: genera mensaje con IA y lo envía por WhatsApp.
    Actualiza el estado en BD según el resultado.
    """
    lead_id = lead.id
    nombre = lead.nombre_cliente
    telefono = lead.telefono

    try:
        # 1. Generar mensaje personalizado con IA
        logger.info(f"🤖 Generando mensaje para lead {lead_id} ({nombre})...")
        mensaje = await generar_mensaje_inicial(nombre, lead.modelo_plan, lead.cuotas_pagas or 0)

        # 2. Enviar por WhatsApp
        logger.info(f"📤 Enviando a {telefono}...")
        respuesta_api = await enviar_mensaje_texto(telefono, mensaje)

        # 3. Actualizar BD → ENVIADO
        whatsapp_id = respuesta_api.get("key", {}).get("id") if isinstance(respuesta_api, dict) else None
        await db.execute(
            update(CampanaLead)
            .where(CampanaLead.id == lead_id)
            .values(
                estado_envio=EstadoEnvio.ENVIADO,
                fecha_envio=datetime.now(),
                whatsapp_message_id=whatsapp_id,
                historial_chat=lead.historial_chat + [
                    {
                        "rol": "sistema",
                        "mensaje": mensaje,
                        "timestamp": datetime.now().isoformat(),
                    }
                ],
            )
        )
        await db.commit()
        await _incrementar_contador_diario(db)

        worker_estado["ultimo_envio"] = datetime.now().isoformat()
        logger.info(f"✅ Lead {lead_id} ({nombre}) → ENVIADO")

    except NumeroInvalidoError:
        # El número no existe en WhatsApp
        await db.execute(
            update(CampanaLead)
            .where(CampanaLead.id == lead_id)
            .values(estado_envio=EstadoEnvio.ERROR_NUMERO)
        )
        await db.commit()
        logger.warning(f"⚠️ Lead {lead_id} ({telefono}) → ERROR_NUMERO (no existe en WhatsApp)")

        # Notificar al CRM si está configurado
        if settings.CRM_WEBHOOK_URL:
            from app.crm_sync import disparar_handoff
            lead_actualizado = await db.get(CampanaLead, lead_id)
            if lead_actualizado:
                await disparar_handoff(lead_actualizado, motivo="NUMERO_INVALIDO")

    except EvolutionAPIError as e:
        # Error de API: volver a PENDIENTE para reintentar después
        await db.execute(
            update(CampanaLead)
            .where(CampanaLead.id == lead_id)
            .values(estado_envio=EstadoEnvio.PENDIENTE)
        )
        await db.commit()
        logger.error(f"❌ Lead {lead_id}: Error Evolution API → {e}. Reintentará en el próximo ciclo.")

    except Exception as e:
        # Error inesperado: volver a PENDIENTE
        await db.execute(
            update(CampanaLead)
            .where(CampanaLead.id == lead_id)
            .values(estado_envio=EstadoEnvio.PENDIENTE)
        )
        await db.commit()
        logger.error(f"❌ Error inesperado procesando lead {lead_id}: {e}")


async def worker_loop():
    """
    Bucle principal del worker. Corre indefinidamente en background.
    
    Ciclo:
    1. Verificar horario permitido
    2. Verificar límite diario
    3. Tomar batch de leads PENDIENTE → marcar PROCESANDO (bloqueo atómico)
    4. Por cada lead: generar mensaje con IA + enviar
    5. Dormir delay aleatorio
    6. Repetir
    """
    logger.info("🚀 Worker de envío iniciado.")
    worker_estado["corriendo"] = True

    while True:
        try:
            # Pausa manual por admin
            if worker_estado["pausado_por_admin"]:
                logger.info("⏸️ Worker pausado por administrador. Esperando 60s...")
                await asyncio.sleep(60)
                continue

            # Verificar horario
            if not await _esta_en_horario_permitido():
                hora = datetime.now().strftime("%H:%M")
                logger.info(
                    f"🌙 Fuera de horario de envío ({hora}). "
                    f"Activo entre {settings.WORKER_HORA_INICIO}:00 y {settings.WORKER_HORA_FIN}:00. "
                    f"Esperando 5 minutos..."
                )
                await asyncio.sleep(300)
                continue

            async with AsyncSessionLocal() as db:
                # Verificar límite diario
                enviados_hoy = await _obtener_enviados_hoy(db)
                worker_estado["enviados_hoy"] = enviados_hoy

                if enviados_hoy >= settings.WORKER_MAX_DIARIO:
                    logger.warning(
                        f"🛑 Límite diario alcanzado ({enviados_hoy}/{settings.WORKER_MAX_DIARIO}). "
                        f"El worker se reanudará mañana."
                    )
                    await asyncio.sleep(3600)  # Esperar 1 hora y re-evaluar
                    continue

                # Tomar batch de leads PENDIENTE y marcarlos como PROCESANDO (atómico)
                result = await db.execute(
                    select(CampanaLead)
                    .where(CampanaLead.estado_envio == EstadoEnvio.PENDIENTE)
                    .limit(settings.WORKER_BATCH_SIZE)
                    .with_for_update(skip_locked=True)  # Bloqueo para evitar procesamiento doble
                )
                leads = result.scalars().all()

                if not leads:
                    logger.info("💤 No hay leads PENDIENTES. Revisando en 2 minutos...")
                    await asyncio.sleep(120)
                    continue

                # Marcar como PROCESANDO
                ids = [l.id for l in leads]
                await db.execute(
                    update(CampanaLead)
                    .where(CampanaLead.id.in_(ids))
                    .values(estado_envio=EstadoEnvio.PROCESANDO)
                )
                await db.commit()

                logger.info(f"📋 Procesando batch de {len(leads)} leads: IDs {ids}")

            # Procesar cada lead con delay entre mensajes
            for lead in leads:
                async with AsyncSessionLocal() as db:
                    lead_fresco = await db.get(CampanaLead, lead.id)
                    if lead_fresco:
                        await _procesar_un_lead(lead_fresco, db)

                # Delay anti-ban entre mensajes (incluso dentro del batch)
                delay = random.randint(settings.WORKER_DELAY_MIN, settings.WORKER_DELAY_MAX)
                logger.info(f"⏳ Esperando {delay}s antes del próximo mensaje...")
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("🛑 Worker detenido correctamente.")
            worker_estado["corriendo"] = False
            break

        except Exception as e:
            logger.error(f"💥 Error crítico en el worker loop: {e}. Reintentando en 60s...")
            await asyncio.sleep(60)
