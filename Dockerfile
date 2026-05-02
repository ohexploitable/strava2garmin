# Start from an official Python image (slim = smaller download, no extras)
FROM python:3.9-slim

# Set the working directory inside the container
WORKDIR /app

# Copy and install dependencies first — Docker caches this layer,
# so rebuilds are fast as long as requirements.txt hasn't changed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY sync.py webhook.py ./

# Cloud Run sets PORT=8080 and expects the app to listen there
EXPOSE 8080

# Run with gunicorn (production server) instead of Flask's dev server
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "webhook:app"]
