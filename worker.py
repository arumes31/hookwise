import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
load_dotenv()

from hookwise import create_app
from hookwise.tasks import celery

app = create_app()
app.app_context().push()

if __name__ == '__main__':
    celery.start()
