from flask import Flask, render_template, request, redirect, url_for, session, flash, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import sqlite3
import os
import threading
import uuid
import mimetypes

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

DB = "tasks.db"

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3

ALLOWED_EXTENSIONS = {
    "mp4",
    "mov",
    "webm",
    "mkv",
    "avi"
}

MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
    "video/x-msvideo"
}

db_lock = threading.Lock()


def allowed_file(filename):
    return "." in filename and \
           filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_mime_type(filename):
    mime, _ = mimetypes.guess_type(filename)
    return mime in MIME_TYPES


def detect_platform(url: str):

    u = url.lower()

    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"

    if "vk.com" in u:
        return "VK Video"

    if "rutube.ru" in u:
        return "Rutube"

    if "tiktok.com" in u:
        return "TikTok"

    if "vimeo.com" in u:
        return "Vimeo"

    return "Другое"


def format_duration(seconds):

    if not seconds:
        return ""

    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)

    if h:
        return f"{h}:{m:02d}:{s:02d}"

    return f"{m}:{s:02d}"


def get_db():

    db = getattr(g, "_database", None)

    if db is None:

        db = g._database = sqlite3.connect(
            DB,
            timeout=15,
            check_same_thread=False
        )

        db.row_factory = sqlite3.Row

        db.execute("PRAGMA journal_mode=WAL;")

    return db


def init_db():

    conn = sqlite3.connect(DB, timeout=15)

    conn.execute("PRAGMA journal_mode=WAL;")

    conn.executescript("""

    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS users (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        username TEXT UNIQUE NOT NULL,

        password_hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS videos (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        user_id INTEGER,

        filename TEXT,

        title TEXT NOT NULL,

        description TEXT,

        path TEXT,

        uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,

        source_url TEXT,

        platform TEXT,

        thumbnail TEXT,

        duration INTEGER,

        import_status TEXT DEFAULT 'ready',

        import_error TEXT,

        FOREIGN KEY(user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS comments (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        video_id INTEGER NOT NULL,

        user_id INTEGER,

        text TEXT NOT NULL,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(video_id)
        REFERENCES videos(id)
        ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_videos_user
    ON videos(user_id);

    """)

    conn.commit()
    conn.close()


def current_user():

    uid = session.get("user_id")

    if not uid:
        return None

    try:

        return get_db().execute(
            "SELECT id, username FROM users WHERE id=?",
            (uid,)
        ).fetchone()

    except:
        return None


def get_video_comments(db, video_id):

    return db.execute("""

        SELECT
            c.id,
            c.text,
            c.created_at,
            u.username AS author

        FROM comments c

        LEFT JOIN users u
        ON c.user_id = u.id

        WHERE c.video_id = ?

        ORDER BY c.created_at ASC

    """, (video_id,)).fetchall()
    
def set_video_status(video_id, status, filename=None, error=None):

    with db_lock:

        conn = sqlite3.connect(DB, timeout=10)

        try:

            if filename:

                conn.execute("""

                    UPDATE videos

                    SET
                        import_status=?,
                        filename=?,
                        path=?,
                        import_error=?

                    WHERE id=?

                """, (
                    status,
                    filename,
                    f"/uploads/{filename}",
                    error,
                    video_id
                ))

            else:

                conn.execute("""

                    UPDATE videos

                    SET
                        import_status=?,
                        import_error=?

                    WHERE id=?

                """, (
                    status,
                    error,
                    video_id
                ))

            conn.commit()

        finally:
            conn.close()


