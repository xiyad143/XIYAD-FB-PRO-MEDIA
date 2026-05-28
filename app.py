import os, json, requests, datetime, time, re, hashlib, base64
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from openai import OpenAI
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'xmedia-pro-secret-change-in-production')
CORS(app)

basedir = os.path.abspath(os.path.dirname(__file__))
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

# Encryption for credentials
encryption_key = os.environ.get('ENCRYPTION_KEY', Fernet.generate_key().decode())
cipher = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)

# Cloudinary config (set via environment variables)
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# ---------- Models ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_app_id_enc = db.Column(db.Text, default='')
    fb_app_secret_enc = db.Column(db.Text, default='')
    fb_long_lived_token_enc = db.Column(db.Text, nullable=True)
    fb_pages_json = db.Column(db.Text, nullable=True)
    groq_api_key_enc = db.Column(db.Text, nullable=True)
    telegram_bot_token_enc = db.Column(db.Text, nullable=True)
    telegram_chat_id_enc = db.Column(db.Text, nullable=True)
    timezone = db.Column(db.String(50), default='UTC')
    auto_upload = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(20), default='light')

    @property
    def fb_app_id(self):
        return cipher.decrypt(self.fb_app_id_enc.encode()).decode() if self.fb_app_id_enc else ''
    @fb_app_id.setter
    def fb_app_id(self, value):
        self.fb_app_id_enc = cipher.encrypt(value.encode()).decode() if value else ''

    @property
    def fb_app_secret(self):
        return cipher.decrypt(self.fb_app_secret_enc.encode()).decode() if self.fb_app_secret_enc else ''
    @fb_app_secret.setter
    def fb_app_secret(self, value):
        self.fb_app_secret_enc = cipher.encrypt(value.encode()).decode() if value else ''

    @property
    def fb_long_lived_token(self):
        return cipher.decrypt(self.fb_long_lived_token_enc.encode()).decode() if self.fb_long_lived_token_enc else None
    @fb_long_lived_token.setter
    def fb_long_lived_token(self, value):
        self.fb_long_lived_token_enc = cipher.encrypt(value.encode()).decode() if value else None

    @property
    def groq_api_key(self):
        return cipher.decrypt(self.groq_api_key_enc.encode()).decode() if self.groq_api_key_enc else ''
    @groq_api_key.setter
    def groq_api_key(self, value):
        self.groq_api_key_enc = cipher.encrypt(value.encode()).decode() if value else ''

    @property
    def telegram_bot_token(self):
        return cipher.decrypt(self.telegram_bot_token_enc.encode()).decode() if self.telegram_bot_token_enc else ''
    @telegram_bot_token.setter
    def telegram_bot_token(self, value):
        self.telegram_bot_token_enc = cipher.encrypt(value.encode()).decode() if value else ''

    @property
    def telegram_chat_id(self):
        return cipher.decrypt(self.telegram_chat_id_enc.encode()).decode() if self.telegram_chat_id_enc else ''
    @telegram_chat_id.setter
    def telegram_chat_id(self, value):
        self.telegram_chat_id_enc = cipher.encrypt(value.encode()).decode() if value else ''

class Page(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    name = db.Column(db.String(200))
    access_token_enc = db.Column(db.Text)
    fan_count = db.Column(db.Integer, default=0)
    picture_url = db.Column(db.Text)
    tasks = db.Column(db.Text)
    connected = db.Column(db.Boolean, default=True)

    @property
    def access_token(self):
        return cipher.decrypt(self.access_token_enc.encode()).decode() if self.access_token_enc else None
    @access_token.setter
    def access_token(self, value):
        self.access_token_enc = cipher.encrypt(value.encode()).decode() if value else None

class MediaFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cloudinary_public_id = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text)
    original_name = db.Column(db.String(200))
    upload_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    file_size = db.Column(db.Integer, default=0)
    duration = db.Column(db.Float, default=0)  # seconds
    thumbnail_url = db.Column(db.Text)
    folder = db.Column(db.String(100), default='My Videos')
    used = db.Column(db.Boolean, default=False)  # for auto-select

