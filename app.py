import os, json, requests, datetime, tempfile
from flask import Flask, request, jsonify, render_template, send_from_directory, session
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'xmedia-pro-secret-change-in-production')
CORS(app)

# Use /tmp for Render (writable ephemeral filesystem)
basedir = '/tmp'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'xmedia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# Scheduler
class SchedulerConfig:
    SCHEDULER_API_ENABLED = True
app.config.from_object(SchedulerConfig())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# ---------- Models ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_app_id = db.Column(db.String(100), default='')
    fb_app_secret = db.Column(db.String(100), default='')
    fb_long_lived_token = db.Column(db.Text, nullable=True)
    fb_pages_json = db.Column(db.Text, nullable=True)
    groq_api_key = db.Column(db.String(100), nullable=True)
    telegram_bot_token = db.Column(db.String(100), nullable=True)
    telegram_chat_id = db.Column(db.String(100), nullable=True)
    timezone = db.Column(db.String(50), default='UTC')
    auto_upload = db.Column(db.Boolean, default=False)

class Page(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    name = db.Column(db.String(200))
    access_token = db.Column(db.Text)
    fan_count = db.Column(db.Integer, default=0)
    picture_url = db.Column(db.Text)
    tasks = db.Column(db.Text)

class MediaFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    original_name = db.Column(db.String(200))
    upload_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    file_size = db.Column(db.Integer, default=0)
    folder = db.Column(db.String(100), default='My Videos')

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), nullable=False)
    media_id = db.Column(db.Integer, db.ForeignKey('media_file.id'))
    caption = db.Column(db.Text)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    job_id = db.Column(db.String(100), nullable=True)

# ---------- Helpers ----------
def get_user():
    user = db.session.get(User, 1)
    if not user:
        user = User(id=1)
        db.session.add(user)
        db.session.commit()
    return user

def get_page_token(page_id):
    page = db.session.get(Page, page_id)
    if page:
        return page.access_token
    user = get_user()
    if user.fb_pages_json:
        pages = json.loads(user.fb_pages_json)
        for p in pages:
            if p['id'] == page_id:
                return p.get('access_token')
    return None

def exchange_long_lived_token(short_token):
    user = get_user()
    if not user.fb_app_id or not user.fb_app_secret:
        raise Exception('App ID and Secret not configured')
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        'grant_type': 'fb_exchange_token',
        'client_id': user.fb_app_id,
        'client_secret': user.fb_app_secret,
        'fb_exchange_token': short_token
    }
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('access_token')

def fetch_facebook_user(token):
    resp = requests.get("https://graph.facebook.com/v19.0/me", params={
        'fields': 'name,picture{url}',
        'access_token': token
    })
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return {
        'name': data['name'],
        'picture': data.get('picture', {}).get('data', {}).get('url', '')
    }

def fetch_pages(token):
    url = "https://graph.facebook.com/v19.0/me/accounts"
    params = {'access_token': token, 'fields': 'id,name,access_token,fan_count,picture,tasks'}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('data', [])

def upload_to_facebook(page_id, filepath, title='', description='', video_state='PUBLISHED', scheduled_time=None):
    token = get_page_token(page_id)
    if not token:
        raise Exception('No page token')
    file_size = os.path.getsize(filepath)
    # Start upload
    start_params = {'access_token': token, 'upload_phase': 'start', 'file_size': file_size}
    start_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=start_params)
    start_data = start_resp.json()
    if 'error' in start_data:
        raise Exception(start_data['error']['message'])
    video_id = start_data['video_id']
    upload_url = start_data['upload_url']
    # Upload binary
    with open(filepath, 'rb') as f:
        upload_resp = requests.post(upload_url, headers={
            'Authorization': f'OAuth {token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream'
        }, data=f)
    if upload_resp.status_code != 200:
        raise Exception(f'Upload failed: {upload_resp.text}')
    # Finish
    finish_params = {
        'access_token': token, 'upload_phase': 'finish',
        'video_id': video_id, 'title': title, 'description': description,
        'video_state': video_state
    }
    if video_state == 'SCHEDULED' and scheduled_time:
        finish_params['scheduled_publish_time'] = scheduled_time
    finish_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data:
        raise Exception(finish_data['error']['message'])
    return video_id