def download_video_thread(video_id: int, url: str):

    unique = f"import_{video_id}_{uuid.uuid4().hex[:8]}"

    out_tmpl = os.path.join(
        UPLOAD_FOLDER,
        f"{unique}_%(title).80s.%(ext)s"
    )

    # ГЛАВНОЕ ИСПРАВЛЕНИЕ
    ydl_opts = {

        # Куда сохранять
        "outtmpl": out_tmpl,

        # Только mp4
        # максимально совместимый формат
        "format": "best[ext=mp4]/best",

        # mp4 контейнер
        "merge_output_format": "mp4",

        # Без плейлистов
        "noplaylist": True,

        # Безопасные имена
        "restrictfilenames": True,

        # SSL
        "nocheckcertificate": True,

        # Логи
        "quiet": False,
    }

    try:

        set_video_status(video_id, "downloading")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            title = info.get("title") or "Видео"

            description = info.get("description") or ""

            duration = info.get("duration")

            thumbnail = info.get("thumbnail")

        files = [
            f for f in os.listdir(UPLOAD_FOLDER)
            if f.startswith(unique)
        ]

        if not files:
            raise Exception("Файл не найден")

        latest_file = max(
            files,
            key=lambda f: os.path.getctime(
                os.path.join(UPLOAD_FOLDER, f)
            )
        )

        conn = sqlite3.connect(DB, timeout=10)

        conn.execute("""

            UPDATE videos

            SET
                filename=?,
                path=?,
                title=?,
                description=?,
                thumbnail=?,
                duration=?,
                import_status='ready'

            WHERE id=?

        """, (
            latest_file,
            f"/uploads/{latest_file}",
            title,
            description,
            thumbnail,
            duration,
            video_id
        ))

        conn.commit()
        conn.close()

        print(f"✅ Видео импортировано: {latest_file}")

    except Exception as e:

        print("❌ ОШИБКА ИМПОРТА:")
        print(str(e))

        set_video_status(
            video_id,
            "error",
            error=str(e)[:500]
        )


@app.before_request
def before_request():
    g.user = current_user()


@app.teardown_appcontext
def close_connection(exception):

    db = getattr(g, "_database", None)

    if db is not None:
        db.close()



@app.route("/")
def index():

    db = get_db()

    videos = db.execute("""

        SELECT
            v.*,
            u.username as uploaded_by

        FROM videos v

        LEFT JOIN users u
        ON v.user_id = u.id

        WHERE
            (v.import_status IS NULL
            OR v.import_status='ready')

            AND v.path IS NOT NULL

        ORDER BY v.uploaded_at DESC

    """).fetchall()

    video_list = []

    for v in videos:

        vid = dict(v)

        vid["comments"] = get_video_comments(
            db,
            vid["id"]
        )

        video_list.append(vid)

    return render_template(
        "index.html",
        user=g.user,
        videos=video_list,
        format_duration=format_duration
    )


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form["username"].strip()

        password = request.form["password"]

        user = get_db().execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session["user_id"] = user["id"]

            flash("Вы успешно вошли!", "success")

            return redirect(url_for("index"))

        flash("Неверный логин или пароль", "danger")

    return render_template(
        "login.html",
        user=g.user
    )



@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form["username"].strip()

        password = request.form["password"]

        if not username or not password:

            flash("Заполните все поля", "danger")

            return redirect(url_for("register"))

        try:

            db = get_db()

            db.execute("""

                INSERT INTO users (
                    username,
                    password_hash
                )

                VALUES (?, ?)

            """, (
                username,
                generate_password_hash(password)
            ))

            db.commit()

            flash("Регистрация успешна!", "success")

            return redirect(url_for("login"))

        except sqlite3.IntegrityError:

            flash("Имя уже занято", "danger")

    return render_template(
        "register.html",
        user=g.user
    )



@app.route("/logout")
def logout():

    session.clear()

    flash("Вы вышли", "info")

    return redirect(url_for("index"))


@app.route("/upload", methods=["GET", "POST"])
def upload():

    if not g.user:

        flash("Войдите в аккаунт", "danger")

        return redirect(url_for("login"))

    if request.method == "POST":

        if "video" not in request.files:

            flash("Файл не выбран", "danger")

            return redirect(url_for("upload"))

        file = request.files["video"]

        if file.filename == "":

            flash("Файл не выбран", "danger")

            return redirect(url_for("upload"))

        if not allowed_file(file.filename):

            flash("Неверный формат", "danger")

            return redirect(url_for("upload"))

        title = request.form.get("title", "").strip()

        description = request.form.get("description", "").strip()

        filename = secure_filename(file.filename)

        unique = uuid.uuid4().hex[:8]

        name, ext = os.path.splitext(filename)

        saved_name = f"{name}-{unique}{ext}"

        save_path = os.path.join(
            UPLOAD_FOLDER,
            saved_name
        )

        file.save(save_path)

        db = get_db()

        db.execute("""

            INSERT INTO videos (

                user_id,
                filename,
                title,
                description,
                path,
                import_status

            )

            VALUES (?, ?, ?, ?, ?, 'ready')

        """, (
            g.user["id"],
            saved_name,
            title or name,
            description,
            f"/uploads/{saved_name}"
        ))

        db.commit()

        flash("Видео загружено!", "success")

        return redirect(url_for("index"))

    return render_template(
        "upload.html",
        user=g.user
    )



