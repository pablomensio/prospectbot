"""
Webhook Receptor de Evolution API + Triaje de IA.
Recibe los mensajes entrantes y los clasifica con IA.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Header
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models import CampanaLead, EstadoEnvio, ClasificacionIA
from app.llm_client import clasificar_respuesta
from app.crm_sync import disparar_handoff

from fastapi import Depends

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api/webhook", tags=["Webhook"])


def _normalizar_telefono(jid: str) -> str:
    """
    Convierte el JID de WhatsApp (ej: '5493515123456@s.whatsapp.net')
    al formato de teléfono local ('5493515123456').
    """
    return jid.split("@")[0].strip()


@router.post("/evolution", summary="Receptor de eventos de Evolution API")
async def recibir_evento_evolution(
    request: Request,
    x_webhook_token: str = Header(None, alias="x-webhook-token"),
    db: AsyncSession = Depends(get_session),
):
    """
    Endpoint principal de webhook.
    Evolution API debe apuntar sus eventos a: POST /api/webhook/evolution
    
    Seguridad: Verifica el token de webhook si está configurado.
    """
    # Validación de token (si está configurado)
    if settings.WEBHOOK_SECRET_TOKEN and x_webhook_token != settings.WEBHOOK_SECRET_TOKEN:
        logger.warning("⚠️ Intento de webhook con token inválido.")
        raise HTTPException(status_code=401, detail="Token de webhook inválido.")

    # Leer el cuerpo del request
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo del request no es JSON válido.")

    logger.debug(f"📩 Evento recibido de Evolution: {body.get('event', 'desconocido')}")

    # Filtrar solo eventos de mensajes entrantes
    evento = body.get("event", "")
    if evento != "messages.upsert":
        return {"status": "ignorado", "razon": f"Evento '{evento}' no es de interés."}

    data = body.get("data", {})
    
    # Solo procesar mensajes de otros (no los propios mensajes enviados)
    if data.get("key", {}).get("fromMe", True):
        return {"status": "ignorado", "razon": "Mensaje propio, no del cliente."}

    # Extraer información del mensaje
    jid = data.get("key", {}).get("remoteJid", "")
    if not jid:
        return {"status": "ignorado", "razon": "Sin JID de remitente."}

    # Obtener texto del mensaje (puede venir en distintos formatos)
    mensaje_content = data.get("message", {})
    texto_cliente = (
        mensaje_content.get("conversation")
        or mensaje_content.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()

    if not texto_cliente:
        return {"status": "ignorado", "razon": "Mensaje sin texto (puede ser imagen, sticker, etc.)"}

    telefono = _normalizar_telefono(jid)
    logger.info(f"📨 Mensaje de {telefono}: '{texto_cliente[:80]}...'")

    # Buscar el lead en nuestra BD
    result = await db.execute(
        select(CampanaLead).where(CampanaLead.telefono == telefono)
    )
    lead = result.scalar_one_or_none()

    if not lead:
        logger.info(f"ℹ️ Teléfono {telefono} no pertenece a ningún lead de campañas. Ignorado.")
        return {"status": "ignorado", "razon": "Número no encontrado en ninguna campaña."}

    if lead.estado_envio not in (EstadoEnvio.ENVIADO, EstadoEnvio.CONTACTADO):
        logger.info(f"ℹ️ Lead {lead.id} en estado '{lead.estado_envio}'. No se procesa respuesta.")
        return {"status": "ignorado", "razon": f"Lead en estado {lead.estado_envio}."}

    # Actualizar historial del chat
    historial_actualizado = (lead.historial_chat or []) + [
        {
            "rol": "cliente",
            "mensaje": texto_cliente,
            "timestamp": datetime.now().isoformat(),
        }
    ]

    # Clasificar con IA
    contexto = f"Modelo del plan: {lead.modelo_plan}. Cuotas pagas: {lead.cuotas_pagas}."
    clasificacion = await clasificar_respuesta(texto_cliente, contexto)
    intencion = clasificacion["intencion"]
    razon = clasificacion["razon"]

    logger.info(f"🧠 Lead {lead.id} clasificado como: {intencion} | Razón: {razon}")

    # Determinar nuevo contador de dudas
    nuevo_contador_dudas = lead.contador_dudas
    if intencion == "DUDA":
        nuevo_contador_dudas += 1

    # Determinar si hay que hacer handoff
    debe_handoff = (
        intencion == ClasificacionIA.INTERES
        or nuevo_contador_dudas >= settings.MAX_DUDAS_ANTES_HANDOFF
    )

    nuevo_estado = EstadoEnvio.CONTACTADO if debe_handoff else EstadoEnvio.ENVIADO

    # Guardar clasificación e historial en BD
    await db.execute(
        update(CampanaLead)
        .where(CampanaLead.id == lead.id)
        .values(
            clasificacion_ia=intencion,
            razon_ia=razon,
            historial_chat=historial_actualizado,
            fecha_respuesta=datetime.now(),
            estado_envio=nuevo_estado,
            contador_dudas=nuevo_contador_dudas,
        )
    )
    await db.commit()

    # Ejecutar handoff si corresponde
    if debe_handoff:
        motivo = "INTERES" if intencion == ClasificacionIA.INTERES else "LIMITE_DUDAS"
        
        # Refrescar el objeto para tener los datos actualizados
        lead_actualizado = await db.get(CampanaLead, lead.id)
        handoff_ok = await disparar_handoff(lead_actualizado, motivo=motivo)

        logger.info(
            f"🔥 Handoff para lead {lead.id} ({lead.nombre_cliente}): "
            f"{'✅ exitoso' if handoff_ok else '❌ fallido'}"
        )

        return {
            "status": "procesado",
            "accion": "HANDOFF",
            "lead_id": lead.id,
            "clasificacion": intencion,
            "handoff_exitoso": handoff_ok,
        }

    return {
        "status": "procesado",
        "accion": "CLASIFICADO",
        "lead_id": lead.id,
        "clasificacion": intencion,
    }
