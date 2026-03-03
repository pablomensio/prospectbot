"""
Módulo de Ingesta y Sanitización.
Fase 1: Lee Excel/CSV, limpia números argentinos, inserta en BD.

Soporta dos tipos de archivo:
  A) Con cabeceras (headers): detección automática de columnas por nombre.
  B) Sin cabeceras (raw): mapeo por posición de columna.
     Esquema confirmado para base Renault/Dister:
       Col  7 = Apellido
       Col  8 = Nombre
       Col 20 = Teléfono principal
       Col 22 = Teléfono alternativo 2
       Col 24 = Teléfono alternativo 3
       Col 30 = Email
       Col 84 = Patente
       Col 85 = Año del vehículo
       Col 90 = Marca
       Col 91 = Modelo
"""
import re
import io
import uuid
from typing import Optional

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models import CampanaLead, EstadoEnvio


# ---------------------------------------------------------------------------
# SANITIZACIÓN DE NÚMEROS ARGENTINOS (corregida vs versión original)
# ---------------------------------------------------------------------------

# Prefijos de área argentina más comunes (evitar falsos positivos con "15")
_CODIGO_PAIS = "549"


def sanitizar_numero_argentino(telefono_crudo) -> Optional[str]:
    """
    Estandariza números de teléfono argentinos al formato internacional
    requerido por WhatsApp: 549 + código de área + 8 dígitos.

    Retorna el número limpio o None si es irrecuperable.

    Mejoras vs código original:
    - NO usa replace('15','') que rompe números con 15 en el medio.
    - Detecta correctamente números con código de área de 2, 3 o 4 dígitos.
    - Valida la longitud final DESPUÉS de aplicar el prefijo.
    """
    if pd.isna(telefono_crudo) or not str(telefono_crudo).strip():
        return None

    # Eliminar todo lo que no sea dígito
    numero = re.sub(r'\D', '', str(telefono_crudo))

    if not numero:
        return None

    # --- Caso 1: Ya tiene el prefijo completo 549 ---
    if numero.startswith('549'):
        # Puede venir con o sin el "9" del celular
        # 549 + cod_area(2-4) + número(6-8) = 12 a 15 dígitos
        if 12 <= len(numero) <= 15:
            return numero
        return None

    # --- Caso 2: Tiene código de país 54 pero le falta el 9 ---
    if numero.startswith('54') and not numero.startswith('549'):
        numero = '549' + numero[2:]
        if 12 <= len(numero) <= 15:
            return numero
        return None

    # --- Caso 3: Empieza con 0 (número local con discado) ---
    # Ej: 0351 1512 3456 → quitar 0 → 351 1512 3456
    if numero.startswith('0'):
        numero = numero[1:]

    # --- Caso 4: Empieza con 15 (legado sin código de área) ---
    # No soportamos este caso porque no podemos adivinar el código de área
    if numero.startswith('15') and len(numero) <= 10:
        return None  # Irrecuperable sin código de área

    # --- Caso 5: Número local puro (10 dígitos = cod_area + número) ---
    if len(numero) == 10:
        return _CODIGO_PAIS + numero

    # --- Caso 6: Número de 11 dígitos (con el "9" del celular ya incluido) ---
    # Ej: 9 351 512 3456
    if len(numero) == 11 and numero.startswith('9'):
        return '54' + numero

    return None


# ---------------------------------------------------------------------------
# LÓGICA DE INGESTA
# ---------------------------------------------------------------------------

# Sinónimos de columna que el sistema reconoce automáticamente (modo CON cabeceras)
_COLUMN_MAP = {
    "nombre_cliente": ["nombre", "name", "cliente", "apellido y nombre", "razon social"],
    "telefono": ["telefono", "teléfono", "tel", "phone", "celular", "movil", "móvil", "whatsapp"],
    "modelo_plan": ["modelo", "plan", "vehiculo", "vehículo", "auto", "producto"],
    "cuotas_pagas": ["cuotas", "cuotas pagas", "cuotas_pagas", "cant cuotas", "cuotas abonadas"],
}

