"""
Motor de base de datos asíncrono (SQLite + aiosqlite + SQLAlchemy).
Principio del SKILL: tipado estricto, constraints e índices para performance.
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    """Dependency de FastAPI para obtener una sesión de BD."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """
    Crea todas las tablas al arrancar la aplicación.
    Equivalente a las migraciones iniciales del SKILL.
    """
    from app import models  # noqa: F401 — importar para registrar los modelos
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
