PYTHON=python

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e .[dev]

run-backend:
	$(PYTHON) -m uvicorn altlink.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

run-client-bot:
	$(PYTHON) -m altlink.presentation.bots.client_app

run-admin-bot:
	$(PYTHON) -m altlink.presentation.bots.admin_app

run-scheduler:
	$(PYTHON) -m altlink.scheduler.main

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

