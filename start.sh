#!/bin/sh
celery -A app.core.celery_app worker --loglevel=info --concurrency=2 &
uvicorn app.main:app --host 0.0.0.0 --port 8080
