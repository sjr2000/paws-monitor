#!/bin/bash
playwright install chromium --with-deps
python monitor.py
