from gevent import monkey

monkey.patch_all()

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from hookwise import create_app  # noqa: E402
from hookwise.tasks import celery  # noqa: E402

app = create_app()
app.app_context().push()

if __name__ == "__main__":
    celery.start()
