.PHONY: all setup run clean

VENV = venv
PYTHON = $(VENV)/bin/python
PIP = $(VENV)/bin/pip

# Default values
QUERY ?= What is an LLM agent?
DATE_RESTRICT ?=
TARGET_SITE ?=
MODEL_NAME ?=
LOG_LEVEL ?= INFO

all: run

setup: $(VENV)/bin/activate

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run: setup
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found. Please create one based on .env.example"; \
		exit 1; \
	fi
	@echo "Running ask.py with environment variables from .env"
	@source $(VENV)/bin/activate && source .env && $(PYTHON) ask.py \
		-q "$(QUERY)" \
		$(if $(DATE_RESTRICT),-d "$(DATE_RESTRICT)") \
		$(if $(TARGET_SITE),-s "$(TARGET_SITE)") \
		$(if $(MODEL_NAME),-m "$(MODEL_NAME)") \
		-l $(LOG_LEVEL)

clean:
	rm -rf $(VENV)
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete

.env:
	@echo "Creating .env file from .env.example"
	@cp .env.example .env
	@echo "Please edit .env and add your actual API key and other configurations"