@app.route("/import", methods=["GET", "POST"])
def import_video():

    if not g.user:

        flash("Войдите в аккаунт", "danger")

        return redirect(url_for("login"))

    if request.method == "POST":

        if not YT_DLP_AVAILABLE:

            flash("yt-dlp не установлен", "danger")

            return redirect(url_for("import_video"))

        url = request.form.get("url", "").strip()

        title = request.form.get("title", "").strip()

        description = request.form.get("description", "").strip()

        if not url:

            flash("Введите ссылку", "danger")

            return redirect(url_for("import_video"))

        platform = detect_platform(url)

        db = get_db()

        result = db.execute("""

            INSERT INTO videos (

                user_id,
                title,
                description,
                source_url,
                platform,
                import_status

            )

            VALUES (?, ?, ?, ?, ?, 'pending')

            RETURNING id

        """, (
            g.user["id"],
            title or "Импортируемое видео",
            description,
            url,
            platform
        )).fetchone()

        db.commit()

        if result:

            video_id = result["id"]

            threading.Thread(
                target=download_video_thread,
                args=(video_id, url),
                daemon=True
            ).start()

            flash(
                f"Импорт видео с {platform} начался",
                "success"
            )

        return redirect(url_for("index"))

    return render_template(
        "import.html",
        user=g.user
    )



@app.route("/comment", methods=["POST"])
def add_comment():

    if not g.user:

        flash("Войдите", "danger")

        return redirect(url_for("login"))

    video_id = request.form.get("video_id")

    text = request.form.get("text", "").strip()

    if not text:

        flash("Комментарий пуст", "danger")

        return redirect(url_for("index"))

    db = get_db()

    db.execute("""

        INSERT INTO comments (

            video_id,
            user_id,
            text

        )

        VALUES (?, ?, ?)

    """, (
        video_id,
        g.user["id"],
        text
    ))

    db.commit()

    flash("Комментарий добавлен", "success")

    return redirect(url_for("index"))



@app.route("/profile")
@app.route("/profile/<int:user_id>")
def profile(user_id=None):

    db = get_db()

    if user_id is None:

        if not g.user:

            flash("Войдите", "danger")

            return redirect(url_for("login"))

        user_id = g.user["id"]

        user = g.user

    else:

        user = db.execute("""

            SELECT id, username
            FROM users
            WHERE id=?

        """, (user_id,)).fetchone()

        if not user:

            flash("Пользователь не найден", "danger")

            return redirect(url_for("index"))

    videos = db.execute("""

        SELECT *
        FROM videos
        WHERE user_id=?

        ORDER BY uploaded_at DESC

    """, (user_id,)).fetchall()

    video_list = []

    for v in videos:

        vid = dict(v)

        vid["comments"] = get_video_comments(
            db,
            vid["id"]
        )

        video_list.append(vid)

    is_owner = g.user and g.user["id"] == user_id

    return render_template(
        "profile.html",
        user=user,
        videos=video_list,
        is_owner=is_owner,
        format_duration=format_duration
    )


@app.route("/uploads/<filename>")
def uploaded_file(filename):

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        filename
    )

@app.route("/team")
def team():
    return render_template("team.html")


@app.route("/agreement")
def agreement():
    return render_template("agreement.html")


@app.route("/main")
def main():
    return redirect(url_for("index"))


@app.route("/sogl")
def sogl():

    flash(
        "Вы приняли соглашение",
        "info"
    )

    return redirect(url_for("index"))




if __name__ == "__main__":

    init_db()

    print("✅ База данных готова")

    print("🚀 Сервер:")
    print("http://127.0.0.1:5000")

    app.run(
        debug=True,
        host="0.0.0.0",
        port=5000
    )
