import logging
import os

from fastapi import FastAPI

from app.api import router

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="Log Detector API")
app.include_router(router)