# Mapeo por posición para archivos SIN cabeceras (base Renault/Dister confirmada)
_POSICION_SIN_HEADER = {
    "apellido": 7,
    "nombre": 8,
    "telefono_1": 20,
    "telefono_2": 22,
    "telefono_3": 24,
    "email": 30,
    "patente": 84,
    "anio": 85,
    "marca": 90,
    "modelo": 91,
}


def _tiene_cabeceras(df: pd.DataFrame) -> bool:
    """
    Detecta si la primera fila del DataFrame son cabeceras legibles
    o datos directos. Heurística: si la primera celda parece un número
    de ID largo (>8 dígitos), no hay cabeceras.
    """
    primera_celda = str(df.columns[0]).strip()
    # Si la primera columna tiene un número largo (ej: CUIT 20161763625), no hay headers
    if re.match(r'^\d{8,}$', primera_celda):
        return False
    # Si todas las columnas son enteros (0, 1, 2...), tampoco
    if all(isinstance(c, int) for c in df.columns):
        return False
    return True


def _detectar_columnas(df_columns: list) -> dict:
    """
    Mapea automáticamente las columnas del Excel a los campos del sistema.
    Retorna un dict: {campo_interno: nombre_columna_excel}
    """
    columnas_lower = {col.lower().strip(): col for col in df_columns}
    mapeo = {}

    for campo, sinonimos in _COLUMN_MAP.items():
        for sinonimo in sinonimos:
            if sinonimo in columnas_lower:
                mapeo[campo] = columnas_lower[sinonimo]
                break

    return mapeo


def _extraer_fila_sin_header(fila: pd.Series) -> dict:
    """
    Extrae los campos relevantes de una fila SIN cabeceras
    usando el mapeo por posición confirmado.
    Intenta los 3 teléfonos disponibles en orden de prioridad.
    """
    p = _POSICION_SIN_HEADER
    n_cols = len(fila)

    def safe_get(idx):
        return str(fila.iloc[idx]).strip() if idx < n_cols else ""

    apellido = safe_get(p["apellido"]).title()
    nombre = safe_get(p["nombre"]).title()
    nombre_completo = f"{nombre} {apellido}".strip()

    # Teléfonos con fallback
    telefono = (
        sanitizar_numero_argentino(safe_get(p["telefono_1"]))
        or sanitizar_numero_argentino(safe_get(p["telefono_2"]))
        or sanitizar_numero_argentino(safe_get(p["telefono_3"]))
    )

    marca = safe_get(p["marca"])
    modelo_raw = safe_get(p["modelo"])
    anio = safe_get(p["anio"])

    # Construir descripción del vehículo limpia
    modelo = f"{marca} {modelo_raw} ({anio})".strip() if marca and marca != "nan" else modelo_raw

    return {
        "nombre_cliente": nombre_completo,
        "telefono_limpio": telefono,
        "modelo_plan": modelo,
        "cuotas_pagas": 0,  # Esta base no tiene cuotas; la IA se adapta
        "telefono_original": safe_get(p["telefono_1"]),
    }


