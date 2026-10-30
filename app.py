import streamlit as st
import pytesseract
import json
import re
import sqlite3
import hashlib
import hmac
from google import genai
from pypdf import PdfReader
from pdf2image import convert_from_path
from PIL import Image

import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = (
    st.secrets["GEMINI_API_KEY"]
    if "GEMINI_API_KEY" in st.secrets
    else os.getenv("GEMINI_API_KEY")
)

# ==============================================
# BASE DE DATOS SQLITE
# ==============================================

# Ubicación del archivo donde se guardarán los datos
RUTA_BASE_DATOS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "base_documentos.db"
)

# Carpeta donde se guardarán los archivos PDF
CARPETA_DOCUMENTOS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "documentos"
)

# Crear la carpeta si todavía no existe
os.makedirs(CARPETA_DOCUMENTOS, exist_ok=True)

def conectar_base_datos():
    """Conecta la aplicación con la base de datos SQLite."""
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.row_factory = sqlite3.Row
    return conexion

def inicializar_base_datos():
    """Crea la tabla de documentos si todavía no existe."""

    conexion = conectar_base_datos()

    try:
        conexion.execute("""
            CREATE TABLE IF NOT EXISTS documentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                ruta_pdf TEXT,
                texto TEXT,
                datos_json TEXT,
                estado TEXT,
                huella_archivo TEXT UNIQUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Comprobar si la columna ruta_pdf ya existe
        columnas = conexion.execute(
            "PRAGMA table_info(documentos)"
        ).fetchall()

        nombres_columnas = [
            columna["name"] for columna in columnas
        ]

        # Agregar la columna solamente si falta
        if "ruta_pdf" not in nombres_columnas:
            conexion.execute(
                "ALTER TABLE documentos ADD COLUMN ruta_pdf TEXT"
            )
            conexion.commit()

    finally:
        conexion.close()

# Crear la tabla al iniciar la aplicación
inicializar_base_datos()

def guardar_documento(documento):
    """Guarda el documento y su información en SQLite."""

    contenido_pdf = documento["pdf"]
    huella = hashlib.sha256(contenido_pdf).hexdigest()

    # Crear un nombre único para guardar el PDF
    nombre_pdf = f"{huella}.pdf"

    ruta_pdf = os.path.join(
        CARPETA_DOCUMENTOS,
        nombre_pdf
    )

    # Guardar el archivo PDF si todavía no existe
    if not os.path.exists(ruta_pdf):
        with open(ruta_pdf, "wb") as archivo:
            archivo.write(contenido_pdf)

    conexion = conectar_base_datos()

    try:
        conexion.execute("""
            INSERT INTO documentos (
                nombre,
                ruta_pdf,
                texto,
                datos_json,
                estado,
                huella_archivo
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(huella_archivo) DO UPDATE SET
                nombre = excluded.nombre,
                ruta_pdf = excluded.ruta_pdf,
                texto = excluded.texto,
                datos_json = excluded.datos_json,
                estado = excluded.estado
        """, (
            documento["nombre"],
            ruta_pdf,
            documento["texto"],
            json.dumps(
                documento.get("datos"),
                ensure_ascii=False
            ),
            documento["estado"],
            huella
        ))
        
        conexion.commit()

    finally:
        conexion.close()

def cargar_documentos_desde_base():
    """Recupera los documentos guardados en SQLite."""

    conexion = conectar_base_datos()

    try:
        filas = conexion.execute("""
            SELECT nombre, ruta_pdf, texto, datos_json, estado
            FROM documentos
            ORDER BY id DESC
        """).fetchall()

        documentos = []

        for fila in filas:
            ruta_pdf = fila["ruta_pdf"]

            # Recuperar el PDF original si existe
            contenido_pdf = None

            if ruta_pdf and os.path.exists(ruta_pdf):
                with open(ruta_pdf, "rb") as archivo:
                    contenido_pdf = archivo.read()

            documentos.append({
                "nombre": fila["nombre"],
                "texto": fila["texto"] or "",
                "datos": json.loads(fila["datos_json"])
                if fila["datos_json"]
                else None,
                "estado": fila["estado"],
                "ruta_pdf": ruta_pdf,
                "pdf": contenido_pdf
            })

        return documentos

    finally:
        conexion.close()

def verificar_credenciales(usuario, contrasena):
    try:
        configuracion = st.secrets["auth"]

        usuario_correcto = configuracion["username"]
        hash_guardado = configuracion["password_hash"]
        sal = configuracion["salt"]

        hash_ingresado = hashlib.pbkdf2_hmac(
            "sha256",
            contrasena.encode("utf-8"),
            sal.encode("utf-8"),
            300_000
        ).hex()

        return (
            hmac.compare_digest(usuario, usuario_correcto)
            and hmac.compare_digest(hash_ingresado, hash_guardado)
        )

    except Exception:
        return False


def mostrar_inicio_sesion():
    if st.session_state.get("autenticado", False):
        return True

    st.title("🔐 Inicio de sesión")
    st.write("Ingresá tus credenciales para acceder al sistema.")

    with st.form("formulario_login"):
        usuario = st.text_input("Usuario")
        contrasena = st.text_input("Contraseña", type="password")
        ingresar = st.form_submit_button("Ingresar")

    if ingresar:
        if verificar_credenciales(usuario.strip(), contrasena):
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    return False

# ==================================================
# CONFIGURACIÓN DE EXTRACCIÓN DE TEXTO
# ==================================================

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

RUTA_POPPLER = (
    r"C:\Herramientas\Release-26.09.0-0"
    r"\poppler-26.09.0\Library\bin"
)

# ==================================================
# FUNCIÓN PARA EXTRAER TEXTO DE DOCUMENTOS PDF
# ==================================================

def extraer_texto_pdf(archivo_pdf):

    """
    Extrae texto de un PDF.
    Si el PDF no contiene texto digital suficiente,
    intenta reconocerlo mediante OCR.
    """

    texto_completo = ""

    try:

        # Leer directamente el PDF
        lector = PdfReader(archivo_pdf)

        for numero_pagina, pagina in enumerate(lector.pages, start=1):

            texto_pagina = pagina.extract_text() or ""

            texto_completo += (
                f"\n--- Página {numero_pagina} ---\n"
                f"{texto_pagina}\n"
            )

        # Si no se encontró texto digital suficiente,
        # intentar reconocer el documento mediante OCR.
        if len(texto_completo.strip()) < 50:

            archivo_pdf.seek(0)

            imagenes = convert_from_path(
                archivo_pdf,
                poppler_path=RUTA_POPPLER
            )

            texto_completo = ""

            for numero_pagina, imagen in enumerate(imagenes, start=1):

                texto_pagina = pytesseract.image_to_string(
                    imagen,
                    lang="spa"
                )

                texto_completo += (
                    f"\n--- Página {numero_pagina} ---\n"
                    f"{texto_pagina}\n"
                )

        return texto_completo.strip()

    except Exception as error:

        raise RuntimeError(
            f"No se pudo leer el PDF: {error}"
        ) from error

# ==============================================
# EXTRACCIÓN DE DATOS MEDIANTE PYTHON
# Funciones recuperadas del notebook de Colab
# ==============================================

def obtener_fecha(texto):
    """Busca la fecha del documento."""

    texto = texto.upper()

    # Primero busca la fecha después de SALA DE SESIONES.
    sala = re.search(
        r'SALA\s+DE\s+SESIONES.*?(\d{1,2}\s+DE\s+\w+\s+DE\s+\d{4})',
        texto,
        re.DOTALL
    )

    if sala:
        return sala.group(1)

    # Si no aparece, busca la primera fecha encontrada.
    fecha = re.search(
        r'(\d{1,2}\s+DE\s+\w+\s+DE\s+\d{4})',
        texto
    )

    if fecha:
        return fecha.group(1)

    return ""


def obtener_numero(texto):
    """Busca el número del documento."""

    texto = texto.upper()

    patrones = [
        r'ORDENANZA\s*N\S*\s*(\d+/\d+)',
        r'DECRETO\s*N\S*\s*(\d+/\d+)',
        r'RESOLUCIÓN\s*N\S*\s*(\d+/\d+)',
        r'RESOLUCION\s*N\S*\s*(\d+/\d+)',
        r'N\S*\s*(\d+/\d+)'
    ]

    for patron in patrones:
        resultado = re.search(patron, texto)

        if resultado:
            return resultado.group(1)

    return ""


def obtener_tipo(texto):
    """Identifica el tipo de documento."""

    texto = texto.upper()

    if "DECRETO ACUERDO" in texto:
        return "Decreto Acuerdo"

    if "DECRETO" in texto:
        return "Decreto"

    if "ORDENANZA" in texto:
        return "Ordenanza"

    if "RESOLUCION" in texto:
        return "Resolución"

    if "PROYECTO" in texto:
        return "Proyecto"

    if "ACTA" in texto:
        return "Acta"

    if "DECLARACION" in texto:
        return "Declaración"

    return "Otro"

def analizar_con_gemini(texto):

    # Verificar que exista la clave de Gemini
    if not GEMINI_API_KEY:
        st.error(
            "No se encontró la clave de Gemini. "
            "Revisá el archivo .env"
        )
        return None

    # Crear el cliente de Gemini
    cliente = genai.Client(api_key=GEMINI_API_KEY)

    # Preparar las instrucciones para analizar el documento
    prompt = f"""
    Analizá el siguiente documento del Concejo Deliberante.

    Extraé únicamente estos datos:
    - tema
    - categoria_general
    - descripcion
    - area_comision

    Devolvé únicamente un JSON válido con esas cuatro claves.
    Si un dato no aparece en el documento, utilizá null.
    No inventes información.

    TEXTO DEL DOCUMENTO:
    {texto[:30000]}
    """

    # Intentar analizar el documento hasta 3 veces
    for intento in range(3):

        try:

            respuesta = cliente.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
                config={
                    "response_mime_type": "application/json"
                }
            )

            # Convertir la respuesta de Gemini a un diccionario
            datos = json.loads(respuesta.text)

            return datos

        except Exception as error:

            mensaje_error = str(error)

            # Identificar errores que podrían ser temporales
            error_temporal = any(
                codigo in mensaje_error
                for codigo in ["503", "429", "500", "502", "504"]
            )

            # Si el error no parece temporal, no volver a intentar
            if not error_temporal:
                st.error(
                    f"Error al analizar el documento con Gemini: {error}"
                )
                return None

            # Si todavía quedan intentos, esperar antes de repetir
            if intento < 2:

                import time

                espera = 10 * (intento + 1)

                st.warning(
                    f"Gemini está temporalmente ocupado. "
                    f"Reintentando en {espera} segundos..."
                )

                time.sleep(espera)

            else:

                st.error(
                    "No se pudo analizar el documento después "
                    "de 3 intentos. Podés volver a intentarlo "
                    "más tarde."
                )

    return None

# ==================================================
# 1. CONFIGURACIÓN GENERAL
# ==================================================

st.set_page_config(
    page_title="Honorable Concejo Deliberante",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================================================
# INICIO DE SESIÓN
# ==================================================

if not mostrar_inicio_sesion():
    st.stop()

# Cargar los documentos guardados en SQLite
st.session_state["documentos_procesados"] = (
    cargar_documentos_desde_base()
)

# ==================================================
# 2. DISEÑO VISUAL: GLASSMORPHISM AZUL Y CELESTE
# ==================================================

st.markdown("""
<style>

/* ---------- Fondo general ---------- */

.stApp {
    background:
        radial-gradient(
            circle at 10% 10%,
            rgba(116, 190, 255, 0.22),
            transparent 35%
        ),
        linear-gradient(
            135deg,
            #edf6ff 0%,
            #e5efff 50%,
            #f5f9ff 100%
        );
    color: #17365D;
}

/* ---------- Área principal ---------- */

.block-container {
    padding-top: 2.5rem;
    padding-bottom: 3rem;
    max-width: 1400px;
}

/* ---------- Textos ---------- */

h1, h2, h3, h4 {
    color: #17365D !important;
}

p, label, .stMarkdown {
    color: #334e68;
}

/* ---------- Menú lateral ---------- */

[data-testid="stSidebar"] {
    background: linear-gradient(
        180deg,
        #12345a 0%,
        #174a78 55%,
        #17628a 100%
    );
    border-right: 1px solid rgba(255, 255, 255, 0.15);
}

[data-testid="stSidebar"] * {
    color: #ffffff;
}

/* Texto secundario del menú */

[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: #d5eaff !important;
}

/* ---------- Separadores ---------- */

hr {
    border-color: rgba(255, 255, 255, 0.22);
}

/* ---------- Tarjetas de vidrio ---------- */

.tarjeta {
    background: rgba(255, 255, 255, 0.62);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);

    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 18px;

    padding: 24px;
    margin-bottom: 18px;

    box-shadow: 0 8px 28px rgba(35, 91, 145, 0.09);
}

/* ---------- Tarjetas principales ---------- */

.tarjeta h3 {
    color: #17365D !important;
    margin-top: 0;
    font-size: 1.15rem;
}

.tarjeta h2 {
    color: #1479c9 !important;
    font-size: 2.2rem;
    margin-bottom: 8px;
}

.tarjeta p {
    color: #52677d !important;
    line-height: 1.6;
}

/* ---------- Encabezado de bienvenida ---------- */

.encabezado {
    background: linear-gradient(
        120deg,
        rgba(255, 255, 255, 0.82),
        rgba(210, 235, 255, 0.56)
    );

    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);

    border: 1px solid rgba(255, 255, 255, 0.9);
    border-radius: 22px;

    padding: 30px;
    margin-bottom: 26px;

    box-shadow: 0 10px 35px rgba(30, 87, 145, 0.08);
}

