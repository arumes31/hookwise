import os
import signal
import sys
from typing import Any

from dotenv import load_dotenv

from hookwise import create_app, socketio

load_dotenv()

app = create_app()


def graceful_shutdown(sig: int, frame: Any) -> None:
    print("Shutting down gracefully...")
    # Add any cleanup logic here
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

if __name__ == "__main__":
    from gevent import monkey

    monkey.patch_all()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
