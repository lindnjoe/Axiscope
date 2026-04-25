import os
from flask.app import Flask
from flask.helpers import send_from_directory

app = Flask(__name__)

# Disable browser caching of the static UI assets so that updates to
# js/css/index.html land immediately after a service restart instead of
# being masked by stale cached copies.
@app.after_request
def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:path>')
def serve_files(path):
    return send_from_directory('.', path)

if __name__ == '__main__':
    try:
        from waitress import serve
        print("Starting AxisScope server on port 3000...")
        serve(app, host='0.0.0.0', port=3000)
    except Exception as e:
        print(f"Error starting server: {e}")
        exit(1)