async def procesar_archivo(
    contenido: bytes,
    nombre_archivo: str,
    nombre_campana: Optional[str],
    db: AsyncSession,
) -> dict:
    """
    Lee el archivo, sanitiza, y carga los leads en la BD local.
    Garantiza idempotencia: si el teléfono ya existe, se omite (no duplica).
    """
    # 1. Leer el archivo — intentar primero CON cabeceras
    try:
        if nombre_archivo.endswith(".xlsx"):
            df_con_header = pd.read_excel(io.BytesIO(contenido), dtype=str)
            df_sin_header = pd.read_excel(io.BytesIO(contenido), header=None, dtype=str)
        else:
            df_con_header = pd.read_csv(
                io.BytesIO(contenido), encoding="utf-8", sep=None, engine="python", dtype=str
            )
            df_sin_header = pd.read_csv(
                io.BytesIO(contenido), header=None, encoding="utf-8", sep=None,
                engine="python", dtype=str
            )
    except Exception as e:
        raise ValueError(f"No se pudo leer el archivo: {e}")

    if df_con_header.empty:
        raise ValueError("El archivo está vacío o no tiene datos.")

    # 2. Detectar si tiene cabeceras
    tiene_headers = _tiene_cabeceras(df_con_header)
    modo_sin_header = not tiene_headers

    if modo_sin_header:
        df = df_sin_header
        mapeo = {}  # No necesitamos mapeo, usamos posiciones
        col_tel = None  # Indicador de modo sin header
    else:
        df = df_con_header
        mapeo = _detectar_columnas(list(df.columns))
        col_tel = mapeo.get("telefono")

        if not col_tel:
            columnas_disponibles = list(df.columns)
            raise ValueError(
                f"No se encontró una columna de teléfono. "
                f"Columnas disponibles: {columnas_disponibles}. "
                f"Renombra la columna a 'Telefono' o 'Celular', o sube el archivo sin cabeceras."
            )

    # 3. Nombre de campaña (para agrupar)
    campana = nombre_campana or f"campana_{uuid.uuid4().hex[:8]}"

    estadisticas = {
        "campana": campana,
        "total_filas": len(df),
        "validos": 0,
        "invalidos": 0,
        "duplicados": 0,
        "errores_detalle": [],
    }

    # 4. Obtener teléfonos ya existentes en BD (para chequeo de duplicados rápido)
    existing = await db.execute(select(CampanaLead.telefono))
    telefonos_existentes = {row[0] for row in existing.fetchall()}

    # 5. Procesar fila por fila
    leads_a_insertar = []

    for index, fila in df.iterrows():

        if modo_sin_header:
            # --- MODO SIN CABECERAS: extracción por posición ---
            datos = _extraer_fila_sin_header(fila)
            nombre = datos["nombre_cliente"]
            telefono_limpio = datos["telefono_limpio"]
            modelo = datos["modelo_plan"]
            cuotas = datos["cuotas_pagas"]
            telefono_raw = datos["telefono_original"]
        else:
            # --- MODO CON CABECERAS: extracción por nombre de columna ---
            telefono_raw = fila.get(col_tel, "")
            nombre = str(fila.get(mapeo.get("nombre_cliente", ""), f"Cliente_{index}")).strip()
            modelo = str(fila.get(mapeo.get("modelo_plan", ""), "Vehículo")).strip()
            cuotas_raw = fila.get(mapeo.get("cuotas_pagas", ""), 0)
            try:
                cuotas = int(float(str(cuotas_raw))) if cuotas_raw and not pd.isna(cuotas_raw) else 0
            except (ValueError, TypeError):
                cuotas = 0
            telefono_limpio = sanitizar_numero_argentino(telefono_raw)

        if not telefono_limpio:
            estadisticas["invalidos"] += 1
            estadisticas["errores_detalle"].append({
                "fila": int(index) + 2,
                "nombre": nombre,
                "telefono_original": str(telefono_raw),
                "razon": "Número irrecuperable (sin teléfono válido en ninguna de las 3 columnas)",
            })
            continue

        if telefono_limpio in telefonos_existentes:
            estadisticas["duplicados"] += 1
            continue

        telefonos_existentes.add(telefono_limpio)
        leads_a_insertar.append(
            CampanaLead(
                nombre_cliente=nombre,
                telefono=telefono_limpio,
                modelo_plan=modelo,
                cuotas_pagas=cuotas,
                estado_envio=EstadoEnvio.PENDIENTE,
                historial_chat=[],
                campana=campana,
            )
        )

    # 6. Inserción masiva
    if leads_a_insertar:
        db.add_all(leads_a_insertar)
        await db.commit()
        estadisticas["validos"] = len(leads_a_insertar)

    estadisticas["modo_lectura"] = "sin_cabeceras" if modo_sin_header else "con_cabeceras"
    return estadisticas
