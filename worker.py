import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
load_dotenv()

from hookwise.tasks import celery
from hookwise import create_app

app = create_app()
app.app_context().push()

if __name__ == '__main__':
    celery.start()
