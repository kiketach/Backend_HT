from flask import Flask, request, Response, jsonify
from flask_cors import CORS
import vertexai
from vertexai import agent_engines
from dotenv import load_dotenv
import os
import json
import logging

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=dotenv_path)

# Configurar logging básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Simplify CORS initialization to allow all origins and methods for testing
CORS(app)

@app.before_request
def log_request_info():
    logger.info(f"Incoming request: {request.method} {request.path} (URL: {request.url}, BaseURL: {request.base_url}) from {request.remote_addr}")
    logger.info(f"Request Headers: {request.headers}")
    if request.method in ['POST', 'PUT']:
        content_type = request.content_type
        if content_type is not None and 'application/json' in content_type:
            try:
                # Limit body size to avoid overly verbose logs, e.g., 1KB
                body_data = request.get_data(as_text=True)
                if len(body_data) > 1024:
                    logger.info(f"Request JSON Body (truncated): {body_data[:1024]}...")
                else:
                    logger.info(f"Request JSON Body: {body_data}")
            except Exception as e:
                logger.warning(f"Could not log JSON request body: {e}")
        else:
            try:
                # Limit body size for non-JSON as well
                body_data = request.get_data(as_text=True)
                if len(body_data) > 1024:
                    logger.info(f"Request Body (Content-Type: {content_type}, truncated): {body_data[:1024]}...")
                else:
                    logger.info(f"Request Body (Content-Type: {content_type}): {body_data}")
            except Exception as e:
                logger.warning(f"Could not log request data: {e}")

# --- Configuración del Proyecto ---
PROJECT_ID = os.environ.get("PROJECT_ID")
LOCATION = os.environ.get("LOCATION")
AGENT_ENGINE_RESOURCE_NAME = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")

# Global variable for the agent engine
remote_agent_engine = None

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

@app.route('/agent', methods=['POST']) # Ensure 'OPTIONS' is NOT listed here
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
            for event in remote_agent_engine.stream_query(
                user_id=user_id,
                session_id=session_id,
                message=message_text,
                # cuando el agente use herramientas específicas que necesitan parámetros en tiempo de ejecución:
                # tools_config={"tool_parameters_configs": [{"tool": "nombre_de_tu_herramienta", "parameters": {"param1": "valor1"}}]}
            ):
                logger.debug(f"Agent event received: {type(event)}")
                response_data = {}
                # La estructura del evento puede variar. Inspecciona los eventos del agente.
                # Comúnmente, para respuestas de LLM, el texto está en event.text o event.parts[0].text
                if hasattr(event, 'text') and event.text:
                    logger.info(f"Processing event with 'text' attribute: {event.text}")
                    response_data = {"type": "text_event", "message": {"content": {"parts": [{"text": event.text}]}}}
                elif hasattr(event, 'parts') and event.parts and hasattr(event.parts[0], 'text'):
                    logger.info(f"Processing event with 'parts[0].text' attribute: {event.parts[0].text}")
                    response_data = {"type": "content_parts_event", "message": {"content": {"parts": [{"text": event.parts[0].text}]}}}
                elif hasattr(event, 'content') and event.content and hasattr(event.content, 'parts') and \
                     event.content.parts and hasattr(event.content.parts[0], 'text'):
                    logger.info(f"Processing event with 'content.parts[0].text' attribute: {event.content.parts[0].text}")
                    response_data = {"type": "content_event", "message": {"content": {"parts": [{"text": event.content.parts[0].text}]}}}
                # Caso específico para cuando 'event' es un diccionario y tiene la estructura que vimos en data_str
                elif isinstance(event, dict) and \
                     'content' in event and isinstance(event['content'], dict) and \
                     'parts' in event['content'] and isinstance(event['content']['parts'], list) and \
                     len(event['content']['parts']) > 0 and isinstance(event['content']['parts'][0], dict) and \
                     'text' in event['content']['parts'][0]:
                    logger.info(f"Processing event as dict with 'content.parts[0].text': {event['content']['parts'][0]['text']}")
                    response_data = {"type": "dict_content_event", "message": {"content": {"parts": [{"text": event['content']['parts'][0]['text']}]}}}
                # Puedes añadir más casos para ToolCall, ToolResponse si necesitas manejarlos explícitamente en el frontend
                else:
                    # Fallback: intenta convertir el evento a string si no es una estructura conocida.
                    # Esto es solo para depuración, idealmente deberías manejar todos los tipos de eventos esperados.
                    logger.warning(f"Unknown event structure or type: {type(event)} - Event: {event}")
                    response_data = {"type": "unknown_event", "data_str": str(event)}
                if response_data: # Solo envía si hemos construido algo
                    yield f"data: {json.dumps(response_data)}\n\n"
            logger.info(f"Stream ended for session {session_id}")
        except Exception as e_stream:
            logger.error(f"Error during agent stream: {e_stream}", exc_info=True)
            error_response = {"type": "error", "message": "Error processing your request with the agent."}
            yield f"data: {json.dumps(error_response)}\n\n"
        
    return Response(generate_sse(), mimetype='text/event-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting Flask app on port {port}")
    app.run(debug=os.environ.get("FLASK_DEBUG", "False").lower() == "true", host='0.0.0.0', port=port)