def execute_scheduled_post(post_id):
    with scheduler.app.app_context():
        post = db.session.get(ScheduledPost, post_id)
        if not post or post.status != 'pending':
            return
        post.status = 'processing'
        db.session.commit()
        try:
            media = db.session.get(MediaFile, post.media_id)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
            upload_to_facebook(post.page_id, filepath, title=post.caption or '', description='')
            post.status = 'published'
        except Exception as e:
            post.status = 'failed'
        finally:
            db.session.commit()

def schedule_job(post):
    job = scheduler.add_job(
        id=f'post_{post.id}',
        func=execute_scheduled_post,
        args=[post.id],
        trigger='date',
        run_date=post.scheduled_time
    )
    post.job_id = job.id
    db.session.commit()

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---------- Authentication ----------
@app.route('/api/connect/token', methods=['POST'])
def connect_with_token():
    token = request.json.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 400
    try:
        user_info = fetch_facebook_user(token)
        pages = fetch_pages(token)
        user = get_user()
        user.fb_long_lived_token = token  # assume it's already long‑lived
        # Save pages
        Page.query.delete()
        for p in pages:
            page = Page(
                id=p['id'], name=p['name'],
                access_token=p['access_token'],
                fan_count=p.get('fan_count', 0),
                picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                tasks=json.dumps(p.get('tasks', []))
            )
            db.session.add(page)
        db.session.commit()
        return jsonify({'success': True, 'user': user_info, 'pages_count': len(pages)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/connect/app', methods=['POST'])
def save_app_credentials():
    app_id = request.json.get('app_id', '').strip()
    app_secret = request.json.get('app_secret', '').strip()
    if not app_id or not app_secret:
        return jsonify({'error': 'Both fields required'}), 400
    user = get_user()
    user.fb_app_id = app_id
    user.fb_app_secret = app_secret
    db.session.commit()
    return jsonify({'success': True})

@app.route('/connect/facebook')
def facebook_oauth_start():
    user = get_user()
    if not user.fb_app_id or not user.fb_app_secret:
        flash('Save your App ID and Secret first.', 'warning')
        return redirect('/')
    redirect_uri = request.host_url.rstrip('/') + '/connect/facebook/callback'
    fb_url = (
        f'https://www.facebook.com/v19.0/dialog/oauth?'
        f'client_id={user.fb_app_id}&redirect_uri={redirect_uri}'
        f'&scope=pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_metadata'
        f'&response_type=code'
    )
    return redirect(fb_url)

@app.route('/connect/facebook/callback')
def facebook_oauth_callback():
    code = request.args.get('code')
    if not code:
        return 'Authorization failed', 400
    user = get_user()
    if not user.fb_app_id or not user.fb_app_secret:
        return 'App credentials missing', 400
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        'client_id': user.fb_app_id,
        'client_secret': user.fb_app_secret,
        'redirect_uri': request.host_url.rstrip('/') + '/connect/facebook/callback',
        'code': code
    }
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        return f"Failed to get token: {data['error']['message']}", 400
    short_token = data.get('access_token')
    try:
        long_token = exchange_long_lived_token(short_token)
        user_info = fetch_facebook_user(long_token)
        pages = fetch_pages(long_token)
        user.fb_long_lived_token = long_token
        Page.query.delete()
        for p in pages:
            page = Page(
                id=p['id'], name=p['name'],
                access_token=p['access_token'],
                fan_count=p.get('fan_count', 0),
                picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                tasks=json.dumps(p.get('tasks', []))
            )
            db.session.add(page)
        db.session.commit()
        return redirect('/?connected=true')
    except Exception as e:
        return str(e), 400

@app.route('/api/me')
def api_me():
    user = get_user()
    if not user.fb_long_lived_token:
        return jsonify({'authenticated': False})
    try:
        profile = fetch_facebook_user(user.fb_long_lived_token)
        return jsonify({'authenticated': True, 'user': profile, 'pages_count': len(Page.query.all())})
    except:
        return jsonify({'authenticated': False})

# ---------- Pages ----------
@app.route('/api/pages')
def get_pages():
    pages = Page.query.all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'fan_count': p.fan_count,
        'picture_url': p.picture_url
    } for p in pages])

