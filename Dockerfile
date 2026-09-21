FROM python:3.12-slim

# sounddevice 运行时需要 PortAudio 共享库
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

CMD ["python", "-c", "import pipecat, sounddevice, dashscope; print('paimon-voice deps ok')"]
