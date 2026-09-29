"""Entry point: `python main.py` starts the Owner Care chat service on port 8000 (override with AGENT_PORT)."""

import json
import logging
import os

import uvicorn

from agent import app, health_info

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("READY " + json.dumps(health_info()), flush=True)
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("AGENT_PORT", "8000")))
