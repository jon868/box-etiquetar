FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY etiquetar.py servidor.py ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/datos \
    PORT=8080

EXPOSE 8080

# El contenedor solo levanta la web de descargas.
# El etiquetado se lanza a mano desde la consola del servicio en Easypanel.
CMD ["python", "servidor.py"]
