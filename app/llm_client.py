"""
Cliente LLM usando AWS Bedrock (Claude 3.5 Sonnet).
Modo de invocación: Bedrock Runtime → converse API (formato Messages estándar).
Compatible con la misma interfaz que el resto del sistema.
"""
import json
import logging
import asyncio
from functools import partial
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _get_bedrock_client():
    """
    Crea el cliente de Bedrock Runtime con las credenciales del .env.
    Usa bedrock-runtime para la API de inferencia.
    """
    return boto3.client(
        service_name="bedrock-runtime",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def _invocar_claude_sync(system_prompt: str, user_message: str, max_tokens: int = 300, temperatura: float = 0.8) -> str:
    """
    Invocación SINCRÓNICA a Claude vía Bedrock converse API.
    Se ejecuta en un thread pool para no bloquear el event loop de asyncio.
    """
    client = _get_bedrock_client()

    response = client.converse(
        modelId=settings.BEDROCK_MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[
            {"role": "user", "content": [{"text": user_message}]}
        ],
        inferenceConfig={
            "maxTokens": max_tokens,
            "temperature": temperatura,
        },
    )

    return response["output"]["message"]["content"][0]["text"].strip()


async def _invocar_claude(system_prompt: str, user_message: str, max_tokens: int = 300, temperatura: float = 0.8) -> str:
    """
    Wrapper asíncrono: corre la llamada sincrónica a Bedrock en un thread pool
    para no bloquear el event loop de FastAPI/worker.
    """
    loop = asyncio.get_event_loop()
    fn = partial(_invocar_claude_sync, system_prompt, user_message, max_tokens, temperatura)
    return await loop.run_in_executor(None, fn)


# ---------------------------------------------------------------------------
# GENERACIÓN DE MENSAJE INICIAL PERSONALIZADO
# ---------------------------------------------------------------------------

_PROMPT_GENERADOR = """Eres un experto en ventas de automotores argentinos de Renault.
Tu tarea es redactar UN ÚNICO mensaje de WhatsApp para contactar a un cliente
existente que tiene o tuvo un vehículo Renault.

REGLAS ESTRICTAS:
- Máximo 3 líneas de texto.
- Tono cercano, humano, NO robótico. Podés usar 1 o 2 emojis máximo.
- Usá solo el PRIMER NOMBRE del cliente, nunca el apellido.
- Mencioná el modelo del vehículo para que sepa que es un contacto personalizado.
- Si tiene cuotas pagas (cuotas > 0), mencionalo como ventaja.
  Si cuotas = 0, hablá del vehículo que tiene/tuvo directamente.
- NO uses asteriscos para negritas, NO uses listas con guiones.
- NO prometas precios ni condiciones específicas.
- El objetivo es generar curiosidad y que el cliente responda SI o haga una pregunta.
- Terminá siempre con una pregunta abierta corta.

Ejemplos de tono correcto:
"Hola Daniel! 👋 Te contactamos desde Renault porque tenés un Duster Oroch registrado
y tenemos algo que puede interesarte mucho. ¿Tenés un minuto para contarte?"

"Hola Jorge! Vi que tenés un Renault Kangoo del 2019. Justo esta semana tenemos
una propuesta especial para clientes como vos. ¿Te puedo comentar?"

Responde SOLO con el texto del mensaje, sin comillas ni explicaciones adicionales."""


async def generar_mensaje_inicial(
    nombre: str,
    modelo: str,
    cuotas: int,
) -> str:
    """
    Genera un mensaje de WhatsApp personalizado y único usando Claude via Bedrock.
    """
    user_message = (
        f"Datos del cliente:\n"
        f"- Nombre completo: {nombre}\n"
        f"- Modelo del vehículo: {modelo}\n"
        f"- Cuotas pagas: {cuotas}\n\n"
        f"Redacta el mensaje de WhatsApp ahora."
    )

    try:
        mensaje = await _invocar_claude(
            system_prompt=_PROMPT_GENERADOR,
            user_message=user_message,
            max_tokens=200,
            temperatura=0.85,  # Alta variabilidad para mensajes únicos
        )
        logger.info(f"✅ Mensaje generado para {nombre}: {mensaje[:60]}...")
        return mensaje

    except NoCredentialsError:
        logger.error("❌ Credenciales AWS no válidas o no configuradas.")
        return _fallback_mensaje(nombre, modelo, cuotas)

    except ClientError as e:
        codigo = e.response["Error"]["Code"]
        logger.error(f"❌ AWS Bedrock ClientError [{codigo}]: {e}")
        return _fallback_mensaje(nombre, modelo, cuotas)

    except Exception as e:
        logger.error(f"❌ Error inesperado en generación de mensaje: {e}")
        return _fallback_mensaje(nombre, modelo, cuotas)


def _fallback_mensaje(nombre: str, modelo: str, cuotas: int) -> str:
    """Mensaje de fallback si Bedrock no está disponible."""
    primer_nombre = nombre.split()[0] if nombre else "cliente"
    cuotas_texto = f"con {cuotas} cuotas ya pagadas " if cuotas > 0 else ""
    return (
        f"Hola {primer_nombre}! 👋 Te contactamos desde Renault porque tenés "
        f"un {modelo} {cuotas_texto}y tenemos algo que puede interesarte. "
        f"¿Tenés un minuto?"
    )


# ---------------------------------------------------------------------------
# TRIAJE DE RESPUESTA DEL CLIENTE
# ---------------------------------------------------------------------------

_PROMPT_TRIAJE = """Eres un analista de intenciones de compra especializado en
respuestas de WhatsApp de potenciales clientes de automotores en Argentina.

Tu única tarea es clasificar el mensaje del cliente y devolver un JSON ESTRICTO.

Criterios de clasificación:
- INTERES: El cliente muestra curiosidad, hace preguntas sobre precio/condiciones,
  pide más info, o expresa interés explícito ("me interesa", "¿cuánto sale?", "contame").
- RECHAZO: El cliente rechaza explícitamente, pide no ser contactado, dice que no le
  interesa, o que ya resolvió su situación.
- DUDA: Cualquier otra respuesta ambigua, preguntas genéricas, mensajes cortos sin
  intención clara, o cuando el cliente pide tiempo ("después te digo", "estoy ocupado").

FORMATO DE RESPUESTA (JSON obligatorio, sin texto adicional ni bloques de código):
{"intencion": "INTERES", "razon": "Explicación breve"}

Los únicos valores válidos para "intencion" son: INTERES, RECHAZO, DUDA"""


async def clasificar_respuesta(
    mensaje_cliente: str,
    contexto_lead: Optional[str] = None,
) -> dict:
    """
    Clasifica la respuesta del cliente con Claude via Bedrock.
    Retorna: {"intencion": "INTERES"|"RECHAZO"|"DUDA", "razon": "..."}
    """
    contenido_usuario = f'Mensaje del cliente: "{mensaje_cliente}"'
    if contexto_lead:
        contenido_usuario = f"Contexto: {contexto_lead}\n\n{contenido_usuario}"

    try:
        raw = await _invocar_claude(
            system_prompt=_PROMPT_TRIAJE,
            user_message=contenido_usuario,
            max_tokens=120,
            temperatura=0.1,  # Casi determinístico para clasificación consistente
        )

        # Limpiar posibles bloques de código que el modelo pueda incluir
        raw_limpio = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        resultado = json.loads(raw_limpio)

        if "intencion" not in resultado:
            raise ValueError("El JSON no contiene 'intencion'")

        intencion = resultado["intencion"].upper()
        if intencion not in ("INTERES", "RECHAZO", "DUDA"):
            intencion = "DUDA"

        return {
            "intencion": intencion,
            "razon": resultado.get("razon", "Clasificación automática"),
        }

    except json.JSONDecodeError as e:
        logger.error(f"❌ Claude no devolvió JSON válido: {e}. Raw: {raw!r}")
        return {"intencion": "DUDA", "razon": "Error de parsing en respuesta de IA"}

    except NoCredentialsError:
        logger.error("❌ Credenciales AWS no válidas.")
        return {"intencion": "DUDA", "razon": "Error de credenciales AWS"}

    except ClientError as e:
        codigo = e.response["Error"]["Code"]
        logger.error(f"❌ AWS Bedrock ClientError en triaje [{codigo}]: {e}")
        return {"intencion": "DUDA", "razon": f"Error AWS: {codigo}"}

    except Exception as e:
        logger.error(f"❌ Error inesperado en triaje: {e}")
        return {"intencion": "DUDA", "razon": "Error de conexión con IA"}
