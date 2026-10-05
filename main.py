"""
Entry point for the Healthcare Referral Agent API.

    uvicorn main:app --reload
    python main.py
"""

import os

from backend.app import app

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HEALTHCARE_REFERRAL_HOST", "127.0.0.1"),
        port=int(os.environ.get("HEALTHCARE_REFERRAL_PORT", "8000")),
    )
