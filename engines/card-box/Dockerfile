# Образ учебного движка на видеокарте. Драйвера NVIDIA в образе нет и быть не
# должно: его библиотеки площадка подключает сама, той же версии, что на машине.
# Настоящему движку сюда кладут свои библиотеки для расчётов (PyTorch и прочее).
FROM python:3.12-slim
WORKDIR /app
COPY card_engine ./card_engine
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "card_engine.server"]
