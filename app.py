# Das Patchen muss vor jedem Import stehen, der socket, ssl oder threading
# beruehrt -- danach behalten bereits gebundene Standardbibliotheks-Objekte ihre
# blockierenden Aufrufe und blockieren den Greenlet-Scheduler statt zu yielden.
# In den Containern startet gunicorn mit --worker-class gevent und patcht selbst;
# dieser Aufruf deckt den direkten Start ``python app.py`` ab und ist idempotent.
from gevent import monkey  # noqa: E402  isort:skip

monkey.patch_all()

import os  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from hookwise import create_app, socketio  # noqa: E402

load_dotenv()

app = create_app()


def graceful_shutdown(sig: int, frame: Any) -> None:
    print("Shutting down gracefully...")
    # Add any cleanup logic here
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
