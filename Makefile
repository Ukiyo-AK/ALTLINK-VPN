PYTHON=python

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e .[dev]

run-backend:
	$(PYTHON) -m altlink.main backend

run-client-bot:
	$(PYTHON) -m altlink.main client-bot

run-admin-bot:
	$(PYTHON) -m altlink.main admin-bot

run-scheduler:
	$(PYTHON) -m altlink.main scheduler

migrate:
	$(PYTHON) -m alembic upgrade head

makemigrations:
	$(PYTHON) -m alembic revision --autogenerate -m "$(m)"

seed:
	$(PYTHON) -m altlink.cli seed-defaults

create-admin:
	$(PYTHON) -m altlink.cli create-admin

test:
	$(PYTHON) -m pytest

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

