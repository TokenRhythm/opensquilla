<!-- Traducido de README.md @ 8794ffbe. El README en inglés es la fuente autorizada. -->
<!-- Comprobar si está desactualizado: git log 8794ffbe..HEAD -- README.md -->

# OpenSquilla — Agente de IA eficiente en tokens

<p align="center">
  <img src="assets/opensquilla-long-logo.png" alt="OpenSquilla logo" width="500">
</p>

<p align="center">
  <b>Con el mismo presupuesto, haz que tu agente haga más y lo haga mejor.</b><br>
  Agente de IA con microkernel: enrutamiento inteligente, memoria persistente, sandbox seguro, búsqueda integrada y embeddings locales.
</p>

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/opensquilla/opensquilla/ci.yml?style=for-the-badge" alt="CI"></a>
  <a href="https://opensquilla.ai/"><img src="https://img.shields.io/badge/website-opensquilla.ai-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://github.com/opensquilla/opensquilla/releases"><img src="https://img.shields.io/github/v/release/opensquilla/opensquilla?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge" alt="Apache 2.0 License"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-Hans.md">中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.fr.md">Français</a> · <a href="README.de.md">Deutsch</a> · <b>Español</b>
</p>

> Este documento es una traducción del [`README.md`](README.md) en inglés; si hay alguna discrepancia, la versión en inglés es la autorizada.

---

## Novedades

- 📢 **2026-07-03** — Nuestro informe técnico **[Agentic Routing: The Harness-Native Data Flywheel](docs/releases/agentic_routing_v0.pdf)** (versión preliminar) ya está disponible, publicado junto con OpenSquilla **0.5.0 Preview 1**. Detalla cómo el enrutador nativo del harness convierte el tráfico cotidiano de los agentes en un volante de datos que se mejora a sí mismo.

---

## Resumen

OpenSquilla es un agente de IA con microkernel y eficiente en el uso de tokens. Un enrutador de modelos local envía cada turno al modelo más económico que pueda resolverlo, mientras que la memoria persistente, un sandbox por capas, la búsqueda web integrada y los embeddings en el propio dispositivo completan un único bucle de turnos compartido.

Cada punto de entrada —Web UI, CLI y canales de chat— se ejecuta a través de ese mismo bucle, de modo que el envío de herramientas, los reintentos y el registro de decisiones se comportan de forma idéntica en todas partes. Una capa de proveedores conectable se comunica con TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama, DeepSeek, Gemini, Qwen/DashScope y más de 20 proveedores de LLM adicionales, sin ningún cambio en tu código ni en el esquema de configuración.

OpenSquilla 0.5.0 es la versión estable actual.

Para documentación de producto orientada a tareas, comienza por la [Guía de producto de OpenSquilla](README.product.md) o el [índice de documentación](docs/README.md).

---

## Instalación

OpenSquilla funciona en Windows, macOS y Linux. Elige la ruta que se ajuste a tu caso de uso.

Los instaladores de escritorio y la instalación rápida desde terminal te ofrecen una **versión** precompilada, sin necesidad de Git. Las otras dos —instalar desde el código fuente y desarrollar desde el código fuente— compilan también la consola Vue **a partir de un checkout de Git** (`git clone` + Git LFS). Los wheels publicados y los instaladores de escritorio ya incluyen la consola; sus usuarios no necesitan Node.js ni npm.

Los comandos de instalación de versiones usan los recursos de release publicados en GitHub. Las instalaciones del wheel de Python usan nombres de archivo de wheel con versión, porque los instaladores validan la versión incrustada en el nombre del archivo del wheel.

Para el uso de escritorio de 0.5.0, opta por los instaladores de escritorio empaquetados de la Release de GitHub: `OpenSquilla-0.5.0-mac-arm64.dmg` en macOS y `OpenSquilla-0.5.0-win-x64.exe` en Windows.