# ---------- Media Library ----------
@app.route('/api/media', methods=['GET'])
def get_media():
    folder = request.args.get('folder', '')
    query = MediaFile.query.order_by(MediaFile.upload_date.desc())
    if folder:
        query = query.filter_by(folder=folder)
    media = query.all()
    return jsonify([{
        'id': m.id, 'filename': m.filename, 'original_name': m.original_name,
        'upload_date': m.upload_date.isoformat(), 'file_size': m.file_size,
        'folder': m.folder, 'url': f'/uploads/{m.filename}'
    } for m in media])

@app.route('/api/media/folders', methods=['GET'])
def get_folders():
    folders = db.session.query(MediaFile.folder).distinct().all()
    return jsonify([f[0] for f in folders])

@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file'}), 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    folder = request.form.get('folder', 'My Videos')
    media = MediaFile(
        filename=filename, original_name=file.filename,
        file_size=os.path.getsize(filepath), folder=folder
    )
    db.session.add(media)
    db.session.commit()
    return jsonify({'success': True, 'id': media.id})

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    media = db.session.get(MediaFile, media_id)
    if media:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        db.session.delete(media)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/media/<int:media_id>/assign', methods=['POST'])
def assign_media(media_id):
    data = request.json
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    media.folder = data.get('page_id', data.get('folder', 'My Videos'))
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/media/<int:media_id>/rename', methods=['POST'])
def rename_media(media_id):
    data = request.json
    new_name = data.get('new_name', '').strip()
    if not new_name:
        return jsonify({'error': 'New name required'}), 400
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify({'error': 'Not found'}), 404
    media.original_name = new_name
    db.session.commit()
    return jsonify({'success': True})

# ---------- Scheduling & Publishing ----------
@app.route('/api/schedule', methods=['POST'])
def schedule_post():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    scheduled_time_str = data.get('scheduled_time')
    if not page_id or not media_id or not scheduled_time_str:
        return jsonify({'error': 'Missing fields'}), 400
    try:
        scheduled_time = datetime.datetime.fromisoformat(scheduled_time_str)
        if scheduled_time <= datetime.datetime.utcnow():
            return jsonify({'error': 'Scheduled time must be in the future'}), 400
    except:
        return jsonify({'error': 'Invalid datetime format'}), 400
    post = ScheduledPost(
        page_id=page_id, media_id=media_id, caption=caption,
        scheduled_time=scheduled_time, status='pending'
    )
    db.session.add(post)
    db.session.commit()
    schedule_job(post)
    return jsonify({'success': True, 'id': post.id})

@app.route('/api/publish-now', methods=['POST'])
def publish_now():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    if not page_id or not media_id:
        return jsonify({'error': 'Missing fields'}), 400
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify({'error': 'Media not found'}), 404
    try:
        video_id = upload_to_facebook(
            page_id, os.path.join(app.config['UPLOAD_FOLDER'], media.filename),
            title=caption, description='', video_state='PUBLISHED'
        )
        # Record as published
        post = ScheduledPost(
            page_id=page_id, media_id=media_id, caption=caption,
            scheduled_time=datetime.datetime.utcnow(), status='published'
        )
        db.session.add(post)
        db.session.commit()
        return jsonify({'success': True, 'video_id': video_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scheduled-posts', methods=['GET'])
def get_scheduled():
    posts = ScheduledPost.query.order_by(ScheduledPost.scheduled_time).all()
    return jsonify([{
        'id': p.id, 'page_id': p.page_id, 'media_id': p.media_id,
        'caption': p.caption, 'scheduled_time': p.scheduled_time.isoformat(),
        'status': p.status, 'created_at': p.created_at.isoformat()
    } for p in posts])

@app.route('/api/scheduled-posts/<int:post_id>', methods=['DELETE'])
def delete_scheduled(post_id):
    post = db.session.get(ScheduledPost, post_id)
    if post and post.status == 'pending':
        if post.job_id:
            try:
                scheduler.remove_job(post.job_id)
            except:
                pass
        db.session.delete(post)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'Cannot delete'}), 400

