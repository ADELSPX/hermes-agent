set default-list := true

[group('lint')]
lint: lint-python

[group('lint')]
lint-python:
	ruff check --config ./ruff.strict.toml .

[group('lint')]
lint-python-fix:
	ruff check --config ./ruff.strict.toml . --fix
