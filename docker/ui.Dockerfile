FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /ui
RUN pip install "streamlit>=1.36,<2.0" "httpx>=0.27,<1.0" "pandas>=2.2,<3.0"
COPY ui/ .

EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
