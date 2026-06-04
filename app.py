import os
import secrets
import requests
import uuid
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, send_from_directory
)
from flask_session import Session
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', secrets.token_hex(32)),
    SESSION_TYPE='filesystem',
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
    UPLOAD_FOLDER=os.path.join(os.getcwd(), 'uploads'),
    MAX_CONTENT_LENGTH=500 * 1024 * 1024  # 500 MB max upload size
)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

Session(app)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

GRAPH_URL = 'https://graph.facebook.com/v19.0'

# ---------- Helpers (same as before) ----------
def fetch_facebook_user(access_token):
    try:
        resp = requests.get(f'{GRAPH_URL}/me', params={
            'fields': 'id,name,picture{url}',
            'access_token': access_token
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                'id': data['id'],
                'name': data['name'],
                'picture': data.get('picture', {}).get('data', {}).get('url', '')
            }
    except:
        pass
    return None

def fetch_user_pages(access_token):
    try:
        resp = requests.get(f'{GRAPH_URL}/me/accounts', params={
            'fields': 'id,name,access_token,category,fan_count,picture{url}',
            'access_token': access_token,
            'limit': 100
        }, timeout=10)
        if resp.status_code == 200:
            pages = resp.json().get('data', [])
            enriched = []
            for p in pages:
                enriched.append({
                    'id': p['id'],
                    'name': p.get('name'),
                    'category': p.get('category', 'N/A'),
                    'fan_count': p.get('fan_count', 0),
                    'picture': p.get('picture', {}).get('data', {}).get('url', ''),
                    'access_token': p.get('access_token')
                })
            return enriched
    except:
        pass
    return []

def exchange_code_for_token(app_id, app_secret, redirect_uri, code):
    try:
        params = {
            'client_id': app_id,
            'client_secret': app_secret,
            'redirect_uri': redirect_uri,
            'code': code
        }
        resp = requests.get(f'{GRAPH_URL}/oauth/access_token', params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json().get('access_token')
    except:
        pass
    return None

# ---------- Authentication Routes (unchanged) ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/connect/token', methods=['POST'])
@limiter.limit("5 per minute")
def connect_token():
    token = request.json.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Access token required.'}), 400
    user = fetch_facebook_user(token)
    if not user:
        return jsonify({'error': 'Invalid Facebook token.'}), 400
    pages = fetch_user_pages(token)
    session['fb_token'] = token
    session['user'] = user
    session['pages'] = pages
    session['login_method'] = 'token'
    return jsonify({'success': True, 'user': user, 'pages_count': len(pages)})

@app.route('/connect/app', methods=['POST'])
@limiter.limit("5 per minute")
def connect_app():
    app_id = request.json.get('app_id', '').strip()
    app_secret = request.json.get('app_secret', '').strip()
    if not app_id or not app_secret:
        return jsonify({'error': 'Both App ID and App Secret required.'}), 400
    session['app_id'] = app_id
    session['app_secret'] = app_secret
    return jsonify({'success': True})

@app.route('/connect/facebook')
def facebook_oauth_start():
    if not session.get('app_id') or not session.get('app_secret'):
        flash('Save your App ID and Secret first.', 'warning')
        return redirect(url_for('index'))
    redirect_uri = url_for('facebook_oauth_callback', _external=True)
    fb_url = (
        f'https://www.facebook.com/v19.0/dialog/oauth?'
        f'client_id={session["app_id"]}&redirect_uri={redirect_uri}'
        f'&scope=pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_metadata,pages_read_engagement,pages_manage_posts'
        f'&response_type=code'
    )
    return redirect(fb_url)

@app.route('/connect/facebook/callback')
def facebook_oauth_callback():
    code = request.args.get('code')
    if not code:
        flash('Authorization failed.', 'danger')
        return redirect(url_for('index'))
    app_id = session.get('app_id')
    app_secret = session.get('app_secret')
    if not app_id or not app_secret:
        flash('App credentials missing.', 'danger')
        return redirect(url_for('index'))
    redirect_uri = url_for('facebook_oauth_callback', _external=True)
    access_token = exchange_code_for_token(app_id, app_secret, redirect_uri, code)
    if not access_token:
        flash('Failed to exchange code for token.', 'danger')
        return redirect(url_for('index'))
    user = fetch_facebook_user(access_token)
    if not user:
        flash('Could not fetch profile.', 'danger')
        return redirect(url_for('index'))
    pages = fetch_user_pages(access_token)
    session['fb_token'] = access_token
    session['user'] = user
    session['pages'] = pages
    session['login_method'] = 'oauth'
    flash(f'Welcome, {user["name"]}!', 'success')
    return redirect(url_for('index'))

# ---------- API: Pages & Posts ----------
@app.route('/api/me')
def api_me():
    if 'fb_token' not in session or 'user' not in session:
        return jsonify({'authenticated': False})
    return jsonify({
        'authenticated': True,
        'user': session['user'],
        'pages_count': len(session.get('pages', [])),
        'login_method': session.get('login_method')
    })

@app.route('/api/pages')
def api_pages():
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    pages = fetch_user_pages(session['fb_token'])
    session['pages'] = pages
    return jsonify(pages)

@app.route('/api/page/<page_id>/posts')
def api_page_posts(page_id):
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    pages = session.get('pages', [])
    page_token = next((p['access_token'] for p in pages if p['id'] == page_id), None)
    if not page_token:
        return jsonify({'error': 'Page not found'}), 404
    try:
        resp = requests.get(f'{GRAPH_URL}/{page_id}/feed', params={
            'fields': 'message,created_time,permalink_url',
            'access_token': page_token,
            'limit': 10
        }, timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json().get('data', []))
    except:
        pass
    return jsonify({'error': 'Failed to fetch posts'}), 500

# ---------- MEDIA UPLOAD ----------
def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'jpg', 'jpeg', 'png', 'gif', 'webm'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/upload', methods=['POST'])
@limiter.limit("30 per minute")
def upload_file():
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400

    # Secure filename and save
    original_filename = secure_filename(file.filename)
    ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'bin'
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file.save(save_path)

    file_size = os.path.getsize(save_path)
    return jsonify({
        'success': True,
        'file': {
            'name': unique_name,
            'original_name': original_filename,
            'size': file_size,
            'url': f'/media/{unique_name}',
            'uploaded_at': datetime.utcnow().isoformat()
        }
    })

@app.route('/media/<filename>')
def serve_media(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/media', methods=['GET'])
def list_media():
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    files = []
    for f in os.listdir(app.config['UPLOAD_FOLDER']):
        path = os.path.join(app.config['UPLOAD_FOLDER'], f)
        if os.path.isfile(path):
            files.append({
                'name': f,
                'size': os.path.getsize(path),
                'modified': datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            })
    return jsonify(files)

@app.route('/api/media/<filename>', methods=['DELETE'])
def delete_media(filename):
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    safe_name = secure_filename(filename)
    path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
    if os.path.exists(path):
        os.remove(path)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/media/rename', methods=['POST'])
def rename_media():
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.json
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    if not old_name or not new_name:
        return jsonify({'error': 'Invalid parameters'}), 400
    old_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(old_name))
    new_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(new_name))
    if not os.path.exists(old_path):
        return jsonify({'error': 'File not found'}), 404
    os.rename(old_path, new_path)
    return jsonify({'success': True, 'new_name': new_name})

# ---------- FACEBOOK VIDEO PUBLISHING ----------
@app.route('/api/page/<page_id>/publish/video', methods=['POST'])
def publish_video(page_id):
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    pages = session.get('pages', [])
    page_token = next((p['access_token'] for p in pages if p['id'] == page_id), None)
    if not page_token:
        return jsonify({'error': 'Page not found or token missing'}), 404

    data = request.json
    filename = data.get('filename')
    caption = data.get('caption', '')
    scheduled_time = data.get('scheduled_time')  # Unix timestamp (optional)

    if not filename:
        return jsonify({'error': 'No file specified'}), 400

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
    if not os.path.exists(file_path):
        return jsonify({'error': 'Video file not found on server'}), 404

    # Upload video to Facebook
    url = f'{GRAPH_URL}/{page_id}/videos'
    params = {
        'access_token': page_token,
        'description': caption,
    }
    if scheduled_time:
        params['scheduled_publish_time'] = int(scheduled_time)
        params['published'] = 'false'  # scheduled video
    else:
        params['published'] = 'true'

    try:
        with open(file_path, 'rb') as f:
            files = {'source': (filename, f, 'application/octet-stream')}
            resp = requests.post(url, params=params, files=files, timeout=60)
        if resp.status_code == 200:
            return jsonify({'success': True, 'facebook_video_id': resp.json().get('id')})
        else:
            return jsonify({'error': f'Facebook API error: {resp.text}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return jsonify({'error': 'CSRF validation failed'}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
