# backend original solo archivos de voz con whatsapp (sin web)
from flask import Flask, request, Response, jsonify
from flask_cors import CORS
import vertexai
from vertexai import agent_engines
from dotenv import load_dotenv
import os
import json
import logging
import requests

import magic
import tempfile
from google.cloud import storage

try:
    from google.cloud import speech
    SPEECH_CLIENT_AVAILABLE = True
except ImportError:
    SPEECH_CLIENT_AVAILABLE = False

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=dotenv_path)

# Configurar logging básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log de disponibilidad de Speech-to-Text
if SPEECH_CLIENT_AVAILABLE:
    logger.info("Google Cloud Speech-to-Text está disponible")
else:
    logger.warning("Google Cloud Speech-to-Text no está disponible. Instalar con: pip install google-cloud-speech")

app = Flask(__name__)
# Simplify CORS initialization to allow all origins and methods for testing
CORS(app)

@app.before_request
def log_request_info():
    if request.method == 'POST' and request.path == '/whatsapp/webhook':
        logger.info(f"WhatsApp webhook received from {request.remote_addr}")
    elif request.method == 'POST' and request.path == '/agent':
        logger.info(f"Agent chat request from {request.remote_addr}")
    else:
        logger.info(f"{request.method} {request.path} from {request.remote_addr}")

# --- Configuración del Proyecto ---
PROJECT_ID = os.environ.get("PROJECT_ID")
LOCATION = os.environ.get("LOCATION")
AGENT_ENGINE_RESOURCE_NAME = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET")

# --- Configuración de WhatsApp ---
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")


# Global variable for the agent engine
remote_agent_engine = None
# Caché simple en memoria para las sesiones del agente por número de WhatsApp
session_cache = {}

def extract_text_from_event(event):
    """Extrae texto de un evento del agente de forma unificada."""
    if hasattr(event, 'text') and event.text:
        return event.text
    elif hasattr(event, 'parts') and event.parts and hasattr(event.parts[0], 'text'):
        return event.parts[0].text
    elif hasattr(event, 'content') and event.content and hasattr(event.content, 'parts') and \
         event.content.parts and hasattr(event.content.parts[0], 'text'):
        return event.content.parts[0].text
    elif isinstance(event, dict) and 'content' in event and 'parts' in event.get('content', {}) and event['content']['parts']:
        return event['content']['parts'][0].get('text')
    return None

def init_vertex_ai_and_engine():
    global remote_agent_engine
    if not all([PROJECT_ID, LOCATION, AGENT_ENGINE_RESOURCE_NAME]):
        logger.error("PROJECT_ID, LOCATION, and AGENT_ENGINE_RESOURCE_NAME environment variables are required for Vertex AI setup.")
        remote_agent_engine = None
        return

    try:
        logger.info(f"--- Initializing Vertex AI for project {PROJECT_ID} in {LOCATION} ---")
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        logger.info("Vertex AI initialized.")
        
        logger.info(f"--- Getting Agent Engine: {AGENT_ENGINE_RESOURCE_NAME} ---")
        remote_agent_engine = agent_engines.get(AGENT_ENGINE_RESOURCE_NAME)
        logger.info(f"Agent Engine obtained: {remote_agent_engine.name if remote_agent_engine else 'Not Found'}")
    except Exception as e:
        logger.error(f"Error initializing Vertex AI or getting Agent Engine: {e}", exc_info=True)
        remote_agent_engine = None

# Initialize Vertex AI and Agent Engine when the app starts
# For Gunicorn, this will run once per worker process.
init_vertex_ai_and_engine()

@app.route('/')
def index():
    return "Backend is running.", 200

