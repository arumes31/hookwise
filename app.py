import eventlet
eventlet.monkey_patch()

import os
from dotenv import load_dotenv
from hookwise import create_app, socketio

load_dotenv()

app = create_app()

with app.app_context():
    from hookwise.extensions import db
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
