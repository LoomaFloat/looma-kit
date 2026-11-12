# Образ учебного движка. Площадка собирает его сама: в папке движка должны
# лежать этот файл и всё, что он копирует.
FROM python:3.12-slim
WORKDIR /app
COPY echo_engine ./echo_engine
ENV PYTHONUNBUFFERED=1
# Команду запуска площадка берёт отсюда, если в манифесте нет image.command.
CMD ["python", "-m", "echo_engine.server"]
