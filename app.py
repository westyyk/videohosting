from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
from datetime import datetime
import os
import time
import threading

# yt-dlp (pip install yt-dlp)
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

DB = "tasks.db"
app = Flask(__name__)
app.secret_key = "change_this_secret_in_prod"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2048 * 2048 * 2048  # 1 ГБ

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'webm', 'mkv', 'avi'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# БД
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        category_id INTEGER,
        deadline TEXT,
        completed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    );

    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT NOT NULL,
        title TEXT,
        description TEXT,
        path TEXT,
        uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        -- Поля для импорта (NULL у обычных загрузок)
        source_url TEXT,
        platform TEXT,
        thumbnail TEXT,
        duration INTEGER,
        import_status TEXT DEFAULT 'ready',
        import_error TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL,
        user_id INTEGER,
        text TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(video_id) REFERENCES videos(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    db.commit()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()


def get_video_comments(db, video_id):
    return db.execute("""
        SELECT c.id, c.text, c.created_at, u.username AS author
        FROM comments c
        LEFT JOIN users u ON c.user_id = u.id
        WHERE c.video_id = ?
        ORDER BY c.created_at ASC
    """, (video_id,)).fetchall()


@app.before_request
def before_request():
    init_db()
    g.user = current_user()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Вспомогательные функции для импорта
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    u = url.lower()
    if "rutube.ru"   in u: return "Rutube"
    if "youtube.com" in u or "youtu.be" in u: return "YouTube"
    if "vk.com"      in u or "vkvideo.ru" in u: return "VK Video"
    if "vimeo.com"   in u: return "Vimeo"
    if "dailymotion" in u: return "Dailymotion"
    if "tiktok.com"  in u: return "TikTok"
    return "Другое"


def format_duration(seconds):
    if not seconds:
        return ""
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def download_video_thread(video_id: int, url: str):
    """
    Скачивает видео в фоновом потоке.
    Сохраняет файл в ту же папку uploads/, обновляет запись в БД.
    """
    conn = sqlite3.connect(DB)

    def set_status(status, filename=None, error=None):
        if filename:
            conn.execute(
                "UPDATE videos SET import_status=?, filename=?, path=?, import_error=? WHERE id=?",
                (status, filename, f"/uploads/{filename}", error, video_id)
            )
        else:
            conn.execute(
                "UPDATE videos SET import_status=?, import_error=? WHERE id=?",
                (status, error, video_id)
            )
        conn.commit()

    unique = f"import_{video_id}_{int(time.time())}"
    out_tmpl = os.path.join(UPLOAD_FOLDER, f"{unique}_%(title)s.%(ext)s")

    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        set_status("downloading")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            full_path = ydl.prepare_filename(info)
            if not os.path.exists(full_path):
                full_path = full_path.rsplit(".", 1)[0] + ".mp4"

        filename = os.path.basename(full_path)
        set_status("ready", filename=filename)

    except Exception as e:
        set_status("error", error=str(e)[:500])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if not username or not password:
            flash("Введите имя пользователя и пароль", "danger")
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Имя пользователя уже занято", "danger")
            return redirect(url_for("register"))
        flash("Регистрация прошла успешно", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash("Вы вошли", "success")
            return redirect(url_for("index"))
        flash("Неверные данные", "danger")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли", "info")
    return redirect(url_for("index"))


@app.route("/agreement")
def agreement():
    return render_template("agreement.html")


# ---------------------------------------------------------------------------
# Профиль
# ---------------------------------------------------------------------------

@app.route("/profile")
def profile():
    user = g.user
    db = get_db()
    if not user:
        user_id = None
        user_row = None
    else:
        user_id = user["id"]
        user_row = user

    if user_id:
        user_videos = db.execute("""
            SELECT v.id, v.filename, v.title, v.description, v.path, v.uploaded_at,
                   u.username AS uploaded_by, v.user_id,
                   v.source_url, v.platform, v.thumbnail, v.duration,
                   v.import_status, v.import_error
            FROM videos v
            LEFT JOIN users u ON v.user_id = u.id
            WHERE v.user_id = ?
            ORDER BY v.uploaded_at DESC
        """, (user_id,)).fetchall()
    else:
        user_videos = []

    videos_with_comments = []
    for vid in user_videos:
        vid_dict = dict(vid)
        vid_dict["comments"] = get_video_comments(db, vid_dict["id"])
        videos_with_comments.append(vid_dict)

    is_owner = True if user else False
    return render_template("profile.html", user=user_row, videos=videos_with_comments,
                           is_owner=is_owner, format_duration=format_duration)


@app.route("/profile/<int:user_id>")
def profile_by_id(user_id):
    db = get_db()
    user = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("Пользователь не найден", "danger")
        return redirect(url_for("index"))

    user_videos = db.execute("""
        SELECT v.id, v.filename, v.title, v.description, v.path, v.uploaded_at,
               u.username AS uploaded_by, v.user_id,
               v.source_url, v.platform, v.thumbnail, v.duration,
               v.import_status, v.import_error
        FROM videos v
        LEFT JOIN users u ON v.user_id = u.id
        WHERE v.user_id = ?
        ORDER BY v.uploaded_at DESC
    """, (user_id,)).fetchall()

    videos_with_comments = []
    for vid in user_videos:
        vid_dict = dict(vid)
        vid_dict["comments"] = get_video_comments(db, vid_dict["id"])
        videos_with_comments.append(vid_dict)

    is_owner = g.user and (g.user["id"] == user_id)
    return render_template("profile.html", user=user, videos=videos_with_comments,
                           is_owner=is_owner, format_duration=format_duration)


# ---------------------------------------------------------------------------
# Загрузка видео (обычная)
# ---------------------------------------------------------------------------

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        if 'video' not in request.files:
            flash("Файл не прикреплён", "danger")
            return redirect(url_for("index"))
        file = request.files['video']
        if file.filename == '':
            flash("Файл не выбран", "danger")
            return redirect(url_for("index"))
        if not allowed_file(file.filename):
            flash("Неподдерживаемый формат файла", "danger")
            return redirect(url_for("index"))

        title = request.form.get('title', '')
        description = request.form.get('description', '')
        filename = secure_filename(file.filename)
        unique = f"{int(time.time())}_{os.getpid()}"
        name, ext = os.path.splitext(filename)
        saved_name = f"{name}-{unique}{ext}"
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_name)
        file.save(save_path)

        user_id = g.user["id"] if g.user else None
        db = get_db()
        db.execute("""
            INSERT INTO videos (user_id, filename, title, description, path)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, saved_name, title, description, f"/uploads/{saved_name}"))
        db.commit()

        flash("Видео успешно загружено", "success")
        return redirect(url_for("index"))

    return render_template("upload.html", user=g.user)


# ---------------------------------------------------------------------------
# Удаление видео
# ---------------------------------------------------------------------------

@app.route("/video/delete/<int:video_id>", methods=["POST"])
def delete_video(video_id):
    if not g.user:
        flash("Требуется вход", "danger")
        return redirect(url_for("login"))

    db = get_db()
    vid = db.execute("SELECT id, user_id, path FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not vid:
        flash("Видео не найдено", "danger")
        return redirect(url_for("profile"))
    if vid["user_id"] != g.user["id"]:
        flash("У вас нет прав удалять это видео", "danger")
        return redirect(url_for("profile"))

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], vid["path"].split('/')[-1])
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

    db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    db.commit()
    flash("Видео удалено", "success")
    return redirect(url_for("profile"))


# ---------------------------------------------------------------------------
# Раздача файлов
# ---------------------------------------------------------------------------

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ---------------------------------------------------------------------------
# Статичные страницы
# ---------------------------------------------------------------------------

@app.route("/team")
def team():
    return render_template("team.html")

@app.route("/main")
def main():
    return redirect(url_for("index"))

@app.route("/sogl")
def sogl():
    flash("Вы приняли пользовательское соглашение", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Главная
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    user = g.user

    if user:
        if request.method == "POST" and request.form.get("form_type") == "create_task":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            category_id = request.form.get("category_id") or None
            deadline = request.form.get("deadline") or None
            if not title:
                flash("Заголовок задачи обязателен", "danger")
                return redirect(url_for("index"))
            db.execute("""
                INSERT INTO tasks (user_id, title, description, category_id, deadline)
                VALUES (?, ?, ?, ?, ?)
            """, (user["id"], title, description, category_id, deadline))
            db.commit()
            flash("Задача создана", "success")
            return redirect(url_for("index"))

        tasks = db.execute("""
            SELECT t.*, c.name AS category_name
            FROM tasks t LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = ? ORDER BY t.created_at DESC
        """, (user["id"],)).fetchall()
        categories = db.execute(
            "SELECT id, name FROM categories WHERE user_id = ?", (user["id"],)
        ).fetchall()
    else:
        tasks = []
        categories = []

    videos = []
    for row in db.execute("""
        SELECT
            v.id,
            v.filename,
            v.title,
            v.description,
            v.path,
            v.uploaded_at,
            v.user_id,
            u.username AS uploaded_by
        FROM videos v
        LEFT JOIN users u ON v.user_id = u.id
        WHERE v.path IS NOT NULL
        AND v.path != ''
        AND (
                v.import_status IS NULL
                OR v.import_status = 'ready'
            )
        ORDER BY v.uploaded_at DESC
    """):
        vid_dict = dict(row)
        vid_dict["comments"] = get_video_comments(db, vid_dict["id"])
        videos.append(vid_dict)

    return render_template("index.html", user=user, tasks=tasks,
                           categories=categories, videos=videos)


@app.route("/toggle/<int:task_id>")
def toggle(task_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    row = db.execute(
        "SELECT completed FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, g.user["id"])
    ).fetchone()
    if not row:
        return redirect(url_for("index"))
    db.execute(
        "UPDATE tasks SET completed = ? WHERE id = ? AND user_id = ?",
        (0 if row["completed"] else 1, task_id, g.user["id"])
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"]))
    db.commit()
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET", "POST"])
def edit(task_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category_id = request.form.get("category_id") or None
        deadline = request.form.get("deadline") or None
        completed = 1 if request.form.get("completed") == "on" else 0
        db.execute("""
            UPDATE tasks SET title=?, description=?, category_id=?, deadline=?, completed=?
            WHERE id=? AND user_id=?
        """, (title, description, category_id, deadline, completed, task_id, g.user["id"]))
        db.commit()
        return redirect(url_for("index"))
    task = db.execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, g.user["id"])
    ).fetchone()
    categories = db.execute(
        "SELECT id, name FROM categories WHERE user_id=?", (g.user["id"],)
    ).fetchall()
    return render_template("edit.html", task=task, categories=categories)


# ---------------------------------------------------------------------------
# Комментарии
# ---------------------------------------------------------------------------

@app.route("/comment", methods=["POST"])
def add_comment():
    if not g.user:
        flash("Требуется вход", "danger")
        return redirect(url_for("login"))
    video_id = request.form.get("video_id")
    text = request.form.get("text", "").strip()
    if not video_id or not text:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({'ok': False, 'error': 'Видео и текст обязательны'}), 400
        flash("Видео и текст обязательны", "danger")
        return redirect(url_for("index"))
    db = get_db()
    db.execute("INSERT INTO comments (video_id, user_id, text) VALUES (?, ?, ?)",
               (video_id, g.user["id"], text))
    db.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({'ok': True})
    flash("Комментарий добавлен", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Импорт видео с внешних платформ  ← НОВОЕ
# ---------------------------------------------------------------------------

@app.route("/import", methods=["GET", "POST"])
def video_import():
    if not g.user:
        flash("Войдите, чтобы импортировать видео", "danger")
        return redirect(url_for("login"))
    if not YT_DLP_AVAILABLE:
        flash("Установите yt-dlp: pip install yt-dlp", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        url          = request.form.get("url", "").strip()
        custom_title = request.form.get("title", "").strip()
        custom_desc  = request.form.get("description", "").strip()

        if not url:
            flash("Введите ссылку на видео", "danger")
            return redirect(url_for("video_import"))

        # Получаем мета-данные без скачивания
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            flash(f"Не удалось получить данные о видео: {e}", "danger")
            return redirect(url_for("video_import"))

        title     = custom_title or info.get("title", "Без названия")
        desc      = custom_desc  or (info.get("description") or "")[:1000]
        thumbnail = info.get("thumbnail", "")
        duration  = info.get("duration")
        platform  = detect_platform(url)

        # Создаём запись — filename временно пустой, заполнится после скачивания
        db = get_db()
        cur = db.execute("""
            INSERT INTO videos
                (user_id, filename, title, description, path,
                 source_url, platform, thumbnail, duration, import_status)
            VALUES (?, '', ?, ?, '', ?, ?, ?, ?, 'pending')
        """, (g.user["id"], title, desc, url, platform, thumbnail, duration))
        db.commit()
        video_id = cur.lastrowid

        # Скачивание в фоновом потоке
        threading.Thread(
            target=download_video_thread,
            args=(video_id, url),
            daemon=True
        ).start()

        flash(f'«{title}» добавлено! Скачивание началось.', "success")
        return redirect(url_for("index"))

    return render_template("import.html", user=g.user)


# ---------------------------------------------------------------------------
# API — AJAX
# ---------------------------------------------------------------------------

@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Мета-данные видео по URL (без скачивания)."""
    if not g.user:
        return jsonify({"error": "Необходима авторизация"}), 401
    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp не установлен"}), 500

    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL не указан"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            "title":       info.get("title", ""),
            "description": (info.get("description") or "")[:300],
            "thumbnail":   info.get("thumbnail", ""),
            "duration":    format_duration(info.get("duration")),
            "uploader":    info.get("uploader", ""),
            "platform":    detect_platform(url),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/import_status/<int:video_id>")
def api_import_status(video_id):
    """Статус скачивания для polling."""
    row = get_db().execute(
        "SELECT import_status, import_error, filename FROM videos WHERE id=?", (video_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status":   row["import_status"],
        "error":    row["import_error"],
        "filename": row["filename"],
    })


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
