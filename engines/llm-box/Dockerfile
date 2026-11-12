# Образ движка языковой модели. Драйвера NVIDIA здесь нет: его библиотеки
# площадка подключает сама. В образе только то, чем движок считает.
FROM python:3.12-slim
WORKDIR /app
# torch 2.8–2.10 на PyPI собран под CUDA 12.8 и работает с драйвером не старше
# неё. Образ с ним весит около 3 ГБ: сборка и первая закачка идут минуты.
RUN pip install --no-cache-dir "torch>=2.8,<2.11" "transformers>=4.46,<5"
COPY llm_engine ./llm_engine
# Служба в песочнице слушает все адреса: площадка приходит в её сеть снаружи.
ENV PYTHONUNBUFFERED=1 LLM_BIND=0.0.0.0
CMD ["python", "-m", "llm_engine.server"]
