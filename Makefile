.PHONY: test demo live

PYTHON ?= python3

test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -v

demo:
	$(PYTHON) -m context_kernel.demo

live:
	$(PYTHON) -m context_kernel.demo --live --repetitions 3
