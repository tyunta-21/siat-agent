#!/bin/bash
# Ask the SIAT agent a question:  ~/siat_agent/ask.sh "2020-2025 Toshkent viloyatida sanoat"
cd "$(dirname "$0")" && exec .venv/bin/python query_agent.py "$@"
