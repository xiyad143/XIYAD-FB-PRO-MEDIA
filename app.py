import os, json, requests, datetime
from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, flash
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

class SchedulerConfig:
    SCHEDULER_API_ENABLED = True
    SCHEDULER_TIMEZONE = "UTC"
app.config.from_object(SchedulerConfig())

scheduler = APScheduler()
scheduler.init_app(app)

# ---------- Models ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_app_id = db.Column(db.String(100), default='')
    fb_app_secret = db.Column(db.String(100), default='')
    fb_long_lived_token = db.Column(db.Text, nullable=True)
    fb_user_name = db.Column(db.String(200))
    fb_user_picture = db.Column(db.Text)
    groq_api_key = db.Column(db.String(100), nullable=True)
    timezone = db.Column(db.String(50), default='UTC')
    developer_name = db.Column(db.String(200), default='XIYAD Team')
    developer_email = db.Column(db.String(200))
    developer_phone = db.Column(db.String(50))
    developer_facebook = db.Column(db.String(200))
    developer_whatsapp = db.Column(db.String(50))
    developer_telegram = db.Column(db.String(100))
    developer_website = db.Column(db.String(200))

class Page(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    name = db.Column(db.String(200))
    access_token = db.Column(db.Text)
    fan_count = db.Column(db.Integer, default=0)
    picture_url = db.Column(db.Text)
    category = db.Column(db.String(200))
    new_followers = db.Column(db.Integer, default=0)
    reach = db.Column(db.Integer, default=0)
    engagement = db.Column(db.Integer, default=0)

class PageSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), db.ForeignKey('page.id'))
    post_times = db.Column(db.Text)  # JSON list of times
    auto_publish = db.Column(db.Boolean, default=False)

class MediaFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    original_name = db.Column(db.String(200))
    upload_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    file_size = db.Column(db.Integer, default=0)
    file_type = db.Column(db.String(10), default='video')
    folder = db.Column(db.String(100), default='My Videos')
    page_id = db.Column(db.String(50), nullable=True)

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), nullable=False)
    media_id = db.Column(db.Integer, db.ForeignKey('media_file.id'))
    caption = db.Column(db.Text)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    job_id = db.Column(db.String(100), nullable=True)

class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    event = db.Column(db.String(200))
    details = db.Column(db.Text)
    level = db.Column(db.String(20), default='info')

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    message = db.Column(db.Text)
    read = db.Column(db.Boolean, default=False)

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
    return None

def exchange_long_lived_token(short_token):
    user = get_user()
    if not user.fb_app_id or not user.fb_app_secret:
        raise Exception('App credentials not set')
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
        'id': data['id'],
        'name': data['name'],
        'picture': data.get('picture', {}).get('data', {}).get('url', '')
    }

def fetch_pages(token):
    url = "https://graph.facebook.com/v19.0/me/accounts"
    params = {'access_token': token, 'fields': 'id,name,access_token,fan_count,picture,category'}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('data', [])

def fetch_page_insights(page_id, token):
    metrics = 'page_fans,page_impressions,page_engaged_users,page_video_views,page_fan_adds'
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    params = {'metric': metrics, 'access_token': token}
    resp = requests.get(url, params=params)
    data = resp.json()
    insights = {}
    if 'data' in data:
        for m in data['data']:
            name = m['name']
            values = m['values']
            latest = values[-1]['value'] if values else 0
            insights[name] = latest
    return insights

def post_to_facebook(page_id, filepath, caption='', media_type='video', scheduled_time=None):
    token = get_page_token(page_id)
    if not token:
        raise Exception('No page token')
    if media_type == 'video':
        return upload_video_reel(page_id, filepath, caption, token, scheduled_time)
    else:
        return upload_photo(page_id, filepath, caption, token)

def upload_video_reel(page_id, filepath, description, token, scheduled_time=None):
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
        'video_id': video_id, 'description': description,
        'video_state': 'PUBLISHED'
    }
    if scheduled_time:
        finish_params['scheduled_publish_time'] = scheduled_time
        finish_params['video_state'] = 'SCHEDULED'
    finish_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data:
        raise Exception(finish_data['error']['message'])
    return finish_data.get('id')