class Workflow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200), default='Default')
    enabled = db.Column(db.Boolean, default=True)
    days_of_week = db.Column(db.String(50), default='0,1,2,3,4,5,6')  # comma separated
    posting_times_json = db.Column(db.Text, default='["08:00","12:00","16:00","20:00"]')
    caption_template = db.Column(db.Text, default='{caption}')
    hashtag_template = db.Column(db.Text, default='')
    auto_select_mode = db.Column(db.String(20), default='sequential')  # sequential, random
    repeat = db.Column(db.Boolean, default=True)
    timezone = db.Column(db.String(50), default='UTC')
    last_post_at = db.Column(db.DateTime, nullable=True)

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey('workflow.id'), nullable=True)
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
    if page and page.connected:
        return page.access_token
    return None

def exchange_long_lived_token(short_token):
    user = get_user()
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        'grant_type': 'fb_exchange_token',
        'client_id': user.fb_app_id,
        'client_secret': user.fb_app_secret,
        'fb_exchange_token': short_token
    }
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data: raise Exception(data['error']['message'])
    return data.get('access_token')

def upload_to_facebook(page_id, cloudinary_url, title='', description='', video_state='PUBLISHED', scheduled_time=None):
    token = get_page_token(page_id)
    if not token: raise Exception('No page token')
    # Facebook requires a local file path; we'll download from Cloudinary to temp
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    resp = requests.get(cloudinary_url, stream=True)
    for chunk in resp.iter_content(8192):
        if chunk: temp_file.write(chunk)
    temp_file.close()
    filepath = temp_file.name
    file_size = os.path.getsize(filepath)

    # Start upload
    start_params = {'access_token': token, 'upload_phase': 'start', 'file_size': file_size}
    start_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=start_params)
    start_data = start_resp.json()
    if 'error' in start_data: raise Exception(start_data['error']['message'])

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
    os.remove(filepath)
    if upload_resp.status_code != 200: raise Exception(f'Upload failed: {upload_resp.text}')

    # Finish
    finish_params = {
        'access_token': token,
        'upload_phase': 'finish',
        'video_id': video_id,
        'title': title,
        'description': description,
        'video_state': video_state
    }
    if video_state == 'SCHEDULED' and scheduled_time:
        finish_params['scheduled_publish_time'] = scheduled_time
    finish_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data: raise Exception(finish_data['error']['message'])
    return video_id

def execute_scheduled_post(post_id):
    with scheduler.app.app_context():
        post = db.session.get(ScheduledPost, post_id)
        if not post or post.status != 'pending': return
        post.status = 'processing'
        db.session.commit()
        try:
            media = db.session.get(MediaFile, post.media_id)
            upload_to_facebook(post.page_id, media.url, title=post.caption or '', description='')
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

# ---------- Automation Engine ----------
def process_workflows():
    with scheduler.app.app_context():
        workflows = Workflow.query.filter_by(enabled=True).all()
        now = datetime.datetime.utcnow()
        for wf in workflows:
            # Check if today is in days_of_week
            days = [int(x) for x in wf.days_of_week.split(',')]
            if now.weekday() not in days:
                continue
            posting_times = json.loads(wf.posting_times_json)
            for t in posting_times:
                hour, minute = map(int, t.split(':'))
                scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if scheduled < now:
                    scheduled += datetime.timedelta(days=1)
                # Check if already scheduled for this slot today
                existing = ScheduledPost.query.filter(
                    ScheduledPost.workflow_id == wf.id,
                    ScheduledPost.scheduled_time == scheduled
                ).first()
                if existing:
                    continue
                # Auto-select a video
                media = MediaFile.query.filter_by(used=False).order_by(
                    MediaFile.upload_date if wf.auto_select_mode == 'sequential' else MediaFile.id
                ).first()
                if not media and wf.repeat:
                    # Reuse oldest used video
                    media = MediaFile.query.order_by(MediaFile.upload_date).first()
                if not media:
                    continue
                caption = wf.caption_template.replace('{caption}', media.original_name)
                if wf.hashtag_template:
                    caption += '\n' + wf.hashtag_template
                post = ScheduledPost(
                    workflow_id=wf.id,
                    page_id=wf.page_id,
                    media_id=media.id,
                    caption=caption,
                    scheduled_time=scheduled,
                    status='pending'
                )
                db.session.add(post)
                db.session.commit()
                schedule_job(post)
                media.used = True
                db.session.commit()

# Schedule the automation checker every minute
scheduler.add_job(id='workflow_engine', func=process_workflows, trigger='interval', minutes=1)

