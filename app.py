import os
import secrets
import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash
)
from flask_session import Session
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', secrets.token_hex(32)),
    SESSION_TYPE='filesystem',
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_SECURE=False,          # set True in production with HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
Session(app)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

GRAPH_URL = 'https://graph.facebook.com/v19.0'

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def fetch_facebook_user(access_token):
    """Validate token and return basic profile info."""
    resp = requests.get(f'{GRAPH_URL}/me', params={
        'fields': 'id,name,picture{url}',
        'access_token': access_token
    })
    if resp.status_code == 200:
        data = resp.json()
        return {
            'id': data['id'],
            'name': data['name'],
            'picture': data.get('picture', {}).get('data', {}).get('url', '')
        }
    return None

def fetch_user_pages(access_token):
    """Get all Facebook Pages managed by the user."""
    resp = requests.get(f'{GRAPH_URL}/me/accounts', params={
        'fields': 'id,name,access_token,category,fan_count,picture{url}',
        'access_token': access_token,
        'limit': 100
    })
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
    return []

def exchange_code_for_token(app_id, app_secret, redirect_uri, code):
    """Exchange authorization code for a user access token."""
    params = {
        'client_id': app_id,
        'client_secret': app_secret,
        'redirect_uri': redirect_uri,
        'code': code
    }
    resp = requests.get(f'{GRAPH_URL}/oauth/access_token', params=params)
    if resp.status_code == 200:
        data = resp.json()
        return data.get('access_token')
    return None

# ----------------------------------------------------------------------
# Routes – connection methods
# ----------------------------------------------------------------------
@app.route('/')
def index():
    """Single page that shows connection screen or dashboard."""
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

# ---- Method 1: Access Token Login ----
@app.route('/connect/token', methods=['POST'])
@limiter.limit("5 per minute")
def connect_token():
    token = request.json.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Access token is required.'}), 400

    # Validate the token
    user = fetch_facebook_user(token)
    if not user:
        return jsonify({'error': 'Invalid Facebook token.'}), 400

    # Fetch managed pages
    pages = fetch_user_pages(token)
    # Store everything in session
    session['fb_token'] = token
    session['user'] = user
    session['pages'] = pages
    session['login_method'] = 'token'
    return jsonify({'success': True, 'user': user, 'pages_count': len(pages)})

# ---- Method 2: App ID + App Secret + OAuth ----
@app.route('/connect/app', methods=['POST'])
@limiter.limit("5 per minute")
def connect_app():
    app_id = request.json.get('app_id', '').strip()
    app_secret = request.json.get('app_secret', '').strip()
    if not app_id or not app_secret:
        return jsonify({'error': 'Both App ID and App Secret are required.'}), 400

    session['app_id'] = app_id
    session['app_secret'] = app_secret
    return jsonify({'success': True, 'message': 'App credentials saved. You can now connect with Facebook.'})

@app.route('/connect/facebook')
def facebook_oauth_start():
    if not session.get('app_id') or not session.get('app_secret'):
        flash('Please save your App ID and App Secret first.', 'warning')
        return redirect(url_for('index'))

    # Build Facebook OAuth URL using the user's own App ID
    redirect_uri = url_for('facebook_oauth_callback', _external=True)
    fb_auth_url = (
        f'https://www.facebook.com/v19.0/dialog/oauth?'
        f'client_id={session["app_id"]}&redirect_uri={redirect_uri}'
        f'&scope=pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_metadata'
        f'&response_type=code'
    )
    return redirect(fb_auth_url)

@app.route('/connect/facebook/callback')
def facebook_oauth_callback():
    code = request.args.get('code')
    if not code:
        flash('Authorization failed – no code received.', 'danger')
        return redirect(url_for('index'))

    app_id = session.get('app_id')
    app_secret = session.get('app_secret')
    if not app_id or not app_secret:
        flash('App credentials missing. Please reconfigure.', 'danger')
        return redirect(url_for('index'))

    redirect_uri = url_for('facebook_oauth_callback', _external=True)
    access_token = exchange_code_for_token(app_id, app_secret, redirect_uri, code)
    if not access_token:
        flash('Failed to exchange code for token. Check your App ID & Secret.', 'danger')
        return redirect(url_for('index'))

    # Fetch user & pages with the new token
    user = fetch_facebook_user(access_token)
    if not user:
        flash('Could not fetch your Facebook profile.', 'danger')
        return redirect(url_for('index'))

    pages = fetch_user_pages(access_token)
    session['fb_token'] = access_token
    session['user'] = user
    session['pages'] = pages
    session['login_method'] = 'oauth'
    flash(f'Welcome, {user["name"]}!', 'success')
    return redirect(url_for('index'))

# ----------------------------------------------------------------------
# API endpoints (used by the dashboard after connection)
# ----------------------------------------------------------------------
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
    # Refresh pages from Facebook to keep fan_count up to date
    pages = fetch_user_pages(session['fb_token'])
    session['pages'] = pages   # update cached data
    return jsonify(pages)

@app.route('/api/page/<page_id>/posts')
def api_page_posts(page_id):
    if 'fb_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    # Find the page-specific token
    pages = session.get('pages', [])
    page_token = None
    for p in pages:
        if p['id'] == page_id:
            page_token = p['access_token']
            break
    if not page_token:
        return jsonify({'error': 'Page not found'}), 404

    resp = requests.get(f'{GRAPH_URL}/{page_id}/feed', params={
        'fields': 'message,created_time,permalink_url',
        'access_token': page_token,
        'limit': 10
    })
    if resp.status_code == 200:
        return jsonify(resp.json().get('data', []))
    return jsonify({'error': 'Failed to fetch posts'}), 500

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return jsonify({'error': 'CSRF validation failed'}), 400

if __name__ == '__main__':
    app.run(debug=True)