| Ruta | Público | Cuándo usarla |
| --- | --- | --- |
| [Instaladores de escritorio](#desktop-installers) **(recomendado para escritorio)** | Usuarios de macOS y Windows | Aplicación de escritorio empaquetada |
| [Instalación rápida desde terminal](#quick-terminal-install) **(recomendado)** | Usuarios finales en cualquier SO | Wheel de release desde una terminal |
| [Instalar desde el código fuente](#install-from-source) | Usuarios que siguen `main` | Ejecutar desde un checkout, no editarlo |
| [Desarrollar desde el código fuente](#develop-from-source) | Colaboradores | Editar, probar o depurar el código fuente |

### Requisitos previos

| Requisito | Instalación rápida desde terminal | Instalar desde el código fuente | Desarrollar desde el código fuente |
| --- | :---: | :---: | :---: |
| Python 3.12+ | mediante `uv` | mediante `uv` o el sistema | mediante `uv` |
| Git + Git LFS | — | requerido | requerido |
| Node.js 22.12+ + npm | — | requerido para compilar la Web UI | requerido para la Web UI y los wheels |
| `uv` | se instala si falta | recomendado | requerido |

El perfil predeterminado `recommended` instala **SquillaRouter** —el enrutador de modelos en el dispositivo de OpenSquilla— y sus recursos de modelo; `OPENSQUILLA_INSTALL_PROFILE=core` omite esas dependencias. El indicador de onboarding independiente `--router disabled` mantiene las dependencias instaladas, pero apaga el enrutador en tiempo de ejecución.

En Windows, el runtime ONNX que incluye SquillaRouter también necesita el runtime de Visual C++. El instalador de PowerShell desde el código fuente lo instala automáticamente mediante `winget`; la ruta de **instalación rápida desde terminal** (`uv tool install`) no lo hace: si el arranque registra un error `DLL load failed`, instálalo manualmente (consulta [Solución de problemas](#troubleshooting)). OpenSquilla sigue funcionando con enrutamiento directo a un único modelo hasta que se instale.

En las instalaciones desde terminal de macOS, el runtime LightGBM de SquillaRouter también puede necesitar la biblioteca OpenMP del sistema. La aplicación de escritorio incluye el runtime que necesita, pero la **instalación rápida desde terminal** no instala bibliotecas de Homebrew ni del sistema. Si el arranque registra `Library not loaded: @rpath/libomp.dylib`, ejecuta `brew install libomp` y luego reinicia el gateway. OpenSquilla sigue funcionando con enrutamiento directo a un único modelo hasta que se instale.

Enlaces de instalación: [Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[Node.js](https://nodejs.org/en/download) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/).

<a id="desktop-installers"></a>

### Instaladores de escritorio

Los instaladores de escritorio de 0.5.0 empaquetan la consola de control de Vue y el runtime del gateway en una carcasa de Electron.

- macOS Apple Silicon: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg>
- Windows x64: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe>

Para descargas más rápidas desde China continental, usa los alias de descarga directa de OSS:
- macOS Apple Silicon: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg>
- Windows x64: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe>

Estos enlaces fijos solo avanzan cuando una release elegible más reciente supera la verificación de la réplica. Usa los enlaces versionados de GitHub Release indicados arriba si necesitas una versión concreta.

Cierra cualquier aplicación de escritorio de OpenSquilla en ejecución antes de actualizar. Se reutiliza el perfil Desktop existente en el directorio de datos de aplicaciones de la plataforma. El `~/.opensquilla` de la instalación de terminal es un perfil distinto; transfiérelo explícitamente desde Ajustes si es necesario.

Al actualizar la aplicación de escritorio de Windows de RC3 a RC4 o una versión
posterior, ejecuta el instalador nuevo directamente sobre la instalación existente.
No desinstales RC3 primero: su desinstalador puede eliminar los datos de usuario de
la aplicación. Haz una copia de seguridad de `%APPDATA%\OpenSquilla` antes de
actualizar. Los instaladores RC4 y posteriores conservan los datos del perfil durante
una desinstalación normal.

<a id="quick-terminal-install"></a>

### Instalación rápida desde terminal

La ruta recomendada en Windows, macOS y Linux. `uv` instala OpenSquilla en su propio entorno aislado y gestiona su propio Python, sin necesidad de un Python del sistema. Esta ruta instala únicamente versiones publicadas; para `main`, ramas de desarrollo o checkouts locales, usa [Instalar desde el código fuente](#install-from-source).

**1. Instala `uv`**: omite este paso si `uv --version` ya funciona.

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. Instala OpenSquilla**: el mismo comando en todas las plataformas.

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

Esto instala el wheel de OpenSquilla desde la URL de la release y luego deja que `uv` descargue las dependencias declaradas por los extras seleccionados. El extra predeterminado `recommended` incluye dependencias del runtime de SquillaRouter como ONNX Runtime, LightGBM, NumPy y tokenizers, así que una primera instalación necesita acceso a la red salvo que esos wheels ya estén en caché. `uv` no instala runtimes nativos del sistema como `libomp` de macOS o el Visual C++ Redistributable de Windows; consulta [Solución de problemas](#troubleshooting) si el runtime del enrutador informa de un error de carga de biblioteca nativa.

**3. Configura y ejecuta.**

```sh
opensquilla onboard
opensquilla gateway run
```

> [!NOTE]
> Si no se encuentra `opensquilla` justo después de una instalación nueva con `uv`, abre una terminal nueva o vuelve a ejecutar la línea de PATH del paso 1.

Para una instalación totalmente fijada, usa la URL del wheel con versión:
`https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`.

<a id="install-from-source"></a>

### Instalar desde el código fuente

Usa esta ruta para ejecutar OpenSquilla desde un checkout sin editarlo. El clon es solo el código fuente del paquete para el instalador; tras instalar, usa el comando `opensquilla`, no ejecutes `uv run`. Elige en su lugar [Desarrollar desde el código fuente](#develop-from-source) si tu intención es modificar el código.

1. **Clona con los recursos LFS**

   ```sh
   git lfs install
   git clone https://github.com/opensquilla/opensquilla.git
   cd opensquilla
   git lfs pull --include="src/opensquilla/squilla_router/models/**"
   ```

2. **Ejecuta el instalador**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   El script ejecuta primero `npm ci` y `npm run build` en `opensquilla-webui`, y después instala `.[recommended]` (SquillaRouter + memoria + modelos locales) en un entorno de usuario dedicado mediante `uv tool install`, recurriendo a `python -m pip install --user` cuando `uv` no está disponible. Cada reinstalación desde el código fuente recrea `node_modules` y recompila la consola. La primera ejecución suele descargar más; la caché de npm reduce después el tráfico de red, pero no elimina todo el tiempo de compilación ni las escrituras de disco. Abre una terminal nueva si `opensquilla` no está en el `PATH` tras la instalación.

   `pip install .`, `uv tool install .` y las instalaciones mediante una URL VCS
   son rutas de compilación de bajo nivel, no sustitutos de este script. Un
   checkout local necesita primero una Web UI compilada; un checkout creado por
   una URL VCS no contiene el artefacto generado y se rechaza deliberadamente.
   Usa el instalador desde el código fuente o un wheel oficial de la release.

3. **(opcional) Instala extras avanzados.** La mayoría de los canales —Feishu, Telegram, DingTalk, QQ, WeCom, Slack y Discord— funcionan desde la instalación base. Los extras opcionales son:

   - `matrix` — Canal de Matrix (incorpora `matrix-nio`)
   - `matrix-e2e` — Canal de Matrix con cifrado de extremo a extremo (requiere libolm)
   - `document-extras` — Generación de PDF mediante WeasyPrint

   ```sh
   OPENSQUILLA_INSTALL_EXTRAS=matrix bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras matrix   # Windows
   ```

4. **Configura y ejecuta**: consulta [Configuración](#configuration).

<details>
<summary>Instalar desde el código fuente: requisitos previos de terminal y opciones del instalador</summary>

**Instalar los requisitos previos (Git, Git LFS, Node.js 22.12+ con npm, uv) desde una terminal**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS (Homebrew):

```sh
brew install git git-lfs node uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

En Fedora usa `sudo dnf install -y git git-lfs`; en Arch usa `sudo pacman -S --needed git git-lfs`; instala también Node.js 22.12+ y npm desde la distribución o nodejs.org, y luego instala `uv` con el comando `curl` anterior. Los cambios de PATH de estos instaladores se aplican a las nuevas sesiones de terminal.

**Variables de entorno del instalador y comprobaciones de PATH**

```sh
OPENSQUILLA_INSTALL_PROFILE=core   bash scripts/install_source.sh   # runtime mínimo, sin SquillaRouter
OPENSQUILLA_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # solo imprime el plan
```

Verifica qué `opensquilla` ejecuta tu shell con `command -v opensquilla` (macOS/Linux) o `where.exe opensquilla` (Windows). Si no está en el `PATH`, ejecuta `uv tool update-shell`. Tras reinstalar desde un checkout local, reinicia el gateway para que cargue el paquete actualizado.

</details>

<a id="develop-from-source"></a>

### Desarrollar desde el código fuente

Usa esta ruta cuando estés trabajando en el código fuente de OpenSquilla: haciendo cambios, ejecutando pruebas o depurando el comportamiento contra este checkout. No es la ruta de instalación habitual. A diferencia de [Instalar desde el código fuente](#install-from-source), esta ruta requiere `uv`: `uv sync` crea un `.venv` local del repositorio y `uv run` ejecuta los comandos contra los archivos de este checkout.

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
```

Vuelve a ejecutar `npm run build` después de cambiar la Web UI. Una compilación
estándar del wheel falla si la consola falta o está obsoleta; `uv sync` editable
sigue disponible para el trabajo exclusivo de backend.

El extra `recommended` también incluye SquillaRouter para el desarrollo; el extra `dev` instala las herramientas de prueba, lint y comprobación de tipos. Instala extras adicionales en el mismo entorno que ejecutas:

```sh
uv sync --extra recommended --extra dev --extra matrix
uv run opensquilla channels status matrix --json
```

En este modo, antepón `uv run` a cada comando `opensquilla` de [Configuración](#configuration). No depures un checkout de desarrollo a través de un comando `opensquilla` local del usuario: ese comando se ejecuta en un entorno de Python distinto.

### Desinstalación

Elimina OpenSquilla con `opensquilla uninstall`. Conserva tus datos de forma predeterminada y elimina solo el programa:

```sh
opensquilla uninstall --dry-run   # previsualiza qué se eliminaría y qué se conservaría
opensquilla uninstall             # elimina el programa, conserva tus datos
```

Para eliminar también los datos, opta explícitamente por ello:

```sh
opensquilla uninstall --purge-state    # sesiones, registros, caché, programador, memoria
opensquilla uninstall --purge-config   # config.toml y secretos (.env)
opensquilla uninstall --purge-all      # todo (te pide que escribas una confirmación)
```

Primero se vacía y detiene el gateway en ejecución, la eliminación se mantiene dentro del directorio raíz de OpenSquilla, y las instalaciones de Docker o de escritorio obtienen pasos de eliminación guiados en su lugar. Consulta [`docs/cli.md`](docs/cli.md#uninstall) para la referencia completa.

---

## Privacidad de la instalación

OpenSquilla utiliza telemetría de instalación anónima para estimar el número de
instalaciones, la adopción de versiones y la compatibilidad en tiempo de ejecución. Los
datos se envían únicamente cuando el gateway se inicia por primera vez y solo una vez
por cada versión de OpenSquilla. También agrega localmente, por fecha UTC, el número de
turnos de conversación de nivel superior completados y el uso de tokens. Al arrancar y
después cada hora, OpenSquilla intenta enviar al mismo servicio de telemetría las
instantáneas acumuladas pendientes del día UTC. OpenSquilla también puede realizar
comprobaciones pasivas de actualizaciones, incluidas las realizadas al iniciar la
aplicación de escritorio y, como máximo, una vez al día mientras la aplicación continúa
en ejecución. Las cargas usan un tiempo de espera corto y nunca bloquean el arranque.

Lo que se envía:

- versión del esquema
- resumen (digest) `install_id` estable generado localmente
- versión de OpenSquilla
- tipo de evento (`install`, `version_seen` o `daily_usage`)
- método de instalación (`pip`, `source`, `docker`, `desktop` o `unknown`)
- sistema operativo, versión del SO, arquitectura de CPU y versión mayor/menor de Python
- marcas de tiempo de primera detección y de envío
- marcador de entorno de CI/pruebas (`ci_environment`)
- en los eventos de uso diario: fecha UTC, número de turnos completados y totales de
  tokens de entrada, salida, caché y escritura en caché

El `install_id` es un resumen SHA-256 local y unidireccional derivado de direcciones MAC utilizables, luego de direcciones IP locales cuando no hay ninguna MAC disponible, con un valor aleatorio persistente de reserva. Los valores MAC/IP en bruto no se cargan.

Lo que no se envía: nombres de usuario, nombres de host, rutas, claves de API, configuración de proveedores, contenido de chat/sesión/memoria/agente, nombres de archivo ni contenido de archivos. La IP de origen puede ser visible para los servidores HTTP en la capa de transporte, pero no forma parte de la carga útil.

Para desactivar antes del arranque toda observabilidad de red no iniciada por el
usuario:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

O en la configuración:

```toml
[privacy]
disable_network_observability = true
```

Este interruptor unificado abarca la telemetría automática de instalación, la
telemetría de uso diario agregada, las comprobaciones pasivas de actualizaciones y las
comprobaciones automáticas realizadas al iniciar la aplicación de escritorio y mientras
continúa en ejecución. Mientras siga activado el interruptor unificado o uno de los
interruptores de desactivación compatibles, ni siquiera una comprobación de
actualizaciones iniciada explícitamente por el usuario podrá omitirlo. Otras acciones
iniciadas por el usuario todavía pueden acceder a servicios de red después de expresar
una intención clara, por ejemplo, abrir la página de versiones, descargar archivos de
una versión o utilizar proveedores, búsquedas o canales configurados.

Las variables de entorno anteriores siguen siendo compatibles:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

Las implementaciones avanzadas pueden usar su propio endpoint de telemetría de
instalación:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
```

---

<a id="configuration"></a>

## Configuración

### Configuración inicial

`opensquilla onboard` es el asistente interactivo de configuración inicial. Escribe el archivo de configuración activo y mantiene los secretos del proveedor en variables de entorno cuando pasas `--api-key-env`. El enrutador usa `recommended` de forma predeterminada (SquillaRouter en proveedores compatibles); pasa `--router disabled` para enrutamiento directo a un único modelo.

```sh
opensquilla onboard                # asistente interactivo completo
opensquilla onboard --if-needed    # idempotente: seguro para scripts y reinstalaciones
opensquilla onboard --minimal      # solo el proveedor; omite canales y búsqueda
opensquilla onboard status         # inspecciona cada sección de configuración sin escribir
```

En SSH, CI o cualquier entorno sin TTY, usa la forma no interactiva: mantén el secreto en el entorno y pasa su **nombre**, no su valor:

**Linux / macOS**

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

**Windows PowerShell**

```powershell
$env:OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

OpenRouter es solo un ejemplo: sustitúyelo por cualquier proveedor compatible y su variable de clave de API.

Vuelve a configurar una sección más tarde sin rehacer todo el asistente (estos ejemplos asumen que la clave de API correspondiente ya está en el entorno):

```sh
opensquilla configure provider --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY
opensquilla configure router --router recommended
opensquilla configure search   --search-provider duckduckgo
opensquilla configure search   --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure channels
```

Secciones: `provider`, `router`, `channels`, `search`, `image-generation`, `memory-embedding`. La Web UI expone el mismo catálogo y modelo de estado en `/control/setup`: Provider y Router son la ruta rápida, mientras que Channels, Search, Image generation y Memory embedding se encuentran en el Centro de capacidades (Capability Center) y pueden configurarse más tarde. Dejar los canales vacíos se trata como una exclusión voluntaria, no como una configuración fallida.

**Orden de carga de la configuración:** `OPENSQUILLA_GATEWAY_CONFIG_PATH` →
`./opensquilla.toml` → `~/.opensquilla/config.toml` → valores predeterminados integrados. Para los secretos individuales, los valores del entorno siempre prevalecen sobre los del archivo.

### Migrar desde OpenClaw o Hermes Agent

Si ya tienes estado en `~/.openclaw` o `~/.hermes`, ejecuta primero un dry run para revisar el informe de migración y luego aplícalo explícitamente:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply

opensquilla migrate hermes --json
opensquilla migrate hermes --apply
```

Usa `opensquilla migrate --source openclaw,hermes --apply` para importar ambos directorios raíz predeterminados. Añade `--migrate-secrets` solo después de revisar el informe del dry run. Consulta [`MIGRATION.md`](MIGRATION.md) para rutas personalizadas y la gestión de conflictos.

### Ejecución

```sh
opensquilla gateway run                # en primer plano, 127.0.0.1:18791
opensquilla gateway start --json       # en segundo plano + espera de comprobación de salud
opensquilla chat                       # REPL interactivo
opensquilla agent -m "tu prompt"       # una sola vez, apto para automatización
```

Abre la Web UI en <http://127.0.0.1:18791/control/>. La vista **Health (Salud)** muestra si OpenSquilla está listo, qué no está listo y los siguientes pasos de recuperación. Desde la CLI, ejecuta:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla doctor --config ./opensquilla.toml --json
```

`/health` y `/healthz` son endpoints de liveness ligeros para las comprobaciones de proceso. `opensquilla doctor` y la vista Health de la Web UI son las superficies de comprobación de disponibilidad para la configuración del proveedor, la memoria, los registros, la búsqueda, los canales, la postura del sandbox, el enrutador, la generación de imágenes y la orientación de recuperación. Pulsa `Ctrl+C` para detener un gateway en primer plano.

Otros grupos de comandos incluyen `sessions`, `skills`, `memory`, `migrate`, `cron`, `channels`, `providers`, `models` y `cost`. Ejecuta `opensquilla --help` o `opensquilla <grupo> --help` para más detalles.

<details>
<summary>Configuración avanzada: verificar un canal, vinculación a la red pública, Docker</summary>

**Conectar y verificar un canal de mensajería**

Guardar un canal es un cambio de configuración, no una prueba de conectividad en tiempo de ejecución. Reinicia el gateway tras editar canales y luego verifica el canal en vivo:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

Considera un canal como conectado solo cuando la carga útil de estado informa `enabled=true`, `configured=true` y `connected=true`. Feishu usa el modo websocket de forma predeterminada, Telegram usa polling y Slack puede usar Socket Mode: ninguno de esos modos necesita una URL pública. El modo webhook de Feishu, el modo webhook de Telegram, el modo webhook de Slack y WeCom requieren una URL pública y accesible por el proveedor.

**Vinculación a la red pública**

Para llegar a la Web UI desde otra máquina, vincula el gateway a todas las interfaces y usa la IP pública del host:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

El acceso público también requiere que el firewall del host o el grupo de seguridad de la nube permita el TCP entrante en ese puerto. No expongas el gateway con `[auth] mode = "none"`: configura la autenticación por token antes de vincularlo a `0.0.0.0`.

**Docker**

Se publican imágenes multiarquitectura preconstruidas (`amd64`/`arm64`) en `ghcr.io/opensquilla/opensquilla` con cada etiqueta de release; [`docs/docker.md`](docs/docker.md) es la guía completa de contenedores (servidores domésticos y NAS, exposición en la LAN con autenticación por token, actualizaciones):

```sh
OPENSQUILLA_GATEWAY_IMAGE=ghcr.io/opensquilla/opensquilla:latest docker compose up -d
```

Sin `OPENSQUILLA_GATEWAY_IMAGE`, la ruta de compose ejecuta una imagen `opensquilla:local` que construyes tú mismo. Constrúyela a partir de un checkout del código fuente con los recursos del enrutador de Git LFS descargados (consulta [Instalar desde el código fuente](#install-from-source) para el clon y `git lfs pull`):

```sh
docker build -t opensquilla:local .
```

Luego, `./start.sh` (o `start.ps1` en Windows) ejecuta `docker compose up -d` y sigue los registros del gateway. Docker evita una cadena de herramientas de Python en el host, no la construcción de la imagen local.

</details>

Los niveles de proveedor, el ajuste del sandbox, la generación de imágenes y los ajustes de concurrencia están en `opensquilla.toml.example`.

---

## Novedades en 0.4.1

OpenSquilla 0.4.1 es una versión de mantenimiento para la línea de escritorio y de la Control UI:

- **Fiabilidad del escritorio**: las comprobaciones del gateway empaquetado ahora cubren el modo Coding, `code-task` y el arranque de SquillaRouter, y la gestión de ventanas/artefactos del escritorio es más estable.
- **Compatibilidad de cliente en seis idiomas**: la Control UI y el cliente de escritorio admiten inglés, chino simplificado, japonés, francés, alemán y español en las superficies de primer pintado y de configuración.
- **Modo Coding y empaquetado del enrutador**: las compilaciones de escritorio fallan rápidamente si faltan los recursos del enrutador o siguen siendo punteros de Git LFS, lo que evita paquetes de release degradados.
- **Telemetría y pulido de Windows**: la telemetría de instalación omite los entornos de CI y de pruebas, y los recursos de escritorio de Windows usan el logotipo de OpenSquilla.
- **Gobernanza de la línea principal**: las pull request ordinarias y la integración de releases se alinean en torno a `main`, con ramas de mantenedor reservadas para el trabajo de release, hotfix, staging, integración y sandbox.

Notas completas: [`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/releases/0.4.1.md`](docs/releases/0.4.1.md).

## Novedades en 0.2.1

OpenSquilla 0.2.1 es una versión de mantenimiento centrada en el arranque del paquete de release y la fiabilidad del agente de larga duración:

- **Arranque de la versión portable de Windows**: el lanzador portable detecta y arranca mejor el runtime de Visual C++ que necesita el enrutador ONNX incluido.
- **Turnos de agente de larga duración**: las sesiones de WebUI con uso intensivo de herramientas se recuperan de forma más limpia ante resultados de herramienta sobredimensionados, llamadas de herramienta mal formadas, transferencias de entrega de artefactos y respuestas finales degradadas.
- **Salida de WebUI más limpia**: los marcadores de artefactos generados se mantienen fuera de la repetición normal del chat, mientras que los archivos entregados siguen siendo visibles.
- **Puntuación de recuerdo de memoria**: los vectores de embedding locales y compatibles con OpenAI se normalizan antes de la búsqueda semántica, y las coincidencias fuertes de palabras clave siguen siendo útiles cuando las puntuaciones vectoriales son bajas.

Notas completas: [`CHANGELOG.md`](CHANGELOG.md) ·
[notas de la versión](https://opensquilla.ai/news/).

## Novedades en 0.2.0

Esta versión amplía OpenSquilla en migración, chat por CLI, canales, programación y trabajo de herramientas de larga duración:

- **Ruta de migración desde directorios raíz de agente existentes**: `opensquilla migrate` previsualiza y aplica importaciones desde directorios raíz existentes de OpenClaw/Hermes, incluyendo memoria, archivos de persona, skills, configuración de MCP/canales, gestión de conflictos e informes de migración.
- **CLI de chat utilizable**: `opensquilla chat` tiene una interfaz de terminal estable, salida en streaming, entrada en cola, descubrimiento del modo slash, barras de herramientas/estado y un comportamiento de prompt en vivo más determinista.
- **Automatización de cron entre superficies**: los trabajos cron ahora cubren programaciones estructuradas, ejecuciones exactas/periódicas/cron con reconocimiento de zona horaria, entrega por canal o webhook, destinos de fallo, ejecuciones manuales y paridad entre WebUI/CLI/RPC.
- **Mejores canales de Feishu y Discord**: los adaptadores de canal exponen metadatos de capacidad más claros, una gestión más segura de DM/grupos, rutas nativas de archivos y artefactos, y un comportamiento mejorado de adjuntos/hilos, mientras que las acciones privilegiadas mantienen un alcance acotado.
- **Turnos de larga duración más robustos**: los turnos fallidos se mantienen fuera de la repetición del proveedor, las llamadas de herramienta mal formadas se gestionan de forma más segura y los reintentos sujetos a aprobación esperan las decisiones del operador.
- **Presupuestación más inteligente de contexto y herramientas**: la compactación según el presupuesto del proveedor, la preservación de la caché de prompts, los resultados de herramienta acotados y la concurrencia consciente de efectos secundarios hacen que las grandes sesiones con uso intensivo de herramientas sean más predecibles.
- **Pulido de la Web UI y de la release**: se afinaron para 0.2.0 el orden por recencia, el diseño de tablas, los controles móviles, las notificaciones duplicadas, los formularios de configuración, las URL de release y las rutas de instalación.

Notas completas: [`CHANGELOG.md`](CHANGELOG.md) ·
[notas de la versión](https://opensquilla.ai/news/).

---

## Características clave

| Capacidad | Qué hace |
| --- | --- |
| **Enrutamiento eficiente en tokens** | `SquillaRouter` —un clasificador local de LightGBM + ONNX incluido en el extra `recommended`— puntúa cada turno según su longitud, idioma, código, palabras clave y embeddings semánticos, y luego lo enruta a través de cuatro niveles (C0–C3; los antiguos nombres T0–T3 son alias) hacia el modelo capaz más económico. La clasificación se ejecuta en el dispositivo; tu prompt nunca sale de la máquina para tomar esa decisión. |
| **Razonamiento y prompts adaptativos** | OpenSquilla solicita razonamiento extendido únicamente para los turnos que el enrutador puntúa como complejos, y el prompt del sistema se ajusta a la complejidad de la tarea: ligero para los turnos triviales, con instrucciones completas para los complejos. |
| **Más de 20 proveedores de LLM** | El registro de proveedores apunta a más de 20 backends de LLM —TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama, DeepSeek, Gemini, DashScope/Qwen, Moonshot, Mistral, Groq, Zhipu, SiliconFlow, vLLM, LM Studio y más— con selección de proveedor principal más reserva; el onboarding de la primera ejecución expone el subconjunto verificado. |
| **Skills bajo demanda y MCP** | 15 skills incluidas (coding, GitHub, cron, pptx/docx/xlsx/pdf, resumen, tmux, clima y más) se cargan solo cuando la tarea lo necesita. OpenSquilla es un cliente MCP y también puede ejecutarse como servidor MCP: `opensquilla mcp-server run` necesita el extra `mcp` (instala `opensquilla[recommended,mcp]`). Las skills se pueden crear, instalar y publicar desde la CLI. |
| **Memoria local persistente** | Un `MEMORY.md` curado más notas Markdown fechadas, consultadas con búsqueda de palabras clave de texto completo de SQLite y recuerdo semántico con `sqlite-vec`. Los embeddings se ejecutan en el dispositivo mediante el ONNX incluido, o puedes cambiar a OpenAI/Ollama. Están disponibles un decaimiento exponencial opcional y una consolidación «dream» opcional. |
| **Sandbox de seguridad por capas** | Tres niveles de política (Standard / Strict / Locked) sobre una matriz de permisos. Bubblewrap aísla la ejecución de código en Linux; el backend Seatbelt de macOS actualmente solo renderiza perfiles (la ejecución está pendiente), y todavía no hay backend de sandbox en Windows. Un registro de denegaciones (denial ledger) pausa automáticamente las ejecuciones autónomas tras denegaciones repetidas, las salidas rechazadas se purgan, y los metadatos de las skills y los resultados de las herramientas se escapan en XML como protección contra la inyección de prompts. |
| **Herramientas integradas** | Lectura/escritura/edición de archivos, shell y procesos en segundo plano, git, búsqueda web (DuckDuckGo, Bocha, Brave, Tavily o Exa) y fetch tras una protección SSRF, creación de hojas de cálculo/PPTX/PDF, generación de imágenes y conversión de texto a voz. |
| **Gateway unificado** | Un servidor ASGI de Starlette en `127.0.0.1:18791` con RPC por WebSocket y una consola de control integrada (`/control/`). La Web UI, la CLI y los canales de Terminal, WebSocket, Slack, Telegram, Discord, Feishu, DingTalk, WeCom, Matrix y QQ comparten todos un mismo `TurnRunner`. |
| **Sesiones duraderas, subagentes y programación** | Almacenamiento de sesiones, transcripciones y repetición respaldado por SQLite, con espacios de trabajo por agente. Los agentes generan subagentes con profundidad acotada, y un `SchedulerEngine` con un parser de cron incorporado ejecuta trabajos recurrentes mediante `opensquilla cron`. |
| **Controles del operador** | Las aprobaciones con humano en el bucle (human-in-the-loop) pueden pausar llamadas de herramienta sensibles a la espera de una decisión; los resúmenes de tokens y coste por turno y por sesión (`opensquilla cost`) y los diagnósticos están disponibles desde la CLI y la Web UI. |

Documentación de MetaSkill: [`docs/features/meta-skills.md`](docs/features/meta-skills.md),
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md)
y [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md).

---

## Resultados de benchmark

Resultados promedio de PinchBench 1.2.1 en 25 tareas:

| Agente | Modelo base | Puntuación media | Tokens de entrada totales | Tokens de salida totales | Coste total |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenSquilla | Enrutador de modelos (Opus4.7, GLM5.1, DS4 Flash) | 0.9251 | 1,721,328 | 61,475 | $0.688 |
| OpenClaw | Claude Opus 4.7 | 0.9255 | 3,066,243 | 50,890 | $6.233 |

La puntuación es la media de las 25 tareas; los recuentos de tokens y el coste son totales de la ejecución completa.

---

<a id="troubleshooting"></a>

## Solución de problemas

<details>
<summary>macOS: <code>Library not loaded: @rpath/libomp.dylib</code></summary>

Si el arranque registra `Library not loaded: @rpath/libomp.dylib` desde `lightgbm/lib/lib_lightgbm.dylib`, OpenSquilla sigue funcionando con enrutamiento directo a un único modelo, pero el runtime `SquillaRouter` incluido permanece inactivo hasta que se instale el runtime OpenMP de macOS.

La aplicación de escritorio incluye el runtime nativo que necesita. Si usaste la instalación rápida desde terminal o la instalación desde el código fuente en un shell, instala `libomp` con Homebrew y reinicia el gateway:

```sh
brew install libomp
opensquilla gateway restart
```

</details>

<details>
<summary>Windows: <code>DLL load failed</code> / runtime de Visual C++</summary>

Si el arranque registra `DLL load failed while importing onnxruntime_pybind11_state`, OpenSquilla sigue funcionando con enrutamiento directo a un único modelo, pero el runtime `SquillaRouter` incluido permanece inactivo hasta que se instale el Visual C++ Redistributable para Visual Studio 2015–2022 (x64).

El instalador de PowerShell desde el código fuente intenta instalar el redistributable mediante `winget`. Si usaste la instalación rápida desde terminal, o `winget` no está disponible, instálalo manualmente y reinicia PowerShell: <https://aka.ms/vs/17/release/vc_redist.x64.exe>. Luego restaura el enrutador recomendado:

```powershell
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY --router recommended
opensquilla gateway restart
```

</details>

---

## Créditos

OpenSquilla está inspirado en [OpenClaw](https://github.com/openclaw/openclaw). El contenido de terceros incluido se atribuye en [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Los colaboradores de la comunidad se reconocen en [`CONTRIBUTORS.md`](CONTRIBUTORS.md), incluidas las notas de atribución específicas de cada release para el trabajo combinado con squash o reproducido.

---

## Colaboradores

Gracias a todas las personas que contribuyen a OpenSquilla.

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=opensquilla/opensquilla&max=100&columns=10" alt="OpenSquilla contributors" />
  </a>
</p>

---

## Cómo contribuir

Las contribuciones de todo tipo son bienvenidas: informes de errores, ideas de funcionalidades, documentación, nuevos adaptadores de proveedores o canales, skills y trabajo en el runtime central. Consulta [`CONTRIBUTING.md`](CONTRIBUTING.md) y luego abre una issue o una pull request en [GitHub](https://github.com/opensquilla/opensquilla).

[Código de conducta](CODE_OF_CONDUCT.md) · [Seguridad](SECURITY.md) ·
[Soporte](SUPPORT.md) · [Licencia](LICENSE) (Apache-2.0)
