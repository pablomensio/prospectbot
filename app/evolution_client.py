"""
Cliente de Evolution API.
Encapsula todas las llamadas HTTP a la API de WhatsApp.
"""
import logging
import httpx
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _headers() -> dict:
    return {
        "apikey": settings.EVOLUTION_API_KEY,
        "Content-Type": "application/json",
    }


async def enviar_mensaje_texto(
    telefono: str,
    mensaje: str,
    timeout: int = 30,
) -> dict:
    """
    Envía un mensaje de texto via Evolution API.
    
    Endpoint: POST /message/sendText/{instance}
    
    Retorna el dict de respuesta de la API o lanza excepción en caso de error.
    """
    url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"

    payload = {
        "number": telefono,
        "text": mensaje,
        "delay": 1500,  # Delay de tipeo simulado (ms) para parecer más humano
        "linkPreview": False,
        "mentionsEveryOne": False,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=_headers())

        if response.status_code == 201:
            data = response.json()
            logger.info(f"✅ Mensaje enviado a {telefono} | ID: {data.get('key', {}).get('id', 'N/A')}")
            return data

        elif response.status_code == 400:
            # Evolution devuelve 400 cuando el número no existe en WhatsApp
            logger.warning(f"⚠️ Número inválido en WhatsApp: {telefono} | {response.text}")
            raise NumeroInvalidoError(f"Número {telefono} no existe en WhatsApp")

        else:
            logger.error(f"❌ Error Evolution API {response.status_code}: {response.text}")
            raise EvolutionAPIError(f"Error {response.status_code}: {response.text}")

    except httpx.TimeoutException:
        raise EvolutionAPIError(f"Timeout al contactar Evolution API para {telefono}")

    except httpx.ConnectError:
        raise EvolutionAPIError(
            f"No se puede conectar a Evolution API en {settings.EVOLUTION_API_URL}. "
            "Verificá que el VPS y la instancia estén activos."
        )


async def verificar_numero_whatsapp(telefono: str) -> bool:
    """
    Verifica si un número tiene cuenta de WhatsApp activa.
    Endpoint: GET /chat/whatsappNumbers/{instance}
    Útil para pre-validar antes de intentar el envío.
    """
    url = f"{settings.EVOLUTION_API_URL}/chat/whatsappNumbers/{settings.EVOLUTION_INSTANCE}"
    payload = {"numbers": [telefono]}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload, headers=_headers())

        if response.status_code == 200:
            data = response.json()
            # Evolution retorna [{number, exists, jid}]
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("exists", False)

        return False

    except Exception as e:
        logger.warning(f"⚠️ No se pudo verificar número {telefono}: {e}")
        return True  # En caso de error de verificación, intentar el envío igual


async def obtener_estado_instancia() -> dict:
    """
    Verifica el estado de la instancia de WhatsApp (conectada, desconectada, etc.)
    Útil para el health check del sistema.
    """
    url = f"{settings.EVOLUTION_API_URL}/instance/connectionState/{settings.EVOLUTION_INSTANCE}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=_headers())

        if response.status_code == 200:
            return response.json()

        return {"state": "ERROR", "detail": response.text}

    except Exception as e:
        return {"state": "UNREACHABLE", "detail": str(e)}


async def obtener_qr_conexion() -> dict:
    """
    Obtiene el QR de conexión para la instancia de WhatsApp.
    Reintenta hasta 3 veces si el QR todavía no está listo.
    """
    import asyncio

    for intento in range(3):
        url = f"{settings.EVOLUTION_API_URL}/instance/connect/{settings.EVOLUTION_INSTANCE}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=_headers())

            if response.status_code == 200:
                data = response.json()
                # Si ya tiene el código QR
                if data.get("code"):
                    return data
                # Si count=0, esperar y reintentar
                if intento < 2:
                    await asyncio.sleep(2)
                    continue

            return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}

        except Exception as e:
            return {"error": str(e)}

    return {"error": "QR no disponible aún. Reintentá en unos segundos."}


# ---------------------------------------------------------------------------
# Excepciones custom para manejo elegante
# ---------------------------------------------------------------------------

class EvolutionAPIError(Exception):
    """Error genérico de Evolution API."""
    pass


class NumeroInvalidoError(EvolutionAPIError):
    """El número no existe en WhatsApp."""
    pass
