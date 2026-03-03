"""
Modelos de base de datos (ORM).
Principio del SKILL: tipado estricto, índices en columnas de búsqueda frecuente,
constraints CHECK para reglas de negocio.
"""
import enum
from datetime import datetime
from sqlalchemy import (
    Integer, String, Text, DateTime, CheckConstraint, Index,
    func, JSON
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class EstadoEnvio(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    PROCESANDO = "PROCESANDO"
    ENVIADO = "ENVIADO"
    ERROR_NUMERO = "ERROR_NUMERO"
    CONTACTADO = "CONTACTADO"       # Handoff al CRM completado


class ClasificacionIA(str, enum.Enum):
    INTERES = "INTERES"
    RECHAZO = "RECHAZO"
    DUDA = "DUDA"


class CampanaLead(Base):
    """
    Tabla principal. Un lead = una fila.
    UNIQUE en `telefono` garantiza idempotencia (no se duplican leads).
    """
    __tablename__ = "campana_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Datos del lead
    nombre_cliente: Mapped[str] = mapped_column(String(200), nullable=False)
    telefono: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    modelo_plan: Mapped[str] = mapped_column(String(200), nullable=True)
    cuotas_pagas: Mapped[int] = mapped_column(Integer, nullable=True)

    # Estado del flujo
    estado_envio: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoEnvio.PENDIENTE,
        index=True,  # Índice: el worker filtra por este campo constantemente
    )

    # Triaje IA
    clasificacion_ia: Mapped[str] = mapped_column(String(20), nullable=True, index=True)
    razon_ia: Mapped[str] = mapped_column(Text, nullable=True)
    contador_dudas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Historial de conversación (JSON array de mensajes)
    historial_chat: Mapped[dict] = mapped_column(JSON, nullable=True, default=list)

    # Auditoría
    fecha_ingesta: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    fecha_envio: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_respuesta: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_handoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # ID externo de mensaje en WhatsApp (para trazabilidad)
    whatsapp_message_id: Mapped[str] = mapped_column(String(100), nullable=True)

    # Origen del archivo cargado (para agrupar campañas)
    campana: Mapped[str] = mapped_column(String(100), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint("cuotas_pagas >= 0", name="check_cuotas_positivas"),
        Index("ix_telefono_estado", "telefono", "estado_envio"),
    )

    def __repr__(self) -> str:
        return f"<Lead id={self.id} tel={self.telefono} estado={self.estado_envio}>"


class ContadorDiario(Base):
    """
    Lleva la cuenta de mensajes enviados HOY para respetar el límite anti-ban.
    """
    __tablename__ = "contador_diario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fecha: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)  # "2025-03-03"
    total_enviados: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<ContadorDiario fecha={self.fecha} enviados={self.total_enviados}>"
