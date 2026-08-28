FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Set working directory
WORKDIR /app

# Copy pyproject.toml and source code
COPY pyproject.toml ./
COPY src/ ./src/
COPY examples/ ./examples/
COPY README.md ./

# Install Python dependencies and local rascube package
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir pyserial numpy scipy && \
    pip install --no-cache-dir -e .

# Expose internal API port
EXPOSE 8080

# Run Ground Station REST API & Dashboard
CMD ["python3", "examples/api/server.py", "--host", "0.0.0.0", "--port", "8080"]
