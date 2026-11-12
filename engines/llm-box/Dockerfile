# Образ движка языковой модели. Драйвера NVIDIA здесь нет: его библиотеки
# площадка подключает сама. В образе только то, чем движок считает.
FROM python:3.12-slim
WORKDIR /app
# torch берётся с индекса PyTorch под названную CUDA: на PyPI сборка под CUDA
# меняется от версии к версии. Образ с ним весит несколько гигабайт, сборка идёт
# от десяти минут и дольше, смотря как сеть машины отдаёт библиотеки.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu128 "torch>=2.8,<2.11" \
 && pip install --no-cache-dir "transformers>=4.46,<5"
COPY llm_engine ./llm_engine
# Служба в песочнице слушает все адреса: площадка приходит в её сеть снаружи.
ENV PYTHONUNBUFFERED=1 LLM_BIND=0.0.0.0
CMD ["python", "-m", "llm_engine.server"]
