
# Use Python 3.12 on a lightweight Linux base image.
FROM python:3.12-slim

# Set the application's working directory.
WORKDIR /app

# Prevent Python from writing .pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Display Python logs immediately.
ENV PYTHONUNBUFFERED=1

# Copy dependency definitions first for better build caching.
COPY requirements.txt .

# Install the application's Python dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code and web interface.
COPY app/ ./app/

# Create a non-root user to run the application.
RUN useradd --create-home appuser
USER appuser

# Document the port used by the application.
EXPOSE 8000

# Start the FastAPI application inside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