.encabezado h1 {
    margin-top: 0;
    margin-bottom: 12px;
    font-size: 2.2rem;
}

.encabezado p {
    color: #496783 !important;
    font-size: 1.05rem;
    line-height: 1.7;
}

/* ---------- Botones ---------- */

.stButton > button {
    background: linear-gradient(
        135deg,
        #1686d9,
        #35b6e8
    );

    color: white !important;
    border: none;
    border-radius: 10px;
    padding: 10px 20px;
    font-weight: 600;

    transition: all 0.2s ease;
}

.stButton > button:hover {
    background: linear-gradient(
        135deg,
        #0872bd,
        #159bd2
    );
    box-shadow: 0 5px 16px rgba(22, 134, 217, 0.25);
}

/* ---------- Selector del menú ---------- */

[data-testid="stSidebar"] [role="radiogroup"] {
    gap: 8px;
}

/* ---------- Mensajes informativos ---------- */

[data-testid="stAlert"] {
    border-radius: 12px;
}

/* ---------- Ajustes para pantallas pequeñas ---------- */

@media (max-width: 768px) {
    .block-container {
        padding-top: 1.5rem;
    }

    .encabezado {
        padding: 22px;
    }

    .encabezado h1 {
        font-size: 1.7rem;
    }

    .tarjeta {
        padding: 18px;
    }
}

