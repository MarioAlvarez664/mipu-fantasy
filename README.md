# ⚽ Fantasy Local — Aplicación Fantasy de Fútbol

## 1. Descripción del proyecto

Fantasy Local es una aplicación desarrollada en Python mediante Streamlit que permite gestionar una competición Fantasy de fútbol entre diferentes usuarios.

La aplicación permite:
- Registrar usuarios y gestionar equipos.
- Comprar y vender jugadores mediante una moneda virtual (rupias).
- Gestionar plantillas y alineaciones.
- Registrar estadísticas individuales de los futbolistas.
- Votar las actuaciones de los jugadores.
- Calcular y repartir puntos automáticamente.
- Gestionar la clasificación general.
- Administrar jornadas, jugadores, economía y mercado.

## 2. Estructura del proyecto

Los archivos principales son:

- `fantasy_local.py`: código principal de la aplicación.
- `requirements.txt`: dependencias necesarias para ejecutar el proyecto.
- `fantasy.db`: base de datos SQLite, creada automáticamente si no existe.
- `README.md`: documentación e instrucciones de instalación.

## 3. Requisitos previos

Es necesario disponer de:

- Python 3.10 o superior.
- Conexión a Internet para instalar las dependencias.
- Terminal o consola de comandos.
- Navegador web para acceder a Streamlit.

## 4. Instalación

### Paso 1. Preparar el proyecto

Descargar o copiar los archivos del proyecto en una misma carpeta.

Si se está actualizando una instalación anterior, realizar primero una copia de seguridad del archivo `fantasy.db`.

### Paso 2. Instalar las dependencias

Abrir una terminal en la carpeta del proyecto y ejecutar:

```bash
pip install -r requirements.txt
```

### Paso 3. Configurar la contraseña del administrador

La aplicación utiliza una variable de entorno para establecer de forma segura la contraseña inicial del administrador.

La contraseña debe tener al menos 12 caracteres.

**Windows (PowerShell):**

```powershell
$env:FANTASY_ADMIN_PASSWORD = "UnaClaveLarga-y-Unica-2026!"
```

**Linux / macOS:**

```bash
export FANTASY_ADMIN_PASSWORD='UnaClaveLarga-y-Unica-2026!'
```

Sustituir la contraseña de ejemplo por una contraseña propia y segura.

### Paso 4. Ejecutar la aplicación

Desde la carpeta del proyecto, ejecutar:

```bash
streamlit run fantasy_local.py
```

La aplicación se abrirá automáticamente en el navegador. Si no ocurre, Streamlit mostrará una dirección local, normalmente:

```text
http://localhost:8501
```

## 5. Configuración de administración

El usuario administrador predeterminado es:

```text
Usuario: usuariomaestro
```

La contraseña se establece mediante la variable de entorno `FANTASY_ADMIN_PASSWORD`.

La aplicación crea el administrador si no existe y, cuando detecta la contraseña predeterminada de la versión anterior (`1234`), permite sustituirla por la nueva contraseña configurada.

No se crea un administrador con una contraseña predeterminada si no se ha configurado previamente la variable de entorno.

## 6. Variables de entorno

La aplicación admite las siguientes variables:

| Variable | Descripción |
|---|---|
| `FANTASY_ADMIN_PASSWORD` | Contraseña de aprovisionamiento del administrador. Mínimo 12 caracteres. |
| `FANTASY_DB_PATH` | Ruta alternativa para almacenar o localizar la base de datos SQLite. |

Si no se configura `FANTASY_DB_PATH`, se utilizará `fantasy.db` en la carpeta de ejecución.

## 7. Reglas de funcionamiento

### Gestión de usuarios
- Cada usuario recibe inicialmente 20 rupias.
- Cada usuario puede gestionar una plantilla de hasta 5 jugadores.
- No se permite fichar dos veces al mismo jugador.
- Se permiten un máximo de 4 jugadores titulares.
- Los jugadores restantes permanecen en el banquillo.

### Mercado de fichajes
- Los usuarios pueden comprar y vender jugadores.
- Los precios evolucionan según la demanda.
- Los jugadores sin propietarios experimentan una reducción de precio.
- La fluctuación se limita a una ejecución por jornada.
- Cuando no existen jornadas pendientes, se limita a una ejecución diaria.
- El precio máximo de un jugador está limitado a 1.000.000 de rupias.

### Bloqueo del mercado
- El mercado permanece disponible antes del inicio del siguiente partido.
- Desde la hora de inicio de un partido pendiente, se bloquean los fichajes y los cambios de plantilla.
- El mercado vuelve a habilitarse una vez que el administrador procesa el partido.

### Sistema de votaciones
- La votación se habilita 5 horas después del inicio del partido.
- El plazo de votación finaliza 48 horas después del inicio.
- Cada usuario puede votar una sola vez por partido.
- Las puntuaciones se calculan utilizando las estadísticas y las valoraciones recibidas.
- No se puede procesar una jornada mientras las votaciones permanezcan abiertas.

### Sistema de puntuación
Los puntos se calculan en función de la posición del futbolista, sus goles, asistencias, porterías a cero y penaltis.

También se incorpora la media de las valoraciones de los usuarios.

Los puntos obtenidos se asignan:
- Al acumulado individual del jugador.
- Al acumulado del manager que tenga al jugador como titular.

### Clasificación
La clasificación general ordena a los usuarios según sus puntos acumulados.

### Administración
El administrador dispone de herramientas para:
- Crear y eliminar partidos.
- Añadir, modificar y eliminar jugadores.
- Modificar los saldos de los usuarios.
- Ejecutar las fluctuaciones del mercado.
- Introducir las estadísticas de los partidos.
- Procesar y revertir jornadas.
- Consultar las puntuaciones.

## 8. Base de datos y migraciones

La aplicación utiliza SQLite para almacenar la información.

Las tablas principales son:

- `users`: cuentas, roles, rupias y puntuaciones de los usuarios.
- `players`: jugadores, posiciones, precios y puntos acumulados.
- `user_squads`: plantillas y alineaciones.
- `matches`: jornadas y partidos.
- `ratings`: valoraciones de los usuarios.
- `match_history`: historial de puntos asignados a los managers.
- `match_player_history`: historial de puntos individuales de los futbolistas.
- `market_updates`: registro de las fluctuaciones ejecutadas.

La inicialización incorpora migraciones para adaptar instalaciones anteriores y conservar los datos existentes.

También se aplican restricciones de integridad para evitar duplicidades y operaciones incompatibles.

### Importante al actualizar versiones

Antes de sustituir el código, realizar siempre una copia de seguridad de `fantasy.db`.

Las jornadas procesadas con la versión antigua no disponen de un historial individual completo de puntos por jugador. Por tanto, no pueden revertirse automáticamente con garantías.

La aplicación identifica estos registros y bloquea su reversión automática para evitar errores en los acumulados.

No eliminar manualmente las tablas de historial ni modificar directamente los registros de puntuaciones