def upload_photo(page_id, filepath, caption, token):
    url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
    with open(filepath, 'rb') as f:
        files = {'source': f}
        params = {'access_token': token, 'caption': caption}
        resp = requests.post(url, files=files, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('id')

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
            post_to_facebook(post.page_id, filepath, post.caption or '', media_type=media.file_type)
            post.status = 'published'
            log_event(f"Scheduled post #{post.id} published")
        except Exception as e:
            post.status = 'failed'
            log_event(f"Failed post #{post.id}: {str(e)}", level='error')
        finally:
            db.session.commit()

def schedule_job(post):
    job = scheduler.add_job(
        id=f'post_{post.id}',
        func=execute_scheduled_post,
        args=[post.id],
        trigger='date',
        run_date=post.scheduled_time,
        replace_existing=True
    )
    post.job_id = job.id
    db.session.commit()

def log_event(event, level='info'):
    log = ActivityLog(event=event, level=level)
    db.session.add(log)
    db.session.commit()

def add_notification(message):
    notif = Notification(message=message)
    db.session.add(notif)
    db.session.commit()

# Auto-publisher (runs every minute)
def auto_publisher():
    with scheduler.app.app_context():
        now = datetime.datetime.utcnow()
        current_time_str = now.strftime('%H:%M')
        schedules = PageSchedule.query.filter_by(auto_publish=True).all()
        for sched in schedules:
            times = json.loads(sched.post_times) if sched.post_times else []
            if current_time_str in times:
                # Check if already published this hour
                recent = ScheduledPost.query.filter(
                    ScheduledPost.page_id == sched.page_id,
                    ScheduledPost.status.in_(['published', 'processing']),
                    ScheduledPost.scheduled_time >= now.replace(minute=0, second=0, microsecond=0)
                ).first()
                if recent:
                    continue
                # Pick next media assigned to this page
                media = MediaFile.query.filter_by(page_id=sched.page_id, file_type='video').order_by(MediaFile.upload_date.asc()).first()
                if not media:
                    media = MediaFile.query.filter_by(page_id=sched.page_id, file_type='photo').first()
                if media:
                    new_post = ScheduledPost(
                        page_id=sched.page_id,
                        media_id=media.id,
                        caption=media.original_name,
                        scheduled_time=now,
                        status='pending'
                    )
                    db.session.add(new_post)
                    db.session.commit()
                    schedule_job(new_post)
                    add_notification(f"Auto-published media #{media.id} on page {sched.page_id}")

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Auth
@app.route('/api/connect/token', methods=['POST'])
def connect_with_token():
    token = request.json.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 400
    try:
        user_info = fetch_facebook_user(token)
        pages = fetch_pages(token)
        user = get_user()
        user.fb_long_lived_token = token
        user.fb_user_name = user_info['name']
        user.fb_user_picture = user_info['picture']
        Page.query.delete()
        for p in pages:
            page = Page(
                id=p['id'], name=p['name'], access_token=p['access_token'],
                fan_count=p.get('fan_count', 0),
                picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                category=p.get('category', '')
            )
            db.session.add(page)
        db.session.commit()
        log_event("User connected via token")
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
        return redirect(url_for('index'))
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
        return f"Error: {data['error']['message']}", 400
    short_token = data['access_token']
    try:
        long_token = exchange_long_lived_token(short_token)
        user_info = fetch_facebook_user(long_token)
        pages = fetch_pages(long_token)
        user.fb_long_lived_token = long_token
        user.fb_user_name = user_info['name']
        user.fb_user_picture = user_info['picture']
        Page.query.delete()
        for p in pages:
            page = Page(
                id=p['id'], name=p['name'], access_token=p['access_token'],
                fan_count=p.get('fan_count', 0),
                picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                category=p.get('category', '')
            )
            db.session.add(page)
        db.session.commit()
        flash('Connected successfully!', 'success')
        return redirect(url_for('index'))
    except Exception as e:
        flash(str(e), 'danger')
        return redirect(url_for('index'))

@app.route('/api/me')
def api_me():
    user = get_user()
    if not user.fb_long_lived_token:
        return jsonify({'authenticated': False})
    return jsonify({
        'authenticated': True,
        'user': {
            'name': user.fb_user_name,
            'picture': user.fb_user_picture
        },
        'pages_count': Page.query.count()
    })

@app.route('/api/profile')
def api_profile():
    user = get_user()
    if not user.fb_long_lived_token:
        return jsonify({'error': 'Not connected'}), 400
    return jsonify({
        'name': user.fb_user_name,
        'picture': user.fb_user_picture,
        'connected_date': datetime.datetime.utcnow().isoformat(),
        'token_status': 'Active',
        'pages_count': Page.query.count()
    })

@app.route('/api/pages')
def get_pages():
    pages = Page.query.all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'fan_count': p.fan_count,
        'picture_url': p.picture_url, 'category': p.category,
        'new_followers': p.new_followers, 'reach': p.reach,
        'engagement': p.engagement
    } for p in pages])