# ---------- Routes (existing + new) ----------
@app.route('/')
def index():
    return render_template('index.html')

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
            'auto_upload': user.auto_upload,
            'theme': user.theme
        })
    data = request.json
    user.fb_app_id = data.get('fb_app_id', user.fb_app_id)
    user.fb_app_secret = data.get('fb_app_secret', user.fb_app_secret)
    user.groq_api_key = data.get('groq_api_key', user.groq_api_key)
    user.telegram_bot_token = data.get('telegram_bot_token', user.telegram_bot_token)
    user.telegram_chat_id = data.get('telegram_chat_id', user.telegram_chat_id)
    user.timezone = data.get('timezone', user.timezone)
    user.auto_upload = data.get('auto_upload', user.auto_upload)
    if 'theme' in data: user.theme = data['theme']
    db.session.commit()
    return jsonify(success=True)

# Facebook Connect / Disconnect (unchanged except encryption)
@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    data = request.json
    short_token = data.get('short_lived_token')
    if not short_token: return jsonify(error='Missing token'), 400
    try:
        long_token = exchange_long_lived_token(short_token)
        user = get_user()
        user.fb_long_lived_token = long_token
        pages_url = "https://graph.facebook.com/v19.0/me/accounts"
        params = {'access_token': long_token, 'fields': 'id,name,access_token,fan_count,picture,tasks'}
        resp = requests.get(pages_url, params=params)
        pages_data = resp.json()
        if 'error' in pages_data: return jsonify(error=pages_data['error']['message']), 400
        Page.query.delete()
        for p in pages_data.get('data', []):
            page = Page(
                id=p['id'], name=p['name'],
                access_token=p['access_token'],
                fan_count=p.get('fan_count', 0),
                picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                tasks=json.dumps(p.get('tasks', []))
            )
            db.session.add(page)
        db.session.commit()
        return jsonify(success=True, pages=pages_data['data'])
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/disconnect-facebook', methods=['POST'])
def disconnect_facebook():
    user = get_user()
    user.fb_long_lived_token = None
    user.fb_pages_json = None
    Page.query.delete()
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/user-profile')
def user_profile():
    user = get_user()
    token = user.fb_long_lived_token
    if not token: return jsonify(error='Not connected'), 400
    resp = requests.get(f"https://graph.facebook.com/v19.0/me?fields=name,picture&access_token={token}")
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    return jsonify(data)

@app.route('/api/pages')
def get_pages():
    pages = Page.query.all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'fan_count': p.fan_count,
        'picture_url': p.picture_url, 'connected': p.connected,
        'access_token': p.access_token,  # careful: exposing token only for admin
        'tasks': json.loads(p.tasks or '[]')
    } for p in pages])

@app.route('/api/refresh-pages', methods=['POST'])
def refresh_pages():
    user = get_user()
    token = user.fb_long_lived_token
    if not token: return jsonify(error='Not connected'), 400
    pages_url = "https://graph.facebook.com/v19.0/me/accounts"
    params = {'access_token': token, 'fields': 'id,name,access_token,fan_count,picture,tasks'}
    resp = requests.get(pages_url, params=params)
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    existing_ids = {p.id for p in Page.query.all()}
    for p in data.get('data', []):
        if p['id'] in existing_ids:
            page = Page.query.get(p['id'])
            page.name = p['name']
            page.access_token = p['access_token']
            page.fan_count = p.get('fan_count', 0)
            page.picture_url = p.get('picture', {}).get('data', {}).get('url', '')
            page.tasks = json.dumps(p.get('tasks', []))
            page.connected = True
        else:
            page = Page(id=p['id'], name=p['name'], access_token=p['access_token'],
                        fan_count=p.get('fan_count', 0),
                        picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
                        tasks=json.dumps(p.get('tasks', [])))
            db.session.add(page)
    db.session.commit()
    return jsonify(success=True)

# Media (Cloudinary)
@app.route('/api/cloudinary-signature', methods=['POST'])
def cloudinary_signature():
    # Generate a signature for direct upload (optional)
    timestamp = int(time.time())
    params = {
        'timestamp': timestamp,
        'folder': request.json.get('folder', 'xmedia'),
    }
    signature = cloudinary.utils.api_sign_request(params, cloudinary.config().api_secret)
    return jsonify({
        'timestamp': timestamp,
        'signature': signature,
        'api_key': cloudinary.config().api_key,
        'cloud_name': cloudinary.config().cloud_name
    })

