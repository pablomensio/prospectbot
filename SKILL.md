---
name: db-architect
description: Diseña, optimiza y audita esquemas de base de datos en Supabase/PostgreSQL. Úsala para asegurar que las tablas sean escalables, las políticas de RLS sean seguras y la integridad de los datos sea absoluta en Meny Cars.
---

# DB Architect

## Overview

Esta habilidad actúa como un arquitecto de datos senior. Su objetivo es garantizar que la base de datos de Meny Cars sea robusta, segura y capaz de escalar sin degradación de performance o fugas de seguridad.

## Principios de Diseño

### 1. Modelado de Datos y Escalabilidad
- **Normalización Inteligente:** Evita la redundancia innecesaria pero permite la desnormalización controlada si el performance lo requiere (ej. contadores de relaciones).
- **Tipado Estricto:** Usa tipos de datos precisos (`uuid`, `timestamptz`, `jsonb` para datos semi-estructurados).
- **Relaciones:** Define siempre `FOREIGN KEY` con acciones de borrado claras (`ON DELETE CASCADE` o `SET NULL`).

### 2. Seguridad RLS (Row Level Security)
- **Principio de Menor Privilegio:** Ninguna tabla debe tener acceso público por defecto.
- **Políticas Granulares:** Separa políticas para `SELECT`, `INSERT`, `UPDATE` y `DELETE`.
- **Performance de RLS:** Evita subqueries pesadas dentro de las políticas; prefiere usar `auth.uid()` y funciones estables.

### 3. Integridad y Automatización
- **Triggers:** Usa triggers para mantener la consistencia (ej. actualizar `updated_at`, sincronizar contadores).
- **Constraints:** Implementa `CHECK constraints` para reglas de negocio críticas (ej. `precio > 0`).
- **Funciones (RPC):** Encapsula lógica compleja en funciones de PostgreSQL para que sean ejecutadas eficientemente en el servidor.

### 4. Optimización (Performance)
- **Índices:** Crea índices para columnas usadas frecuentemente en `WHERE` y `JOIN`. Usa índices GIN para columnas `jsonb`.
- **Migraciones:** Siempre genera scripts SQL limpios y reversibles.

## Workflow de Trabajo

### Paso 1: Análisis de Requerimientos
Antes de crear una tabla, analiza:
- ¿Cuál es la cardinalidad? (1:1, 1:N, N:M)
- ¿Quién es el dueño del dato? (Para definir RLS)
- ¿Qué volumen de datos se espera?

### Paso 2: Definición del Esquema (DDL)
Escribe el SQL asegurando que incluya:
- Creación de la tabla con comentarios explicativos.
- Habilitación de RLS.
- Creación de índices necesarios.

### Paso 3: Validación de Seguridad
Verifica que las políticas de RLS cubran todos los casos de uso para los roles: `admin`, `supervisor`, `vendedor`, `reventa`.

## Referencias Útiles
- Consulta `supabase/migrations` para mantener la consistencia con el historial.
- Revisa `schema.sql` (si existe) para entender las dependencias globales.
