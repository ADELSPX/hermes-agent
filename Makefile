
lint-python:
	sudo docker run --rm -it -v ./:/src/ ghcr.io/astral-sh/ruff:0.16.10 check --config /src/ruff.toml /src/

lint-python-fix:
	sudo docker run --rm -it -v ./:/src/ ghcr.io/astral-sh/ruff:0.16.10 check --fix --config /src/ruff.toml /src/
