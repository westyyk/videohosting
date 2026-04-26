from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import date, datetime

DB = "tasks.db"
app = Flask(__name__)
app.secret_key = "change_this_secret_in_prod"


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
    """)



    db.commit()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    cur = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,))
    return cur.fetchone()


@app.before_request
def before_request():
    init_db()
    g.user = current_user()


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


@app.route("/team")
def team():
    return render_template("team.html")

@app.route("/main")
def main():
    return render_template("index.html")


@app.route("/sogl")
def sogl():
    flash("Вы приняли пользовательское соглашение", "info")
    return render_template("index.html")


@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    user = g.user

    if not user:
        return render_template(
            "index.html",
            user=None,
            tasks=[],
            categories=[]
        )

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

    return render_template(
        "index.html",
        user=user,
        tasks=tasks,
        categories=categories
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
