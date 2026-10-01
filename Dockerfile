FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY physics ./physics
RUN useradd --create-home ghost && mkdir -p /app/data && chown ghost:ghost /app/data
USER ghost
ENV PORT=8787
EXPOSE 8787
CMD ["sh", "-c", "exec gunicorn 'app:create_app()' --bind 0.0.0.0:${PORT:-8787} --workers 1 --threads 4 --no-control-socket --timeout 60 --max-requests 3000 --max-requests-jitter 300"]