</style>
""", unsafe_allow_html=True)


# ==================================================
# 3. MENÚ LATERAL
# ==================================================

with st.sidebar:

    st.markdown(
        "<h2 style='color:white;'>🏛️ Concejo</h2>",
        unsafe_allow_html=True
    )

    st.markdown(
        "<p style='color:#d5eaff;'>"
        "Sistema de gestión documental"
        "</p>",
        unsafe_allow_html=True
    )

    st.divider()

    st.markdown("### NAVEGACIÓN")

    # Opciones disponibles en el menú lateral
    opciones_menu = [
        "🏠 Inicio",
        "📄 Documentos",
        "🤖 Procesar documentos",
        "📊 Estadísticas",
        "❓ Ayuda"
    ]

    # Sección seleccionada por defecto
    if "seccion_destino" not in st.session_state:
        st.session_state["seccion_destino"] = "🏠 Inicio"

    # Menú lateral conectado con los botones de acceso rápido
    seccion = st.radio(
        "Seleccionar sección",
        opciones_menu,
        key="seccion_destino",
        label_visibility="collapsed"
    )

    st.divider()

    st.caption("Sistema de análisis documental con IA")


# ==================================================
# 4. PÁGINA INICIO
# ==================================================

if seccion == "🏠 Inicio":

    # ==============================================
    # ENCABEZADO DE BIENVENIDA
    # ==============================================

    st.markdown("""
        <div class="encabezado">
            <h1>Bienvenido al Sistema de Gestión Documental del Concejo</h1>
            <p>Honorable Concejo Deliberante de Tintina</p>
        </div>
    """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ==============================================
    # INFORMACIÓN SOBRE EL CONCEJO
    # ==============================================

    col_foto, col_info = st.columns([1.1, 1])

    with col_foto:

        st.markdown("""
            <div class="tarjeta">
                <h3>🏛️ Nuestra institución</h3>
                <p>
                    Honorable Concejo Deliberante de Tintina
                </p>
                <p>
                    Tintina, provincia de Santiago del Estero,
                    Argentina.
                </p>
            </div>
        """, unsafe_allow_html=True)

        # Más adelante podemos incorporar aquí
        # una fotografía real del edificio.

    with col_info:

        st.markdown("""
            <div class="tarjeta">
                <h3>Sobre el Concejo Deliberante</h3>
                <p>
                    El Honorable Concejo Deliberante de Tintina
                    es la institución legislativa municipal a la
                    que está orientado este proyecto.
                </p>
                <p>
                    Este sistema busca contribuir a la organización
                    de sus documentos, facilitando su clasificación,
                    consulta y búsqueda mediante herramientas
                    de Inteligencia Artificial.
                </p>
                <p>
                    <b>Democracia, participación y compromiso
                    con la comunidad.</b>
                </p>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ==============================================
    # PRESENTACIÓN DEL SISTEMA
    # ==============================================

    st.markdown("## ¿Qué es este sistema?")

    col_sistema, col_objetivo = st.columns([1.2, 1])

    with col_sistema:

        st.markdown("""
            <div class="tarjeta">
                <h3>Gestión documental inteligente</h3>
                <p>
                    Este sistema permite organizar y consultar
                    los documentos institucionales del Concejo.
                </p>
                <p>
                    Mediante el procesamiento de archivos PDF
                    y el uso de Inteligencia Artificial, busca
                    extraer información relevante y clasificar
                    los documentos para facilitar su búsqueda.
                </p>
            </div>
        """, unsafe_allow_html=True)

    with col_objetivo:

        st.markdown("""
            <div class="tarjeta">
                <h3>🎯 Nuestro fin</h3>
                <p>
                    Mejorar la organización documental, agilizar
                    las tareas administrativas y facilitar la
                    consulta de la información registrada.
                </p>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ==============================================
    # MENSAJE INSTITUCIONAL
    # ==============================================

    st.markdown("""
        <div class="tarjeta">
            <h3>🏛️ Nuestro compromiso</h3>
            <p>
                Trabajamos para incorporar herramientas
                tecnológicas que contribuyan a una gestión
                documental más organizada, eficiente y accesible.
            </p>
            <p>
                <b>
                    Transparencia, compromiso y trabajo
                    al servicio de nuestra comunidad.
                </b>
            </p>
        </div>
    """, unsafe_allow_html=True)

# ==================================================
# 5. RESTO DE LAS SECCIONES
# ==================================================

elif seccion == "📄 Documentos":

    # ==================================================
    # REPOSITORIO DE DOCUMENTOS PROCESADOS
    # ==================================================

    import pandas as pd

    st.title("📄 Documentos")

    st.markdown("""
        <div class="tarjeta">
            <h3>📚 Repositorio documental</h3>
            <p>
                En esta sección podés consultar los documentos
                procesados, revisar la información extraída con
                Inteligencia Artificial y descargar los archivos PDF.
            </p>
        </div>
    """, unsafe_allow_html=True)

    # Recuperar los documentos procesados
    documentos = st.session_state.get(
        "documentos_procesados", []
    )

    # Evitar mostrar documentos repetidos por nombre
    documentos_unicos = {}

    for documento in documentos:
        documentos_unicos[documento["nombre"]] = documento

    lista_documentos = list(documentos_unicos.values())

    # ==================================================
    # RESUMEN GENERAL
    # ==================================================

    st.subheader("📊 Resumen del repositorio")

    total_documentos = len(lista_documentos)

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(
            "📄 Documentos registrados",
            total_documentos
        )

    with col2:
        documentos_analizados = sum(
            1 for documento in lista_documentos
            if documento.get("datos")
        )

        st.metric(
            "🤖 Analizados con Gemini",
            documentos_analizados
        )

    with col3:
        documentos_con_pdf = sum(
            1 for documento in lista_documentos
            if documento.get("pdf")
        )

        st.metric(
            "📥 PDF disponibles",
            documentos_con_pdf
        )

    st.markdown("---")

    # ==================================================
    # LISTADO DE DOCUMENTOS
    # ==================================================

    st.subheader("📑 Documentos registrados")

    if not lista_documentos:

        st.info(
            "Todavía no hay documentos registrados. "
            "Ingresá en 'Procesar documentos' para comenzar."
        )

    else:

        # Preparar la información para la tabla
        registros = []

        for documento in lista_documentos:

            datos = documento.get("datos") or {}

            registros.append({
                "Nombre del archivo": documento.get(
                    "nombre", "Sin nombre"
                ),
                "Fecha": datos.get(
                    "fecha"
                ) or "No identificada",
                "Año": datos.get(
                    "anio"
                ) or "No identificado",
                "Tipo": datos.get(
                    "tipo_documento"
                ) or "No identificado",
                "Número": datos.get(
                    "numero_documento"
                ) or "No identificado",
                "Categoría": datos.get(
                    "categoria_general"
                ) or "No identificada",
                "Tema": datos.get(
                    "tema"
                ) or "No identificado"
            })

        df_documentos = pd.DataFrame(registros)

        # Campo de búsqueda
        busqueda_documento = st.text_input(
            "🔎 Buscar en el repositorio",
            placeholder=(
                "Escribí un nombre, tema, categoría o tipo..."
            ),
            key="busqueda_repositorio"
        )

        # Filtrar los documentos
        if busqueda_documento.strip():

            termino = busqueda_documento.strip().casefold()

            mascara = df_documentos.apply(
                lambda fila: fila.astype(str)
                .str.casefold()
                .str.contains(termino, regex=False)
                .any(),
                axis=1
            )

            df_mostrado = df_documentos[mascara]

        else:
            df_mostrado = df_documentos

        st.caption(
            f"Documentos encontrados: {len(df_mostrado)}"
        )

        st.dataframe(
            df_mostrado,
            use_container_width=True,
            hide_index=True
        )

        # ==================================================
        # CONSULTAR Y DESCARGAR UN DOCUMENTO
        # ==================================================

        st.markdown("---")

        st.subheader("📥 Consultar un documento")

        nombres_documentos = [
            documento["nombre"]
            for documento in lista_documentos
        ]

        nombre_seleccionado = st.selectbox(
            "Seleccioná el documento que querés consultar:",
            nombres_documentos,
            key="documento_repositorio"
        )

        documento_seleccionado = next(
            documento
            for documento in lista_documentos
            if documento["nombre"] == nombre_seleccionado
        )

        datos = documento_seleccionado.get("datos") or {}

        col1, col2 = st.columns(2)

        with col1:

            st.write(
                "**📅 Fecha:**",
                datos.get("fecha") or "No identificada"
            )

            st.write(
                "**📆 Año:**",
                datos.get("anio") or "No identificado"
            )

            st.write(
                "**🏛️ Tipo de documento:**",
                datos.get("tipo_documento") or "No identificado"
            )

            st.write(
                "**🔢 Número:**",
                datos.get("numero_documento") or "No identificado"
            )

        with col2:

            st.write(
                "**🏷️ Categoría:**",
                datos.get("categoria_general") or "No identificada"
            )

            st.write(
                "**📝 Tema:**",
                datos.get("tema") or "No identificado"
            )

            st.write(
                "**🏢 Área o comisión:**",
                datos.get("area_comision") or "No identificada"
            )

        descripcion = datos.get("descripcion")

        if descripcion:
            st.markdown("**Descripción:**")
            st.write(descripcion)

        # ==================================================
        # VISUALIZAR Y DESCARGAR EL DOCUMENTO
        # ==================================================

        pdf_bytes = documento_seleccionado.get("pdf")

        st.markdown("---")
        st.subheader("📄 Visualización del documento")

        if pdf_bytes:

            # Mostrar el PDF dentro de la aplicación
            st.pdf(pdf_bytes, height=750)

            # Botón para descargar el archivo original
            st.download_button(
                label="⬇️ Descargar documento PDF",
                data=pdf_bytes,
                file_name=nombre_seleccionado,
                mime="application/pdf",
                key="descargar_repositorio"
            )

        else:

            st.warning(
                "No hay un archivo PDF disponible para visualizar."
            )

elif seccion == "🤖 Procesar documentos":

    # ==============================================
    # PROCESAMIENTO DE DOCUMENTOS PDF
    # ==============================================

    st.title("🤖 Procesar documentos")

    st.markdown("""
        <div class="tarjeta">
            <h3>Gestión documental inteligente</h3>
            <p>
                Seleccioná los documentos PDF que querés incorporar
                al sistema y presioná el botón para iniciar
                el procesamiento automático.
            </p>
        </div>
    """, unsafe_allow_html=True)

    st.subheader("📂 Seleccionar documentos")

    archivos = st.file_uploader(
        "Seleccioná uno o varios archivos PDF",
        type=["pdf"],
        accept_multiple_files=True,
        key="carga_documentos_pdf"
    )

    if archivos:

        st.success(
            f"Documentos seleccionados: {len(archivos)}"
        )

        for archivo in archivos:
            st.write(f"📄 {archivo.name}")

        st.markdown("---")

        # ==========================================
        # BOTÓN PRINCIPAL
        # ==========================================

        if st.button(
            "🚀 Procesar documentos",
            use_container_width=True,
            key="boton_procesar_documentos"
        ):

            resultados = []
            errores = []

            barra_progreso = st.progress(0)

            mensaje_progreso = st.empty()

            total = len(archivos)

            for indice, archivo in enumerate(archivos):

                mensaje_progreso.info(
                    f"Procesando documento "
                    f"{indice + 1} de {total}..."
                )
            
                    # Extraer el texto del PDF
                texto = extraer_texto_pdf(archivo)

                if not texto or not texto.strip():

                        errores.append({
                            "nombre": archivo.name,
                            "motivo": "No se pudo extraer texto del documento."
                        })

                else:

                       # Extraer los datos directamente del texto
                        fecha = obtener_fecha(texto)

                        anio = fecha[-4:] if fecha else None

                        tipo_documento = obtener_tipo(texto)

                        numero_documento = obtener_numero(texto)

                        # Analizar con Gemini los datos que requieren IA
                        datos_ia = analizar_con_gemini(texto) or {}

                        # Unir los datos extraídos por Python y Gemini
                        datos = {
                            "fecha": fecha or None,
                            "anio": anio,
                            "tipo_documento": tipo_documento,
                            "numero_documento": numero_documento or None,
                            "tema": datos_ia.get("tema"),
                            "categoria_general": (
                                datos_ia.get("categoria_general")
                                or datos_ia.get("categoria")
                            ),
                            "descripcion": (
                                datos_ia.get("descripcion")
                                or datos_ia.get("resumen")
                            ),
                            "area_comision": (
                                datos_ia.get("area_comision")
                                or datos_ia.get("area")
                            )
                        }

                        # Guardar el documento aunque Gemini falle
                        resultados.append({
                            "nombre": archivo.name,
                            "texto": texto,
                            "estado": (
                                "Procesado"
                                if datos_ia
                                else "Pendiente de análisis"
                            ),
                            "pdf": archivo.getvalue(),
                            "datos": datos
                        })

                        # Guardar el documento en SQLite
                        guardar_documento(resultados[-1])

                        # ==========================================
                        # RESUMEN FINAL
                        # ==========================================

                        documentos_analizados = sum(
                            1 for documento in resultados
                            if documento.get("datos")
                        )

                        documentos_pendientes = sum(
                            1 for documento in resultados
                            if not documento.get("datos")
                        )

                        if documentos_analizados == len(resultados) and not errores:

                            st.success(
                                "¡Todos los documentos se procesaron y analizaron correctamente!"
                            )

                        elif documentos_analizados > 0:

                            st.warning(
                                "El procesamiento finalizó, pero algunos documentos "
                                "no pudieron analizarse con Gemini."
                            )

                        elif resultados:

                            st.warning(
                                "Se extrajo el texto de los documentos, pero Gemini "
                                "no pudo completar el análisis."
                            )

                        if errores:

                          st.error(
                              f"No se pudo extraer texto de {len(errores)} documento(s)."
                          )

                          for error in errores:

                              st.write(
                                  f"📄 {error['nombre']}: {error['motivo']}"
                              )

                        if resultados:

                            st.subheader("📊 Resumen del procesamiento")

                            col1, col2, col3 = st.columns(3)

                            with col1:

                                st.metric(
                                    "📄 Texto extraído",
                                    len(resultados)
                                )

                            with col2:

                                st.metric(
                                    "🤖 Analizados con Gemini",
                                    documentos_analizados
                                )

                            with col3:

                                st.metric(
                                    "⏳ Pendientes de análisis",
                                    documentos_pendientes + len(errores)
                                )

                            st.info(
                                "Los documentos procesados se guardaron en la sesión actual. "
                                "Los que quedaron pendientes pueden volver a analizarse "
                                "en una etapa posterior."
                            )

                        else:

                            st.info(
                                   "Seleccioná uno o varios archivos PDF "
                                   "para comenzar."
                            )

elif seccion == "📊 Estadísticas":

    # ==================================================
    # DASHBOARD DE GESTIÓN DOCUMENTAL
    # ==================================================

    import pandas as pd

    import plotly.express as px

    st.title("📊 Dashboard de Gestión Documental")

    st.markdown("""
        <div class="tarjeta">
            <h3>🏛️ Honorable Concejo Deliberante de Tintina</h3>
            <p>
                Panel de control para consultar la actividad documental,
                los tipos de documentos, las categorías y el estado
                de los análisis realizados mediante inteligencia artificial.
            </p>
        </div>
    """, unsafe_allow_html=True)

    # ==================================================
    # OBTENER LOS DOCUMENTOS REGISTRADOS
    # ==================================================

    documentos = st.session_state.get(
        "documentos_procesados", []
    )

    # Evitar contar dos veces documentos con el mismo nombre.
    documentos_unicos = {}

    for documento in documentos:
        documentos_unicos[documento.get("nombre", "Sin nombre")] = documento

    documentos = list(documentos_unicos.values())

    total_documentos = len(documentos)

    documentos_analizados = sum(
        1 for documento in documentos
        if isinstance(documento.get("datos"), dict)
        and documento.get("datos")
    )

    documentos_pendientes = sum(
        1 for documento in documentos
        if not (
            isinstance(documento.get("datos"), dict)
            and documento.get("datos")
        )
    )

    # ==================================================
    # PREPARAR LOS DATOS EXTRAÍDOS POR GEMINI
    # ==================================================

    registros = []

    for documento in documentos:

        datos = documento.get("datos") or {}

        if not isinstance(datos, dict):
            datos = {}

        registros.append({
            "nombre": documento.get("nombre", "Sin nombre"),
            "fecha": datos.get("fecha"),
            "anio": datos.get("anio"),
            "tipo_documento": datos.get("tipo_documento"),
            "numero_documento": datos.get("numero_documento"),
            "tema": datos.get("tema"),
            "categoria_general": datos.get("categoria_general"),
            "descripcion": datos.get("descripcion"),
            "area_comision": datos.get("area_comision"),
            "estado": (
                "Analizado"
                if datos
                else "Pendiente de análisis"
            )
        })

    df = pd.DataFrame(registros)

    # ==================================================
    # NORMALIZAR LOS CAMPOS PARA LOS GRÁFICOS
    # ==================================================

    campos_categoricos = [
        "tipo_documento",
        "categoria_general",
        "area_comision",
        "tema"
    ]

    for campo in campos_categoricos:

        if campo not in df.columns:
            df[campo] = None

        df[campo] = df[campo].apply(
            lambda valor: (
                str(valor).strip()
                if pd.notna(valor)
                and str(valor).strip()
                and str(valor).strip().lower() not in [
                    "none", "null", "nan", "no identificado"
                ]
                else "Sin clasificar"
            )
        )

    if "anio" not in df.columns:
        df["anio"] = None

    df["anio"] = pd.to_numeric(
        df["anio"],
        errors="coerce"
    )

    # ==================================================
    # TÍTULO E INDICADORES GENERALES
    # ==================================================

    st.markdown("---")

    st.subheader("📌 Indicadores generales")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "📄 Total de documentos",
            total_documentos
        )

    with col2:
        st.metric(
            "🤖 Analizados con IA",
            documentos_analizados
        )

    with col3:
        categorias_identificadas = (
            df.loc[
                (df["categoria_general"] != "Sin clasificar")
                & (df["estado"] == "Analizado"),
                "categoria_general"
            ].nunique()
            if not df.empty
            else 0
        )

        st.metric(
            "🏷️ Categorías identificadas",
            categorias_identificadas
        )

    with col4:
        st.metric(
            "⏳ Pendientes de análisis",
            documentos_pendientes
        )

    if total_documentos == 0:

        st.info(
            "Todavía no hay documentos procesados para generar "
            "estadísticas. Entrá en '🤖 Procesar documentos', "
            "cargá algunos PDF y volvé a esta sección."
        )

    else:

        # ==================================================
        # FILTROS
        # ==================================================

        st.markdown("---")

        st.subheader("🔎 Filtros")

        col_filtro1, col_filtro2 = st.columns(2)

        with col_filtro1:

            anios_disponibles = sorted(
                df["anio"].dropna().astype(int).unique().tolist()
            )

            opciones_anio = ["Todos los años"] + [
                str(anio) for anio in anios_disponibles
            ]

            anio_seleccionado = st.selectbox(
                "Filtrar por año",
                opciones_anio,
                key="estadisticas_filtro_anio"
            )

        with col_filtro2:

            categorias_disponibles = sorted(
                df.loc[
                    df["categoria_general"] != "Sin clasificar",
                    "categoria_general"
                ].unique().tolist()
            )

            opciones_categoria = (
                ["Todas las categorías"]
                + categorias_disponibles
            )

            categoria_seleccionada = st.selectbox(
                "Filtrar por categoría",
                opciones_categoria,
                key="estadisticas_filtro_categoria"
            )

        df_filtrado = df.copy()

        if anio_seleccionado != "Todos los años":

            df_filtrado = df_filtrado[
                df_filtrado["anio"]
                == int(anio_seleccionado)
            ]

        if categoria_seleccionada != "Todas las categorías":

            df_filtrado = df_filtrado[
                df_filtrado["categoria_general"]
                == categoria_seleccionada
            ]

        st.caption(
            f"Documentos que coinciden con los filtros: "
            f"{len(df_filtrado)}"
        )

        # ==================================================
        # GRÁFICO 1: DOCUMENTOS POR TIPO
        # ==================================================

        st.markdown("---")

        st.subheader("📑 Cantidad de documentos por tipo")

        df_tipos = df_filtrado[
            (df_filtrado["estado"] == "Analizado")
            & (df_filtrado["tipo_documento"] != "Sin clasificar")
        ]

        if not df_tipos.empty:

            conteo_tipos = (
                df_tipos["tipo_documento"]
                .value_counts()
                .rename_axis("Tipo de documento")
                .reset_index(name="Cantidad")
            )

            fig_tipos = px.bar(
                conteo_tipos,
                x="Tipo de documento",
                y="Cantidad",
                color="Tipo de documento",
                text="Cantidad",
                title="Documentos registrados según su tipo"
            )

            fig_tipos.update_layout(
                showlegend=False,
                xaxis_title="Tipo de documento",
                yaxis_title="Cantidad",
                template="plotly_white"
            )

            st.plotly_chart(
                fig_tipos,
                use_container_width=True
            )

        else:

            st.info(
                "Todavía no hay tipos de documentos identificados "
                "para los filtros seleccionados."
            )

        # ==================================================
        # GRÁFICO 2: ACTIVIDAD DOCUMENTAL POR AÑO
        # ==================================================

        st.markdown("---")

        st.subheader("📅 Actividad documental por año")

        df_anios = df_filtrado[
            (df_filtrado["estado"] == "Analizado")
            & (df_filtrado["anio"].notna())
        ].copy()

        if not df_anios.empty:

            df_anios["anio"] = df_anios["anio"].astype(int)

            conteo_anios = (
                df_anios.groupby("anio")
                .size()
                .reset_index(name="Cantidad")
                .sort_values("anio")
            )

            fig_anios = px.line(
                conteo_anios,
                x="anio",
                y="Cantidad",
                markers=True,
                text="Cantidad",
                title="Cantidad de documentos por año"
            )

            fig_anios.update_traces(
                line=dict(width=3),
                marker=dict(size=10),
                textposition="top center"
            )

            fig_anios.update_layout(
                xaxis_title="Año",
                yaxis_title="Cantidad de documentos",
                template="plotly_white"
            )

            st.plotly_chart(
                fig_anios,
                use_container_width=True
            )

        else:

            st.info(
                "No hay años identificados en los documentos "
                "analizados para los filtros seleccionados."
            )

        # ==================================================
        # GRÁFICO 3: ESTADO DEL PROCESAMIENTO
        # ==================================================

        st.markdown("---")

        st.subheader("⚙️ Estado de los documentos")

        conteo_estados = (
            df_filtrado["estado"]
            .value_counts()
            .rename_axis("Estado")
            .reset_index(name="Cantidad")
        )

        if not conteo_estados.empty:

            fig_estados = px.pie(
                conteo_estados,
                names="Estado",
                values="Cantidad",
                hole=0.35,
                title="Estado del análisis documental"
            )

            fig_estados.update_traces(
                textposition="inside",
                textinfo="label+value+percent"
            )

            fig_estados.update_layout(
                template="plotly_white"
            )

            st.plotly_chart(
                fig_estados,
                use_container_width=True
            )

        # ==================================================
        # GRÁFICO 4: CATEGORÍAS MÁS FRECUENTES
        # ==================================================

        st.markdown("---")

        st.subheader("🏷️ Categorías generales más frecuentes")

        df_categorias = df_filtrado[
            (df_filtrado["estado"] == "Analizado")
            & (df_filtrado["categoria_general"] != "Sin clasificar")
        ]

        if not df_categorias.empty:

            conteo_categorias = (
                df_categorias["categoria_general"]
                .value_counts()
                .head(10)
                .rename_axis("Categoría")
                .reset_index(name="Cantidad")
            )

            fig_categorias = px.bar(
                conteo_categorias.sort_values("Cantidad"),
                x="Cantidad",
                y="Categoría",
                orientation="h",
                text="Cantidad",
                title="Categorías más frecuentes"
            )

            fig_categorias.update_layout(
                xaxis_title="Cantidad de documentos",
                yaxis_title="Categoría",
                template="plotly_white"
            )

            st.plotly_chart(
                fig_categorias,
                use_container_width=True
            )

        else:

            st.info(
                "Todavía no hay categorías identificadas "
                "para los filtros seleccionados."
            )

        # ==================================================
        # GRÁFICO 5: DOCUMENTOS POR ÁREA O COMISIÓN
        # ==================================================

        st.markdown("---")

        st.subheader("🏛️ Participación por área o comisión")

        df_areas = df_filtrado[
            (df_filtrado["estado"] == "Analizado")
            & (df_filtrado["area_comision"] != "Sin clasificar")
        ]

        if not df_areas.empty:

            conteo_areas = (
                df_areas["area_comision"]
                .value_counts()
                .head(10)
                .rename_axis("Área o comisión")
                .reset_index(name="Cantidad")
            )

            fig_areas = px.bar(
                conteo_areas.sort_values("Cantidad"),
                x="Cantidad",
                y="Área o comisión",
                orientation="h",
                text="Cantidad",
                title="Documentos por área o comisión"
            )

            fig_areas.update_layout(
                xaxis_title="Cantidad de documentos",
                yaxis_title="Área o comisión",
                template="plotly_white"
            )

            st.plotly_chart(
                fig_areas,
                use_container_width=True
            )

        else:

            st.info(
                "Todavía no hay áreas o comisiones identificadas "
                "para los filtros seleccionados."
            )

        # ==================================================
        # GRÁFICO 6: TEMAS MÁS FRECUENTES
        # ==================================================

        st.markdown("---")

        st.subheader("🔍 Temas más frecuentes")

        df_temas = df_filtrado[
            (df_filtrado["estado"] == "Analizado")
            & (df_filtrado["tema"] != "Sin clasificar")
        ]

        if not df_temas.empty:

            conteo_temas = (
                df_temas["tema"]
                .value_counts()
                .head(10)
                .rename_axis("Tema")
                .reset_index(name="Cantidad")
            )

            fig_temas = px.bar(
                conteo_temas.sort_values("Cantidad"),
                x="Cantidad",
                y="Tema",
                orientation="h",
                text="Cantidad",
                title="Temas más frecuentes en los documentos"
            )

            fig_temas.update_layout(
                xaxis_title="Cantidad de documentos",
                yaxis_title="Tema",
                template="plotly_white"
            )

            st.plotly_chart(
                fig_temas,
                use_container_width=True
            )

        else:

            st.info(
                "Todavía no hay temas identificados "
                "para los filtros seleccionados."
            )

        # ==================================================
        # TABLA DE DOCUMENTOS
        # ==================================================

        st.markdown("---")

        st.subheader("📋 Datos utilizados en las estadísticas")

        columnas_tabla = [
            "nombre",
            "fecha",
            "anio",
            "tipo_documento",
            "numero_documento",
            "tema",
            "categoria_general",
            "area_comision",
            "estado"
        ]

        df_tabla = df_filtrado[columnas_tabla].copy()

        df_tabla = df_tabla.rename(columns={
            "nombre": "Archivo",
            "fecha": "Fecha",
            "anio": "Año",
            "tipo_documento": "Tipo de documento",
            "numero_documento": "Número",
            "tema": "Tema",
            "categoria_general": "Categoría",
            "area_comision": "Área o comisión",
            "estado": "Estado"
        })

        st.dataframe(
            df_tabla,
            use_container_width=True,
            hide_index=True
        )

        # ==================================================
        # DESCARGAR DATOS
        # ==================================================

        csv = df_tabla.to_csv(
            index=False
        ).encode("utf-8-sig")

        st.download_button(
            label="⬇️ Descargar estadísticas en CSV",
            data=csv,
            file_name="estadisticas_documentales_tintina.csv",
            mime="text/csv",
            key="descargar_estadisticas_csv"
        )

elif seccion == "❓ Ayuda":

    # ==================================================
    # PÁGINA DE AYUDA Y SOPORTE
    # ==================================================

    st.title("❓ Ayuda y soporte")

    st.markdown("""
        <div class="tarjeta">
            <h3>🏛️ Bienvenido al sistema de gestión documental</h3>
            <p>
                Esta sección explica cómo utilizar las principales
                funciones de la plataforma del Honorable Concejo
                Deliberante de Tintina.
            </p>
            <p>
                Encontrarás instrucciones para procesar documentos,
                consultar archivos y comprender los resultados
                del análisis automático.
            </p>
        </div>
    """, unsafe_allow_html=True)

    # ==================================================
    # GUÍA DE USO
    # ==================================================

    st.subheader("📚 Guía de uso")

    with st.expander("📄 1. ¿Cómo cargar documentos PDF?"):

        st.markdown("""
        **Pasos para incorporar documentos al sistema:**

        1. Ingresá en la sección **🤖 Procesar documentos**.
        2. Presioná el botón para seleccionar archivos.
        3. Elegí uno o varios documentos en formato PDF.
        4. Verificá que los archivos seleccionados sean correctos.
        5. Presioná **Procesar documentos**.
        6. Esperá a que finalice el procesamiento.
        7. y listo. podés ingresar a la sección **📄Documentos**

        """)

    with st.expander("🤖 2. ¿Cómo funciona el análisis con inteligencia artificial?"):

        st.markdown("""
        El sistema utiliza Google Gemini para analizar el texto
        extraído de los documentos.

        Cuando el análisis funciona correctamente, puede identificar:

        - Fecha del documento.
        - Año.
        - Tipo de documento.
        - Número del documento.
        - Tema principal.
        - Categoría general.
        - Descripción.
        - Área o comisión relacionada.

        La información que no aparece en el documento puede quedar
        sin identificar.

        **Recomendación:** revisá los resultados antes de utilizarlos
        como información institucional definitiva.
        """)

    with st.expander("📂 3. ¿Cómo consultar los documentos?"):

        st.markdown("""
        Ingresá en **📄 Documentos** para consultar el repositorio
        documental y revisar las opciones disponibles.

        Según las funciones habilitadas, podrás consultar los datos
        extraídos y visualizar o descargar el PDF original.

        Si un documento no aparece, comprobá que se haya procesado
        y que continúe disponible en la sesión actual.
        """)

    with st.expander("📊 4. ¿Cómo interpretar las estadísticas?"):

        st.markdown("""
        La sección **📊 Estadísticas** presenta indicadores y gráficos
        construidos a partir de los documentos procesados.

        Entre otros datos, puede mostrar:

        - Cantidad total de documentos.
        - Documentos analizados con inteligencia artificial.
        - Tipos de documentos identificados.
        - Actividad documental por año.
        - Categorías y áreas o comisiones.
        - Documentos pendientes de análisis.

        Los gráficos dependen de la información disponible.
        Si faltan documentos o no se identificaron determinados
        campos, algunos gráficos pueden aparecer vacíos.
        """)

    # ==================================================
    # PROBLEMAS FRECUENTES
    # ==================================================

    st.markdown("---")

    st.subheader("🛠️ Problemas frecuentes")

    with st.expander("❌ El sistema no puede leer un PDF"):

        st.markdown("""
        **Posibles causas:**

        - El archivo está dañado.
        - El PDF está protegido o tiene restricciones.
        - El documento contiene imágenes difíciles de reconocer.
        - No se pudo completar el reconocimiento de texto.

        **Qué podés hacer:**

        1. Comprobá que el PDF se abra correctamente.
        2. Intentá abrirlo con un lector de PDF.
        3. Si es un documento escaneado, verificá que las páginas
           sean legibles y estén bien orientadas.
        4. Volvé a intentar procesarlo.
        """)

    with st.expander("🤖 Gemini muestra un error o está ocupado"):

        st.markdown("""
        El servicio de inteligencia artificial puede presentar
        errores temporales, límites de uso o problemas de conexión.

        **Qué podés hacer:**

        1. Esperá unos minutos.
        2. Volvé a intentar el análisis.
        3. Verificá que la conexión a internet funcione.

        Si el servicio continúa fallando, el problema puede estar
        en Gemini y no necesariamente en tu aplicación.
        """)

    with st.expander("📊 Los gráficos no muestran información"):

        st.markdown("""
        Los gráficos se generan a partir de los datos disponibles
        en los documentos procesados.

        Si todavía no cargaste documentos o Gemini no pudo analizarlos,
        algunos gráficos no tendrán información para mostrar.

        **Qué podés hacer:**

        - Comprobar si hay documentos procesados.
        - Revisar si los documentos tienen datos analizados.
        - Verificar que los campos necesarios estén identificados.

        No es necesario que todos los gráficos tengan datos para
        que el resto de la aplicación funcione.
        """)

    # ==================================================
    # RECOMENDACIONES DE SEGURIDAD
    # ==================================================

    st.markdown("---")

    st.subheader("🔐 Recomendaciones de seguridad")

    st.markdown("""
        <div class="tarjeta">
            <ul>
                <li>No compartas el usuario, ni contraseña de esta aplicación web.</li>
            </ul>
        </div>
    """, unsafe_allow_html=True)

    # ==================================================
    # INFORMACIÓN FINAL
    # ==================================================

    st.markdown("---")

    st.success(
        "Si encontrás un problema, anotá el mensaje de error "
        "y en qué paso ocurrió para facilitar su diagnóstico."
    )

    st.caption(
        "Sistema de gestión documental — "
        "Honorable Concejo Deliberante de Tintina."
    )

import hashlib
import secrets

usuario = "concejo_admin"
contrasena = secrets.token_urlsafe(12)
salt = secrets.token_hex(16)

password_hash = hashlib.pbkdf2_hmac(
    "sha256",
    contrasena.encode("utf-8"),
    salt.encode("utf-8"),
    300_000
).hex()

print("Usuario:", usuario)
print("Contraseña:", contrasena)
print("Salt:", salt)
print("Password hash:", password_hash)
