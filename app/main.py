"""
Punto de entrada principal de FastAPI.
Inicializa la BD, arranca el worker en background y registra todos los routers.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import init_db
from app.routers import ingesta_router, worker_router, reportes_router
from app.webhook import router as webhook_router
from app import worker as worker_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
settings = get_settings()

# Referencia global a la tarea del worker para poder cancelarla al apagar
_worker_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la aplicación:
    - Al arrancar: inicializa BD + lanza worker en background
    - Al apagar: cancela el worker limpiamente
    """
    global _worker_task

    logger.info(f"🚀 Iniciando {settings.APP_NAME} v{settings.APP_VERSION}")

    # 1. Inicializar base de datos (crear tablas si no existen)
    await init_db()
    logger.info("✅ Base de datos inicializada.")

    # 2. Resetear cualquier lead que quedó en PROCESANDO de un reinicio anterior
    from app.database import AsyncSessionLocal
    from app.models import CampanaLead, EstadoEnvio
    from sqlalchemy import update
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(CampanaLead)
            .where(CampanaLead.estado_envio == EstadoEnvio.PROCESANDO)
            .values(estado_envio=EstadoEnvio.PENDIENTE)
        )
        await db.commit()
    logger.info("✅ Leads atascados en PROCESANDO reseteados a PENDIENTE.")

    # 3. Lanzar el worker en background
    _worker_task = asyncio.create_task(worker_module.worker_loop())
    logger.info("✅ Worker de envío lanzado en background.")

    yield

    # Al apagar: cancelar el worker
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    logger.info("👋 Microservicio apagado correctamente.")


# ───────────────────────────────────────────────
# Crear la aplicación FastAPI
# ───────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## 🚀 Microservicio de Prospección Automatizada vía WhatsApp

**Funcionalidades:**
- 📥 **Ingesta**: Carga Excel/CSV, sanitiza números argentinos, detecta columnas automáticamente
- 📤 **Worker**: Envío masivo con delays anti-ban, horario restringido, límite diario
- 🤖 **IA Generadora**: Mensaje inicial personalizado por lead (nombre, modelo, cuotas)
- 🧠 **IA Clasificadora**: Triaje automático de respuestas (INTERÉS / RECHAZO / DUDA)
- 🔥 **Handoff**: Notificación automática al CRM externo cuando el lead está caliente

**Integración:** Evolution API (WhatsApp) + OpenAI/Gemini/Ollama
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS (permite llamadas desde el dashboard o Postman)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar todos los routers
app.include_router(ingesta_router)
app.include_router(worker_router)
app.include_router(reportes_router)
app.include_router(webhook_router)

# Montar archivos estáticos (dashboard)
_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    """Sirve el dashboard principal."""
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/health", tags=["🏠 Root"])
async def health_check():
    """Health check para el VPS / systemd / Docker."""
    from app.evolution_client import obtener_estado_instancia
    instancia = await obtener_estado_instancia()
    return {
        "status": "ok",
        "worker_corriendo": worker_module.worker_estado["corriendo"],
        "instancia_whatsapp": instancia.get("state", "desconocido"),
    }
