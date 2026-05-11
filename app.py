from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
from datetime import datetime
import os
import time

DB = "tasks.db"
app = Flask(__name__)
app.secret_key = "change_this_secret_in_prod"

# Папка для загрузок
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2048 * 2048 * 2048  # 1 ГБ

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'webm', 'mkv', 'avi'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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

    -- Новая таблица videos
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT NOT NULL,
        title TEXT,
        description TEXT,
        path TEXT,
        uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    -- Комментарии к видео
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
    db = get_db()
    cur = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,))
    return cur.fetchone()


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


# Роуты аутентификации
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
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

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


# Новый: профиль текущего пользователя
@app.route("/profile")
def profile():
    user = g.user
    # Разрешаем просмотр профиля без входа
    db = get_db()

    if not user:
        user_id = None
        user_row = None
    else:
        user_id = user["id"]
        user_row = user

    # Загружаем видео пользователя (если есть)
    if user_id:
        user_videos = db.execute("""
            SELECT v.id, v.filename, v.title, v.description, v.path, v.uploaded_at,
                   u.username AS uploaded_by, v.user_id
            FROM videos v
            LEFT JOIN users u ON v.user_id = u.id
            WHERE v.user_id = ?
            ORDER BY v.uploaded_at DESC
        """, (user_id,)).fetchall()
    else:
        user_videos = []

    # Добавляем комментарии к каждому видео (если есть)
    videos_with_comments = []
    for vid in user_videos:
        vid_dict = dict(vid)
        vid_dict["comments"] = get_video_comments(db, vid_dict["id"])
        videos_with_comments.append(vid_dict)

    # is_owner для текущего профиля (если просматривает владелец)
    is_owner = True if user else False

    return render_template("profile.html", user=user_row, videos=videos_with_comments, is_owner=is_owner)


# Публичный профиль по id
@app.route("/profile/<int:user_id>")
def profile_by_id(user_id):
    db = get_db()
    user = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("Пользователь не найден", "danger")
        return redirect(url_for("index"))

    user_videos = db.execute("""
        SELECT v.id, v.filename, v.title, v.description, v.path, v.uploaded_at,
               u.username AS uploaded_by, v.user_id
        FROM videos v
        LEFT JOIN users u ON v.user_id = u.id
        WHERE v.user_id = ?
        ORDER BY v.uploaded_at DESC
    """, (user_id,)).fetchall()

    # Подгружаем комментарии к каждому видео
    videos_with_comments = []
    for vid in user_videos:
        vid_dict = dict(vid)
        vid_dict["comments"] = get_video_comments(db, vid_dict["id"])
        videos_with_comments.append(vid_dict)

    # Проверяем владение профилем текущим пользователем
    is_owner = False
    if g.user:
        is_owner = (g.user["id"] == user_id)

    return render_template("profile.html", user=user, videos=videos_with_comments, is_owner=is_owner)


# Новое: загрузка видео
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

        # Сохраняем метаданные в videos
        user_id = g.user["id"] if g.user else None
        db = get_db()
        db.execute("""
            INSERT INTO videos (user_id, filename, title, description, path)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, saved_name, title, description, f"/uploads/{saved_name}"))
        db.commit()

        flash("Видео успешно загружено", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")


# Удаление видео пользователя (только владелец) - новая точка входа
@app.route("/video/delete/<int:video_id>", methods=["POST"])
def delete_video(video_id):
    if not g.user:
        flash("Требуется вход", "danger")
        return redirect(url_for("login"))

    db = get_db()
    vid = db.execute(
        "SELECT id, user_id, path FROM videos WHERE id = ?",
        (video_id,)
    ).fetchone()

    if not vid:
        flash("Видео не найдено", "danger")
        return redirect(url_for("profile"))

    if vid["user_id"] != g.user["id"]:
        flash("У вас нет прав удалять это видео", "danger")
        return redirect(url_for("profile"))

    # Удаляем файл с диска
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], vid["path"].split('/')[-1])
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

    # Удаляем запись из базы
    db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    db.commit()

    flash("Видео удалено", "success")
    return redirect(url_for("profile"))


# Раздача файлов
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


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


# Главная - показываем задачи (если есть) и все видео
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
            FROM tasks t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = ?
            ORDER BY t.created_at DESC
        """, (user["id"],)).fetchall()

        categories = db.execute("""
            SELECT id, name FROM categories
            WHERE user_id = ?
        """, (user["id"],)).fetchall()
    else:
        tasks = []
        categories = []

    # Главная: показываем все видео всех пользователей (для гостя тоже видно)
    videos = []
    for row in db.execute("""
        SELECT v.id, v.filename, v.title, v.description, v.path, v.uploaded_at,
               v.user_id, u.username AS uploaded_by
        FROM videos v
        LEFT JOIN users u ON v.user_id = u.id
        ORDER BY v.uploaded_at DESC
    """):
        vid_id = row["id"]
        comments = get_video_comments(db, vid_id)
        row = dict(row)
        row["comments"] = comments
        videos.append(row)

    return render_template(
        "index.html",
        user=user,
        tasks=tasks,
        categories=categories,
        videos=videos
    )


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

    new = 0 if row["completed"] else 1

    db.execute(
        "UPDATE tasks SET completed = ? WHERE id = ? AND user_id = ?",
        (new, task_id, g.user["id"])
    )
    db.commit()

    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    if not g.user:
        return redirect(url_for("login"))

    db = get_db()
    db.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, g.user["id"])
    )
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
            UPDATE tasks
            SET title=?, description=?, category_id=?, deadline=?, completed=?
            WHERE id=? AND user_id=?
        """, (title, description, category_id, deadline, completed, task_id, g.user["id"]))

        db.commit()
        return redirect(url_for("index"))

    task = db.execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?",
        (task_id, g.user["id"])
    ).fetchone()

    categories = db.execute(
        "SELECT id, name FROM categories WHERE user_id=?",
        (g.user["id"],)
    ).fetchall()

    return render_template("edit.html", task=task, categories=categories)


# Маршрут добавления комментария
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