@app.route('/create-session', methods=['POST']) # Ensure 'OPTIONS' is NOT listed here
def create_session_endpoint():
    # Ensure no explicit OPTIONS handling here
    
    if not remote_agent_engine:
        return jsonify({"error": "Agent Engine not initialized or not found"}), 500
    
    data = request.json
    user_id = data.get('user_id')

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    
    try:
        logger.info(f"Creating session for user_id: {user_id}")
        session_info = remote_agent_engine.create_session(user_id=user_id)
        logger.info(f"Session created: {session_info.get('id')}")
        return jsonify({"session_id": session_info['id']}), 200
    except Exception as e:
        logger.error(f"Error creating session: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/agent', methods=['POST']) 
def chat():
    # Ensure no explicit OPTIONS handling here

    if not remote_agent_engine:
        return jsonify({"error": "Agent Engine not initialized or not found"}), 500

    data = request.json
    user_id = data.get('user_id')
    session_id = data.get('session_id')
    message_text = data.get('message')

    if not user_id or not message_text or not session_id:
        return jsonify({"error": "user_id, session_id, and message are required"}), 400

    logger.info(f"Chat request for user_id: {user_id}, session_id: {session_id}")

    
    def generate_sse():
        try:
            for event in remote_agent_engine.stream_query( # type: ignore
                user_id=user_id,
                session_id=session_id,
                message=message_text,
            ):
                logger.debug(f"Agent event received: {type(event)}")
                text_content = extract_text_from_event(event)
                
                if text_content:
                    logger.info(f"Processing event with text: {text_content}")
                    response_data = {"type": "text_event", "message": {"content": {"parts": [{"text": text_content}]}}}
                else:
                    # Fallback para eventos no-texto
                    logger.warning(f"Unknown event structure or type: {type(event)} - Event: {event}")
                    if isinstance(event, (dict, list)):
                        safe_data_str = json.dumps(event, ensure_ascii=False)
                    else:
                        safe_data_str = str(event)
                    response_data = {"type": "unknown_event", "data_str": safe_data_str}
                
                yield f"data: {json.dumps(response_data)}\n\n"
            logger.info(f"Stream ended for session {session_id}")
        except Exception as e_stream:
            logger.error(f"Error during agent stream: {e_stream}", exc_info=True)
            error_response = {"type": "error", "message": "Error processing your request with the agent."}
            yield f"data: {json.dumps(error_response)}\n\n"
        
    return Response(generate_sse(), mimetype='text/event-stream')


# --- Integración con WhatsApp ---

def upload_to_gcs(local_file_path, bucket_name, destination_blob_name):
    """Sube un archivo a un bucket de GCS."""
    if not bucket_name:
        logger.error("GCS bucket name is not configured.")
        return
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(destination_blob_name)

        blob.upload_from_filename(local_file_path)
        logger.info(f"File {local_file_path} uploaded to gs://{bucket_name}/{destination_blob_name}.")
    except Exception as e:
        logger.error(f"Failed to upload file to GCS: {e}", exc_info=True)
        raise

def download_media(media_id):
    """Descarga un archivo multimedia desde la API de WhatsApp."""
    if not WHATSAPP_ACCESS_TOKEN:
        logger.error("WhatsApp access token is not configured.")
        return None, None
    
    # 1. Obtener la URL del medio
    url_get = f"https://graph.facebook.com/v19.0/{media_id}/"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    
    try:
        response_get = requests.get(url_get, headers=headers)
        response_get.raise_for_status()
        media_info = response_get.json()
        media_url = media_info.get('url')
        if not media_url:
            logger.error(f"Could not get media URL from Meta. Response: {media_info}")
            return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get media URL: {e}")
        return None, None

    # 2. Descargar el archivo usando la URL obtenida con requests
    temp_file_path = None
    try:
        response_download = requests.get(media_url, headers=headers, stream=True)
        response_download.raise_for_status()
        
        # Crear un archivo temporal para guardar el contenido
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            for chunk in response_download.iter_content(chunk_size=8192):
                temp_file.write(chunk)
            temp_file_path = temp_file.name
        
        # 3. Detectar el tipo MIME
        mime_type = magic.from_file(temp_file_path, mime=True)
        
        return temp_file_path, mime_type
        
    except Exception as e:
        logger.error(f"Failed to download media or detect MIME type: {e}")
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        return None, None

def transcribe_audio(file_path, mime_type):
    """
    Transcribe audio usando Google Cloud Speech-to-Text
    """
    if not SPEECH_CLIENT_AVAILABLE:
        logger.warning("Google Cloud Speech-to-Text no está disponible")
        return None
    
    try:
        client = speech.SpeechClient()
        
        # Leer el archivo de audio
        with open(file_path, 'rb') as audio_file:
            content = audio_file.read()
        
        # Configurar el audio
        audio = speech.RecognitionAudio(content=content)
        
        # Determinar el formato de audio basado en mime_type
        encoding = speech.RecognitionConfig.AudioEncoding.OGG_OPUS
        if 'mp3' in mime_type:
            encoding = speech.RecognitionConfig.AudioEncoding.MP3
        elif 'wav' in mime_type:
            encoding = speech.RecognitionConfig.AudioEncoding.LINEAR16
        elif 'webm' in mime_type:
            encoding = speech.RecognitionConfig.AudioEncoding.WEBM_OPUS
        
        # Configurar el reconocimiento
        config = speech.RecognitionConfig(
            encoding=encoding,
            sample_rate_hertz=16000,  # Frecuencia de muestreo típica
            language_code="es-ES",    # Español
            alternative_language_codes=["es-CO", "es-MX", "en-US"],  # Alternativas
            enable_automatic_punctuation=True,
            enable_word_confidence=True,
            max_alternatives=1
        )
        
        # Realizar la transcripción
        response = client.recognize(config=config, audio=audio)
        
        # Extraer el texto transcrito
        if response.results:
            transcription = response.results[0].alternatives[0].transcript
            confidence = response.results[0].alternatives[0].confidence
            
            logger.info(f"Transcripción exitosa con confianza: {confidence:.2f}")
            return transcription
        else:
            logger.warning("No se pudo transcribir el audio")
            return None
            
    except Exception as e:
        logger.error(f"Error al transcribir audio: {str(e)}")
        return None

def send_whatsapp_message(to, text, phone_number_id):
    """Envía un mensaje de texto a un usuario de WhatsApp."""
    if not WHATSAPP_ACCESS_TOKEN or not phone_number_id:
        logger.error("WhatsApp access token or phone number ID is not configured.")
        return
    
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": text},
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        logger.info(f"Message sent to {to}. Response: {response.json()}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send message to {to}: {e}")
        if e.response is not None:
            logger.error(f"Response body: {e.response.text}")

def process_and_send_event(event, sender_id, phone_number_id):
    """Procesa un evento del stream del agente, extrae el texto y lo envía por WhatsApp."""
    text_to_send = extract_text_from_event(event)
    
    if text_to_send:
        logger.info(f"Extracted text from agent event to send via WhatsApp: '{text_to_send}'")
        send_whatsapp_message(sender_id, text_to_send, phone_number_id)
    else:
        logger.debug(f"Ignoring non-text event for WhatsApp: {type(event)}")


@app.route('/whatsapp/webhook', methods=['GET', 'POST']) # type: ignore
def whatsapp_webhook():
    if request.method == 'GET':
        # Verificación del Webhook
        verify_token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if verify_token == WHATSAPP_VERIFY_TOKEN:
            logger.info("Webhook verified successfully.")
            return challenge, 200
        logger.error("Webhook verification failed.")
        return "Invalid verification token", 403

    # Procesamiento de mensajes POST
    data = request.get_json()
    if not data:
        logger.warning("Received empty POST data.")
        return "No data received", 400

    logger.info(f"Received WhatsApp Webhook: {json.dumps(data)}")

    # Asegurarse de que el motor del agente está listo
    if not remote_agent_engine:
        logger.error("Agent Engine not available to process WhatsApp message.")
        # Se responde con 200 para no tener reintentos de WhatsApp, pero se loguea el error.
        return "OK", 200

    # Extraer información relevante del payload de WhatsApp
    try:
        # Estructura del payload de WhatsApp
        entry = data['entry'][0]
        change = entry['changes'][0]
        value = change['value']

        # Verificar si el payload es un mensaje nuevo o una actualización de estado
        if 'messages' not in value:
            logger.info("Webhook received is not a new message (e.g., status update). Ignoring.")
            return "OK", 200
            
        message = value['messages'][0]
        sender_id = message['from']
        phone_number_id = value['metadata']['phone_number_id']
        
        # Determinar el tipo de mensaje
        msg_type = message['type']
        
        # Obtener o crear una sesión para el usuario
        session_id = session_cache.get(sender_id)
        if not session_id:
            logger.info(f"No active session for {sender_id}, creating a new one.")
            new_session = remote_agent_engine.create_session(user_id=sender_id)
            session_id = new_session['id']
            session_cache[sender_id] = session_id
            logger.info(f"New session {session_id} created for {sender_id}.")
        else:
            logger.info(f"Using existing session {session_id} for {sender_id}.")

        if msg_type == 'image':
            media_id = message['image']['id']
            temp_file_path, mime_type = download_media(media_id)
            if temp_file_path and mime_type:
                logger.info(f"Archivo de imagen descargado en: {temp_file_path}, MIME-type: {mime_type}")
                try:
                    blob_name = f"temp_media/{os.path.basename(temp_file_path)}"
                    upload_to_gcs(temp_file_path, STAGING_BUCKET, blob_name)
                    gcs_uri = f"gs://{STAGING_BUCKET}/{blob_name}"

                    # Usar mensaje de texto descriptivo (método de respaldo)
                    prompt_para_imagen = f"El usuario ha enviado una imagen. El archivo de imagen está disponible en la URI: {gcs_uri}. Por favor, analiza este archivo de imagen y responde de manera apropiada basándote en lo que ves en la imagen."
                    
                    logger.info(f"Enviando imagen al Agent Engine usando mensaje de texto: {gcs_uri}")
                    
                    for event in remote_agent_engine.stream_query( # type: ignore
                        user_id=sender_id,
                        session_id=session_id,
                        message=prompt_para_imagen,  # Solo el mensaje de texto
                    ):
                        process_and_send_event(event, sender_id, phone_number_id)
                finally:
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
            else:
                send_whatsapp_message(sender_id, "No pude procesar la imagen que enviaste.", phone_number_id)

        elif msg_type == 'audio':
            media_id = message['audio']['id']
            temp_file_path, mime_type = download_media(media_id)
            if temp_file_path and mime_type:
                logger.info(f"Archivo de audio descargado en: {temp_file_path}, MIME-type: {mime_type}")
                try:
                    # Intentar transcribir el audio primero
                    transcribed_text = transcribe_audio(temp_file_path, mime_type)
                    
                    if transcribed_text:
                        logger.info(f"Audio transcrito exitosamente: {transcribed_text}")
                        # Usar el texto transcrito directamente
                        audio_message = f"El usuario ha enviado una nota de voz que dice: \"{transcribed_text}\". Por favor, responde de manera apropiada a este mensaje."
                    else:
                        # Si no se puede transcribir, subir a GCS y usar mensaje de respaldo
                        blob_name = f"temp_media/{os.path.basename(temp_file_path)}"
                        upload_to_gcs(temp_file_path, STAGING_BUCKET, blob_name)
                        gcs_uri = f"gs://{STAGING_BUCKET}/{blob_name}"
                        audio_message = f"El usuario ha enviado una nota de voz. El archivo de audio está disponible en la URI: {gcs_uri}. Por favor, procesa este archivo de audio, transcribe su contenido si es necesario, y responde de manera apropiada basándote en lo que el usuario dice en el audio."
                        logger.info(f"Usando mensaje de respaldo con GCS URI: {gcs_uri}")
                    
                    logger.info(f"Enviando audio al Agent Engine")
                    
                    for event in remote_agent_engine.stream_query( # type: ignore
                        user_id=sender_id,
                        session_id=session_id,
                        message=audio_message,  # Mensaje con transcripción o respaldo
                    ):
                        process_and_send_event(event, sender_id, phone_number_id)
                finally:
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
            else:
                send_whatsapp_message(sender_id, "No pude procesar el audio que enviaste.", phone_number_id)
        
        else:  # Mensaje de texto
            message_body = message['text']['body']
            logger.info(f"Texto del mensaje de WhatsApp: '{message_body}'")
            # Usar un generador para procesar la respuesta en streaming
            for event in remote_agent_engine.stream_query( # type: ignore
                user_id=sender_id,
                session_id=session_id,
                message=message_body,
            ):
                process_and_send_event(event, sender_id, phone_number_id)

    except (KeyError, IndexError) as e:
        logger.error(f"Error parsing WhatsApp payload: {e} - Payload: {json.dumps(data)}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in whatsapp_webhook: {e}", exc_info=True)

    # WhatsApp espera una respuesta 200 OK para confirmar la recepción del mensaje
    return "OK", 200


# Esta sección es clave para asegurar que Gunicorn encuentre la app
if __name__ == '__main__':
    # Esto se usa solo para desarrollo local, no en Gunicorn
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
else:
    # Cuando se ejecuta con Gunicorn, este bloque no se ejecuta,
    # pero nos aseguramos que el objeto 'app' está listo.
    gunicorn_app = app