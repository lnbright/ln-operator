PYTHON := venv/bin/python3

.PHONY: test
test:
	$(PYTHON) -m unittest discover tests -v
