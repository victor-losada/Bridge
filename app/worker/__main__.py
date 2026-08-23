"""Punto de entrada: python -m app.worker"""

from app.worker.worker import run_worker_from_env

if __name__ == "__main__":
    run_worker_from_env()
