"""
Routers de la API REST.
Endpoints para ingesta, control del worker y reportes.
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_session
from app.models import CampanaLead, EstadoEnvio
from app.ingestion import procesar_archivo
from app.worker import worker_estado
from app.evolution_client import obtener_estado_instancia, obtener_qr_conexion

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────
# ROUTER: Ingesta
# ───────────────────────────────────────────────
ingesta_router = APIRouter(prefix="/api/ingesta", tags=["📥 Ingesta"])


@ingesta_router.post("/cargar", summary="Cargar Excel o CSV de leads")
async def cargar_archivo(
    archivo: UploadFile = File(..., description="Archivo .xlsx o .csv con los leads"),
    campana: Optional[str] = Form(None, description="Nombre de la campaña (opcional)"),
    db: AsyncSession = Depends(get_session),
):
    """
    Sube un archivo Excel o CSV, sanitiza los números y los guarda en la BD.
    Los leads cargados quedan en estado PENDIENTE listos para el worker.
    """
    nombre = archivo.filename or "archivo"

    if not (nombre.endswith(".xlsx") or nombre.endswith(".csv")):
        raise HTTPException(
            status_code=400,
            detail="Solo se aceptan archivos .xlsx o .csv"
        )

    contenido = await archivo.read()
    if len(contenido) == 0:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")

    try:
        estadisticas = await procesar_archivo(contenido, nombre, campana, db)
        return {
            "status": "ok",
            "mensaje": f"Archivo procesado correctamente.",
            "estadisticas": estadisticas,
        }
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ───────────────────────────────────────────────
# ROUTER: Control del Worker
# ───────────────────────────────────────────────
worker_router = APIRouter(prefix="/api/worker", tags=["⚙️ Worker"])


@worker_router.get("/estado", summary="Estado del worker y estadísticas de envío")
async def estado_worker(db: AsyncSession = Depends(get_session)):
    """Retorna el estado actual del worker y conteos de leads por estado."""

    # Contar leads por estado
    conteos_result = await db.execute(
        select(CampanaLead.estado_envio, func.count(CampanaLead.id))
        .group_by(CampanaLead.estado_envio)
    )
    conteos = {row[0]: row[1] for row in conteos_result.fetchall()}

    # Estado de la instancia de WhatsApp
    instancia = await obtener_estado_instancia()

    return {
        "worker": worker_estado,
        "instancia_whatsapp": instancia,
        "leads": {
            "pendientes": conteos.get(EstadoEnvio.PENDIENTE, 0),
            "procesando": conteos.get(EstadoEnvio.PROCESANDO, 0),
            "enviados": conteos.get(EstadoEnvio.ENVIADO, 0),
            "error_numero": conteos.get(EstadoEnvio.ERROR_NUMERO, 0),
            "contactados": conteos.get(EstadoEnvio.CONTACTADO, 0),
            "total": sum(conteos.values()),
        },
    }


@worker_router.post("/pausar", summary="Pausar el worker")
async def pausar_worker():
    """Pausa el envío de mensajes. Los leads en PROCESANDO terminarán su ciclo actual."""
    worker_estado["pausado_por_admin"] = True
    return {"status": "worker pausado"}


@worker_router.get("/conectar", summary="Obtener QR de conexión")
async def conectar_whatsapp():
    """Retorna el QR de conexión (base64) para escanear desde WhatsApp."""
    return await obtener_qr_conexion()


@worker_router.post("/reanudar", summary="Reanudar el worker")
async def reanudar_worker():
    """Reanuda el envío de mensajes."""
    worker_estado["pausado_por_admin"] = False
    return {"status": "worker reanudado"}


@worker_router.post("/resetear-procesando", summary="Resetear leads atascados en PROCESANDO")
async def resetear_procesando(db: AsyncSession = Depends(get_session)):
    """
    En caso de reinicio inesperado del servidor, los leads quedan en PROCESANDO.
    Este endpoint los devuelve a PENDIENTE para ser reintentados.
    """
    result = await db.execute(
        update(CampanaLead)
        .where(CampanaLead.estado_envio == EstadoEnvio.PROCESANDO)
        .values(estado_envio=EstadoEnvio.PENDIENTE)
        .returning(CampanaLead.id)
    )
    await db.commit()
    ids_reseteados = [row[0] for row in result.fetchall()]
    return {
        "status": "ok",
        "leads_reseteados": len(ids_reseteados),
        "ids": ids_reseteados,
    }


# ───────────────────────────────────────────────
# ROUTER: Reportes y Visualización
# ───────────────────────────────────────────────
reportes_router = APIRouter(prefix="/api/reportes", tags=["📊 Reportes"])


@reportes_router.get("/leads", summary="Listar leads con filtros")
async def listar_leads(
    estado: Optional[str] = Query(None, description="Filtrar por estado: PENDIENTE, ENVIADO, CONTACTADO..."),
    clasificacion: Optional[str] = Query(None, description="Filtrar por clasificación IA: INTERES, RECHAZO, DUDA"),
    campana: Optional[str] = Query(None, description="Filtrar por nombre de campaña"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
):
    query = select(CampanaLead)

    if estado:
        query = query.where(CampanaLead.estado_envio == estado.upper())
    if clasificacion:
        query = query.where(CampanaLead.clasificacion_ia == clasificacion.upper())
    if campana:
        query = query.where(CampanaLead.campana == campana)

    query = query.order_by(CampanaLead.fecha_ingesta.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    leads = result.scalars().all()

    return {
        "total": len(leads),
        "leads": [
            {
                "id": l.id,
                "nombre": l.nombre_cliente,
                "telefono": l.telefono,
                "modelo": l.modelo_plan,
                "cuotas": l.cuotas_pagas,
                "estado": l.estado_envio,
                "clasificacion": l.clasificacion_ia,
                "razon_ia": l.razon_ia,
                "campana": l.campana,
                "fecha_ingesta": l.fecha_ingesta.isoformat() if l.fecha_ingesta else None,
                "fecha_envio": l.fecha_envio.isoformat() if l.fecha_envio else None,
                "fecha_respuesta": l.fecha_respuesta.isoformat() if l.fecha_respuesta else None,
            }
            for l in leads
        ],
    }


@reportes_router.get("/resumen", summary="Resumen ejecutivo de la campaña")
async def resumen_campana(
    campana: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session),
):
    """Dashboard rápido con métricas clave."""
    query_base = select(CampanaLead)
    if campana:
        query_base = query_base.where(CampanaLead.campana == campana)

    # Totales por estado
    result = await db.execute(
        select(CampanaLead.estado_envio, func.count(CampanaLead.id))
        .where(CampanaLead.campana == campana if campana else True)
        .group_by(CampanaLead.estado_envio)
    )
    por_estado = {row[0]: row[1] for row in result.fetchall()}

    # Totales por clasificación IA
    result2 = await db.execute(
        select(CampanaLead.clasificacion_ia, func.count(CampanaLead.id))
        .where(CampanaLead.clasificacion_ia != None)
        .group_by(CampanaLead.clasificacion_ia)
    )
    por_clasificacion = {row[0]: row[1] for row in result2.fetchall()}

    total = sum(por_estado.values())
    enviados = por_estado.get("ENVIADO", 0) + por_estado.get("CONTACTADO", 0)
    interesados = por_clasificacion.get("INTERES", 0)

    return {
        "resumen": {
            "total_leads": total,
            "enviados": enviados,
            "contactados_crm": por_estado.get("CONTACTADO", 0),
            "tasa_envio": f"{(enviados/total*100):.1f}%" if total > 0 else "0%",
            "tasa_interes": f"{(interesados/enviados*100):.1f}%" if enviados > 0 else "0%",
        },
        "por_estado": por_estado,
        "por_clasificacion_ia": por_clasificacion,
    }
