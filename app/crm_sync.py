"""
Handoff al CRM externo.
Cuando un lead es INTERESADO o supera el límite de dudas,
se dispara un webhook HTTP al CRM principal del usuario.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

from app.config import get_settings
from app.models import CampanaLead

logger = logging.getLogger(__name__)
settings = get_settings()


async def disparar_handoff(lead: CampanaLead, motivo: str = "INTERES") -> bool:
    """
    Envía los datos del lead al CRM externo vía webhook.
    
    El CRM recibe un payload JSON con toda la información necesaria
    para que un agente humano tome el control de la conversación.
    
    Retorna True si el handoff fue exitoso, False en caso contrario.
    """
    if not settings.CRM_WEBHOOK_URL:
        logger.warning("⚠️ CRM_WEBHOOK_URL no configurado. Handoff omitido.")
        return False

    payload = {
        "evento": "LEAD_CALIENTE",
        "motivo": motivo,
        "timestamp": datetime.now().isoformat(),
        "microservicio": "prospectador-wsp-v1",
        "lead": {
            "id_interno": lead.id,
            "campana": lead.campana,
            "nombre_cliente": lead.nombre_cliente,
            "telefono": lead.telefono,
            "modelo_plan": lead.modelo_plan,
            "cuotas_pagas": lead.cuotas_pagas,
            "clasificacion_ia": lead.clasificacion_ia,
            "razon_ia": lead.razon_ia,
            "contador_dudas": lead.contador_dudas,
            "historial_chat": lead.historial_chat or [],
            "fecha_ingesta": lead.fecha_ingesta.isoformat() if lead.fecha_ingesta else None,
            "fecha_respuesta": lead.fecha_respuesta.isoformat() if lead.fecha_respuesta else None,
        },
        "acciones_sugeridas": _generar_acciones_sugeridas(lead, motivo),
    }

    headers = {
        "Content-Type": "application/json",
        "X-Microservicio-Token": settings.CRM_WEBHOOK_SECRET,
        "X-Evento": "LEAD_CALIENTE",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings.CRM_WEBHOOK_URL, json=payload, headers=headers)

        if response.status_code in (200, 201, 202, 204):
            logger.info(
                f"✅ Handoff exitoso para lead {lead.id} ({lead.nombre_cliente}) "
                f"→ CRM respondió {response.status_code}"
            )
            return True

        else:
            logger.error(
                f"❌ Handoff fallido para lead {lead.id}. "
                f"CRM respondió {response.status_code}: {response.text[:200]}"
            )
            return False

    except httpx.TimeoutException:
        logger.error(f"❌ Timeout al hacer handoff del lead {lead.id} al CRM.")
        return False

    except httpx.ConnectError:
        logger.error(
            f"❌ No se pudo conectar al CRM en {settings.CRM_WEBHOOK_URL}. "
            f"Lead {lead.id} no fue transferido."
        )
        return False

    except Exception as e:
        logger.error(f"❌ Error inesperado en handoff del lead {lead.id}: {e}")
        return False


def _generar_acciones_sugeridas(lead: CampanaLead, motivo: str) -> list:
    """Genera sugerencias contextuales para el agente humano."""
    acciones = []

    if motivo == "INTERES":
        acciones.append(f"Llamar a {lead.nombre_cliente} dentro de las próximas 2 horas.")
        acciones.append(f"Preparar propuesta para plan {lead.modelo_plan}.")
        if lead.cuotas_pagas and lead.cuotas_pagas > 0:
            acciones.append(
                f"Tiene {lead.cuotas_pagas} cuotas pagas. Evaluar opciones de "
                f"canje, transferencia o reactivación del plan."
            )

    elif motivo == "LIMITE_DUDAS":
        acciones.append(
            f"{lead.nombre_cliente} no se comprometió después de {lead.contador_dudas} "
            f"interacciones. Considerar contacto telefónico directo."
        )
        acciones.append("Revisar el historial del chat adjunto antes de llamar.")

    return acciones