@app.route('/api/pages/insights')
def update_page_insights():
    pages = Page.query.all()
    for p in pages:
        try:
            insights = fetch_page_insights(p.id, p.access_token)
            p.new_followers = insights.get('page_fan_adds', 0)
            p.reach = insights.get('page_impressions', 0)
            p.engagement = insights.get('page_engaged_users', 0)
        except:
            pass
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/page/<page_id>/schedule', methods=['GET', 'POST'])
def page_schedule(page_id):
    sched = PageSchedule.query.filter_by(page_id=page_id).first()
    if request.method == 'GET':
        if sched:
            return jsonify({'post_times': json.loads(sched.post_times) if sched.post_times else [], 'auto_publish': sched.auto_publish})
        return jsonify({'post_times': [], 'auto_publish': False})
    else:
        data = request.json
        if not sched:
            sched = PageSchedule(page_id=page_id)
            db.session.add(sched)
        sched.post_times = json.dumps(data.get('post_times', []))
        sched.auto_publish = data.get('auto_publish', False)
        db.session.commit()
        return jsonify(success=True)

# Media
@app.route('/api/media', methods=['GET'])
def get_media():
    folder = request.args.get('folder', '')
    file_type = request.args.get('type', '')
    query = MediaFile.query.order_by(MediaFile.upload_date.desc())
    if folder:
        query = query.filter_by(folder=folder)
    if file_type:
        query = query.filter_by(file_type=file_type)
    media = query.all()
    return jsonify([{
        'id': m.id, 'filename': m.filename, 'original_name': m.original_name,
        'upload_date': m.upload_date.isoformat(), 'file_size': m.file_size,
        'folder': m.folder, 'file_type': m.file_type, 'page_id': m.page_id,
        'url': f'/uploads/{m.filename}'
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
    file_type = 'video' if file.mimetype.startswith('video') else 'photo'
    media = MediaFile(
        filename=filename, original_name=file.filename,
        file_size=os.path.getsize(filepath), folder=folder,
        file_type=file_type, page_id=request.form.get('page_id')
    )
    db.session.add(media)
    db.session.commit()
    log_event(f"Uploaded {file_type} {filename}")
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
    media.page_id = data.get('page_id')
    media.folder = data.get('folder', media.folder)
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

# Publish
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
        result_id = post_to_facebook(page_id, os.path.join(app.config['UPLOAD_FOLDER'], media.filename), caption, media.file_type)
        post = ScheduledPost(page_id=page_id, media_id=media_id, caption=caption,
                             scheduled_time=datetime.datetime.utcnow(), status='published')
        db.session.add(post)
        db.session.commit()
        log_event(f"Published {media.file_type} #{media_id}")
        return jsonify({'success': True, 'id': result_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
            return jsonify({'error': 'Must be in the future'}), 400
    except:
        return jsonify({'error': 'Invalid datetime'}), 400
    post = ScheduledPost(page_id=page_id, media_id=media_id, caption=caption,
                         scheduled_time=scheduled_time, status='pending')
    db.session.add(post)
    db.session.commit()
    schedule_job(post)
    return jsonify({'success': True, 'id': post.id})

@app.route('/api/scheduled-posts')
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

# AI
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

@app.route('/api/ai-caption', methods=['POST'])
def ai_caption():
    if not OPENAI_AVAILABLE:
        return jsonify({'error': 'OpenAI library not installed'}), 500
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    data = request.json
    prompt = data.get('prompt', '')
    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":f"Generate a viral Facebook Reels caption and 10 trending hashtags for: {prompt}. Return JSON with keys 'caption' and 'hashtags'."}],
            max_tokens=250
        )
        content = response.choices[0].message.content.strip()
        try:
            result = json.loads(content)
            return jsonify(result)
        except:
            return jsonify({'caption': content, 'hashtags': ''})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trending')
def trending():
    if not OPENAI_AVAILABLE:
        return jsonify({'error': 'OpenAI library not installed'}), 500
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":"List 10 trending topics for Facebook Reels now, with 3 hashtags each. Return JSON array of objects with 'topic' and 'hashtags'."}],
            max_tokens=500
        )
        content = response.choices[0].message.content.strip()
        try:
            return jsonify(json.loads(content))
        except:
            return jsonify({'raw': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Analytics
@app.route('/api/analytics/<page_id>')
def analytics(page_id):
    token = get_page_token(page_id)
    if not token:
        return jsonify({'error': 'No token'}), 400
    metrics = 'page_fans,page_impressions,page_engaged_users,page_video_views,page_fan_adds'
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    params = {'metric': metrics, 'access_token': token, 'period': 'days_28'}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        return jsonify({'error': data['error']['message']}), 400
    result = {}
    for m in data.get('data', []):
        result[m['name']] = [{'date': v.get('end_time',''), 'value': v.get('value',0)} for v in m['values']]
    return jsonify(result)

# Notifications
@app.route('/api/notifications')
def get_notifications():
    notifs = Notification.query.order_by(Notification.timestamp.desc()).limit(20).all()
    return jsonify([{'id': n.id, 'message': n.message, 'timestamp': n.timestamp.isoformat(), 'read': n.read} for n in notifs])

# Logs
@app.route('/api/logs')
def get_logs():
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(50).all()
    return jsonify([{'timestamp': l.timestamp.isoformat(), 'event': l.event, 'level': l.level} for l in logs])

# Settings
@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    user = get_user()
    if request.method == 'GET':
        return jsonify({
            'fb_app_id': user.fb_app_id,
            'fb_app_secret': user.fb_app_secret,
            'groq_api_key': user.groq_api_key,
            'timezone': user.timezone,
            'developer_name': user.developer_name,
            'developer_email': user.developer_email,
            'developer_phone': user.developer_phone,
            'developer_facebook': user.developer_facebook,
            'developer_whatsapp': user.developer_whatsapp,
            'developer_telegram': user.developer_telegram,
            'developer_website': user.developer_website
        })
    else:
        data = request.json
        for field in ['fb_app_id','fb_app_secret','groq_api_key','timezone','developer_name','developer_email','developer_phone','developer_facebook','developer_whatsapp','developer_telegram','developer_website']:
            if field in data:
                setattr(user, field, data[field])
        db.session.commit()
        return jsonify({'success': True})

# ---------- Init ----------
with app.app_context():
    db.create_all()
    # Reschedule pending posts
    pending = ScheduledPost.query.filter_by(status='pending').all()
    for post in pending:
        if post.scheduled_time > datetime.datetime.utcnow():
            schedule_job(post)
    # Start scheduler
    if not scheduler.running:
        scheduler.start()
    # Auto-publisher every minute
    scheduler.add_job(
        id='auto_publisher',
        func=auto_publisher,
        trigger='interval',
        minutes=1,
        replace_existing=True
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
