.PHONY: test demo live

PYTHON ?= python3

test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -v

demo:
	$(PYTHON) -m shelflife_context.demo

live:
	$(PYTHON) -m shelflife_context.demo --live --repetitions 3
