FROM python:3.12-slim

RUN useradd -m -u 1000 user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860 \
    WORKBENCH_DATA_DIR=/home/user/app/data \
    WORKBENCH_VIDEOS_DIR=/home/user/app/videos \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

USER user
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=user app.py README.md LICENSE.md ./
COPY --chown=user static ./static

RUN mkdir -p "$WORKBENCH_DATA_DIR" "$WORKBENCH_VIDEOS_DIR" tools

EXPOSE 7860

CMD ["python", "app.py"]
