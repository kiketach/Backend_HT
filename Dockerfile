FROM python:3.13-slim

# Establece el directorio de trabajo en el contenedor
WORKDIR /app

# Copia el archivo de requisitos primero para aprovechar el caché de Docker
COPY requirements.txt requirements.txt

# Instala las dependencias del sistema (libmagic) y luego las de Python
RUN apt-get update && apt-get install -y libmagic1 && \
    pip install --no-cache-dir -r requirements.txt

# Copia el resto del código al directorio de trabajo
COPY . .

# Expone el puerto que Gunicorn usará (proporcionado por la variable de entorno PORT)
EXPOSE 8080

# Comando para ejecutar la aplicación usando Gunicorn y la variable de entorno PORT de Cloud Run.
CMD exec gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 new_python_agent_backend:app