# ---------- Analytics ----------
@app.route('/api/analytics/<page_id>')
def analytics(page_id):
    token = get_page_token(page_id)
    if not token:
        return jsonify({'error': 'No token'}), 400
    metrics = 'page_fans,page_impressions,page_engaged_users,page_video_views,page_post_engagements'
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    params = {'metric': metrics, 'access_token': token}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        return jsonify({'error': data['error']['message']}), 400
    result = {}
    for metric in data.get('data', []):
        name = metric['name']
        values = metric['values']
        result[name] = [{'date': v.get('end_time',''), 'value': v.get('value',0)} for v in values]
    return jsonify(result)

# ---------- AI Caption & Hashtags ----------
@app.route('/api/ai-caption', methods=['POST'])
def ai_caption():
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    data = request.json
    prompt = data.get('prompt', '')
    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":f"Generate a viral Facebook Reels caption and 10 trending hashtags for: {prompt}. Return as JSON with keys 'caption' and 'hashtags'."}],
            max_tokens=250
        )
        content = response.choices[0].message.content.strip()
        # Try to parse JSON; fallback to raw text
        try:
            result = json.loads(content)
            return jsonify(result)
        except:
            return jsonify({'caption': content, 'hashtags': ''})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai-hashtags', methods=['POST'])
def ai_hashtags():
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    data = request.json
    topic = data.get('topic', '')
    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":f"Generate 20 trending Facebook Reels hashtags about {topic}. Return them as a comma‑separated list."}],
            max_tokens=150
        )
        hashtags = response.choices[0].message.content.strip()
        return jsonify({'hashtags': hashtags})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------- Trending Intelligence ----------
@app.route('/api/trending', methods=['GET'])
def trending():
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    # Use AI to suggest trending topics (since Facebook doesn't offer a direct trending API)
    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":"List 10 trending topics for Facebook Reels right now, along with 3 hashtags each. Return as a JSON array of objects with 'topic' and 'hashtags'."}],
            max_tokens=500
        )
        content = response.choices[0].message.content.strip()
        try:
            trends = json.loads(content)
            return jsonify(trends)
        except:
            return jsonify({'raw': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------- Settings ----------
@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    user = get_user()
    if request.method == 'GET':
        return jsonify({
            'fb_app_id': user.fb_app_id,
            'fb_app_secret': user.fb_app_secret,
            'groq_api_key': user.groq_api_key,
            'telegram_bot_token': user.telegram_bot_token,
            'telegram_chat_id': user.telegram_chat_id,
            'timezone': user.timezone,
            'auto_upload': user.auto_upload
        })
    else:
        data = request.json
        user.fb_app_id = data.get('fb_app_id', user.fb_app_id)
        user.fb_app_secret = data.get('fb_app_secret', user.fb_app_secret)
        user.groq_api_key = data.get('groq_api_key', user.groq_api_key)
        user.telegram_bot_token = data.get('telegram_bot_token', user.telegram_bot_token)
        user.telegram_chat_id = data.get('telegram_chat_id', user.telegram_chat_id)
        user.timezone = data.get('timezone', user.timezone)
        user.auto_upload = data.get('auto_upload', user.auto_upload)
        db.session.commit()
        return jsonify({'success': True})

# ---------- Init DB ----------
with app.app_context():
    db.create_all()
    # Reschedule pending posts
    pending = ScheduledPost.query.filter_by(status='pending').all()
    for post in pending:
        if post.scheduled_time > datetime.datetime.utcnow():
            schedule_job(post)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
