"""
run_server.py
=============
Start the FastAPI development server.

Usage:
    python run_server.py
    python run_server.py --host 0.0.0.0 --port 8080
"""

import argparse
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start Smart RMC AI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", default=True)
    args = parser.parse_args()

    uvicorn.run(
        "src.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
