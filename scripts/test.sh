#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH=${PYTHONPATH:-src}
python -m unittest discover -s tests -p 'test_*.py' -v