@app.route('/api/media', methods=['GET'])
def get_media():
    media = MediaFile.query.order_by(MediaFile.upload_date.desc()).all()
    return jsonify([{
        'id': m.id, 'url': m.url, 'original_name': m.original_name,
        'upload_date': m.upload_date.isoformat(), 'file_size': m.file_size,
        'duration': m.duration, 'thumbnail_url': m.thumbnail_url,
        'folder': m.folder, 'used': m.used
    } for m in media])

@app.route('/api/media/save', methods=['POST'])
def save_media():
    data = request.json
    media = MediaFile(
        cloudinary_public_id=data['public_id'],
        url=data['secure_url'],
        original_name=data.get('original_name', data['public_id']),
        file_size=data.get('bytes', 0),
        duration=data.get('duration', 0),
        thumbnail_url=data.get('thumbnail_url', ''),
        folder=data.get('folder', 'My Videos')
    )
    db.session.add(media)
    db.session.commit()
    return jsonify(success=True, id=media.id)

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    media = db.session.get(MediaFile, media_id)
    if media:
        # Delete from Cloudinary
        cloudinary.uploader.destroy(media.cloudinary_public_id, resource_type='video')
        db.session.delete(media)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(error='Not found'), 404

@app.route('/api/media/<int:media_id>/assign', methods=['POST'])
def assign_media(media_id):
    data = request.json
    media = db.session.get(MediaFile, media_id)
    if media:
        media.folder = data.get('folder', 'My Videos')
        media.used = data.get('used', media.used)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(error='Not found'), 404

# Scheduling (existing + automation)
@app.route('/api/schedule', methods=['POST'])
def schedule_post():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    scheduled_time_str = data.get('scheduled_time')
    if not page_id or not media_id or not scheduled_time_str: return jsonify(error='Missing fields'), 400
    try:
        scheduled_time = datetime.datetime.fromisoformat(scheduled_time_str)
    except:
        scheduled_time = datetime.datetime.strptime(scheduled_time_str, '%Y-%m-%dT%H:%M')
    post = ScheduledPost(
        page_id=page_id, media_id=media_id, caption=caption,
        scheduled_time=scheduled_time, status='pending'
    )
    db.session.add(post)
    db.session.commit()
    schedule_job(post)
    return jsonify(success=True, id=post.id)

@app.route('/api/scheduled-posts', methods=['GET'])
def get_scheduled():
    posts = ScheduledPost.query.order_by(ScheduledPost.scheduled_time).all()
    return jsonify([{
        'id': p.id, 'page_id': p.page_id, 'media_id': p.media_id,
        'media_filename': MediaFile.query.get(p.media_id).original_name if p.media_id else '',
        'caption': p.caption, 'scheduled_time': p.scheduled_time.isoformat(),
        'status': p.status, 'created_at': p.created_at.isoformat()
    } for p in posts])

@app.route('/api/scheduled-posts/<int:post_id>', methods=['DELETE'])
def delete_scheduled(post_id):
    post = db.session.get(ScheduledPost, post_id)
    if post and post.status == 'pending':
        if post.job_id:
            try: scheduler.remove_job(post.job_id)
            except: pass
        db.session.delete(post)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(error='Cannot delete'), 400

# Instant Publish
@app.route('/api/publish-now', methods=['POST'])
def publish_now():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    if not page_id or not media_id: return jsonify(error='Missing fields'), 400
    media = db.session.get(MediaFile, media_id)
    if not media: return jsonify(error='Media not found'), 404
    try:
        video_id = upload_to_facebook(page_id, media.url, title=caption, description='', video_state='PUBLISHED')
        post = ScheduledPost(
            page_id=page_id, media_id=media_id, caption=caption,
            scheduled_time=datetime.datetime.utcnow(), status='published'
        )
        db.session.add(post)
        db.session.commit()
        return jsonify(success=True, video_id=video_id)
    except Exception as e:
        return jsonify(error=str(e)), 500

# Analytics
@app.route('/api/analytics/<page_id>')
def analytics(page_id):
    token = get_page_token(page_id)
    if not token: return jsonify(error='No token'), 400
    metrics = 'page_fans,page_impressions,page_engaged_users,page_video_views,page_post_engagements'
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    params = {'metric': metrics, 'access_token': token, 'period': 'day'}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    result = {}
    for metric in data.get('data', []):
        name = metric['name']
        values = metric['values']
        result[name] = [{'date': v.get('end_time',''), 'value': v.get('value',0)} for v in values]
    return jsonify(result)

