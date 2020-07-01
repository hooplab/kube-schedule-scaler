FROM python:3.8-alpine

RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev libressl-dev

RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir poetry

RUN apk del gcc musl-dev python3-dev libffi-dev libressl-dev

RUN adduser -u 1000 -D app && \
    mkdir /app && \
    chown app: /app

USER 1000
WORKDIR /app

COPY poetry.lock pyproject.toml ./
COPY schedule_scaling/ ./schedule_scaling/

RUN poetry config virtualenvs.create false
RUN poetry install --no-dev

CMD ["poetry", "run", "schedule_scaling/main.py"]
