FROM python:3.13-slim

# Establece el directorio de trabajo en el contenedor
WORKDIR /app

# Copia el archivo de requisitos primero para aprovechar el caché de Docker
COPY requirements.txt requirements.txt

# Instala las dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código al directorio de trabajo
COPY . .

# Comando para ejecutar tu aplicación Flask cuando el contenedor se inicie.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "new_python_agent_backend:app"]