# AI Caption
@app.route('/api/ai-caption', methods=['POST'])
def ai_caption():
    user = get_user()
    if not user.groq_api_key: return jsonify(error='Groq key missing'), 400
    data = request.json
    prompt = data.get('prompt', '')
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":f"Generate a social media caption and 5 hashtags for: {prompt}"}],
            max_tokens=200
        )
        caption = response.choices[0].message.content.strip()
        return jsonify(caption=caption)
    except Exception as e:
        return jsonify(error=str(e)), 500

# Workflow endpoints
@app.route('/api/workflows', methods=['GET', 'POST'])
def workflows():
    if request.method == 'GET':
        wfs = Workflow.query.all()
        return jsonify([{
            'id': w.id, 'page_id': w.page_id, 'name': w.name,
            'enabled': w.enabled, 'days_of_week': w.days_of_week,
            'posting_times': json.loads(w.posting_times_json),
            'caption_template': w.caption_template,
            'hashtag_template': w.hashtag_template,
            'auto_select_mode': w.auto_select_mode,
            'repeat': w.repeat,
            'timezone': w.timezone
        } for w in wfs])
    data = request.json
    wf = Workflow(
        page_id=data['page_id'],
        name=data.get('name', 'Default'),
        days_of_week=data.get('days_of_week', '0,1,2,3,4,5,6'),
        posting_times_json=json.dumps(data.get('posting_times', ['08:00','12:00','16:00','20:00'])),
        caption_template=data.get('caption_template', '{caption}'),
        hashtag_template=data.get('hashtag_template', ''),
        auto_select_mode=data.get('auto_select_mode', 'sequential'),
        repeat=data.get('repeat', True),
        timezone=data.get('timezone', 'UTC')
    )
    db.session.add(wf)
    db.session.commit()
    return jsonify(success=True, id=wf.id)

@app.route('/api/workflows/<int:workflow_id>', methods=['PUT', 'DELETE'])
def modify_workflow(workflow_id):
    wf = db.session.get(Workflow, workflow_id)
    if not wf: return jsonify(error='Not found'), 404
    if request.method == 'DELETE':
        db.session.delete(wf)
        db.session.commit()
        return jsonify(success=True)
    data = request.json
    wf.page_id = data.get('page_id', wf.page_id)
    wf.name = data.get('name', wf.name)
    wf.days_of_week = data.get('days_of_week', wf.days_of_week)
    wf.posting_times_json = json.dumps(data.get('posting_times', json.loads(wf.posting_times_json)))
    wf.caption_template = data.get('caption_template', wf.caption_template)
    wf.hashtag_template = data.get('hashtag_template', wf.hashtag_template)
    wf.auto_select_mode = data.get('auto_select_mode', wf.auto_select_mode)
    wf.repeat = data.get('repeat', wf.repeat)
    wf.timezone = data.get('timezone', wf.timezone)
    wf.enabled = data.get('enabled', wf.enabled)
    db.session.commit()
    return jsonify(success=True)

# Live streaming (stub – Facebook API for live video is complex, here we provide UI + session creation)
@app.route('/api/live/create', methods=['POST'])
def create_live():
    page_id = request.json.get('page_id')
    title = request.json.get('title', 'Live Stream')
    description = request.json.get('description', '')
    token = get_page_token(page_id)
    if not token: return jsonify(error='No page token'), 400
    # Create a live video object
    url = f"https://graph.facebook.com/v19.0/{page_id}/live_videos"
    params = {
        'access_token': token,
        'title': title,
        'description': description,
        'status': 'LIVE_NOW'  # or 'SCHEDULED'
    }
    resp = requests.post(url, params=params)
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    return jsonify(data)  # contains stream_url, etc.

# Theme
@app.route('/api/theme', methods=['POST'])
def save_theme():
    user = get_user()
    user.theme = request.json.get('theme', 'light')
    db.session.commit()
    return jsonify(success=True)

# Run
with app.app_context():
    db.create_all()
    # Reschedule pending posts
    pending = ScheduledPost.query.filter_by(status='pending').all()
    for post in pending:
        if post.scheduled_time > datetime.datetime.utcnow():
            schedule_job(post)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
