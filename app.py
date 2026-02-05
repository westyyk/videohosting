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
    cur = db.cursor()
    # users, categories, tasks
    cur.executescript(
        """
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
        deadline TEXT, -- stored as YYYY-MM-DD or NULL
        completed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    );
    """
    )
    db.commit()


# ---------- Auth ----------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    cur = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,))
    return cur.fetchone()


def login_required(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return func(*args, **kwargs)

    return wrapper


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
        flash("Регистрация прошла успешно. Войдите.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        db = get_db()
        cur = db.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cur.fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash("Вы вошли в систему", "success")
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Неверные данные", "danger")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли", "info")
    return redirect(url_for("login"))


# ---------- Categories ----------
@app.route("/add_category", methods=["POST"])
@login_required
def add_category():
    name = request.form.get("category_name", "").strip()
    if not name:
        flash("Название категории не может быть пустым", "danger")
        return redirect(url_for("index"))
    db = get_db()
    db.execute(
        "INSERT INTO categories (user_id, name) VALUES (?, ?)", (g.user["id"], name)
    )
    db.commit()
    flash("Категория добавлена", "success")
    return redirect(url_for("index"))



def compute_task_status(task_row):
    if task_row["completed"]:
        return "Готово", False
    dl = task_row["deadline"]
    if dl:
        try:
            dl_date = datetime.strptime(dl, "%Y-%m-%d").date()
            today = date.today()
            if dl_date < today:
                return "Просрочено", True
            else:
                return "В процессе", False
        except Exception:
            return "В процессе", False
    else:
        return "В процессе", False


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    db = get_db()

    # Creating a task via form on same page
    if request.method == "POST" and request.form.get("form_type") == "create_task":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category_id = request.form.get("category_id") or None
        deadline = request.form.get("deadline") or None
        if not title:
            flash("Заголовок задачи обязателен", "danger")
            return redirect(url_for("index"))
        db.execute(
            "INSERT INTO tasks (user_id, title, description, category_id, deadline) VALUES (?, ?, ?, ?, ?)",
            (g.user["id"], title, description, category_id, deadline),
        )
        db.commit()
        flash("Задача создана", "success")
        return redirect(url_for("index"))

    # Filters, search and sort
    q = request.args.get("q", "").strip()
    cat = request.args.get("category")
    sort = request.args.get("sort", "deadline")  # 'deadline' or 'created'
    params = [g.user["id"]]
    where = "WHERE t.user_id = ?"
    if q:
        where += " AND t.title LIKE ?"
        params.append(f"%{q}%")
    if cat and cat != "all":
        where += " AND t.category_id = ?"
        params.append(cat)

    order = "ORDER BY "
    if sort == "created":
        order += "t.created_at DESC"
    else:
        # put tasks without deadline at end, otherwise by deadline asc
        order += "CASE WHEN t.deadline IS NULL THEN 1 ELSE 0 END, t.deadline ASC"

    query = f"""
    SELECT t.*, c.name AS category_name
    FROM tasks t
    LEFT JOIN categories c ON t.category_id = c.id
    {where}
    {order}
    """
    cur = db.execute(query, params)
    rows = cur.fetchall()

    tasks = []
    for r in rows:
        status, overdue = compute_task_status(r)
        tasks.append(
            {
                "id": r["id"],
                "title": r["title"],
                "description": r["description"],
                "category_id": r["category_id"],
                "category_name": r["category_name"],
                "deadline": r["deadline"],
                "completed": bool(r["completed"]),
                "status": status,
                "overdue": overdue,
                "created_at": r["created_at"],
            }
        )

    categories_cur = db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name", (g.user["id"],)
    )
    categories = categories_cur.fetchall()
    return render_template(
        "index.html",
        tasks=tasks,
        categories=categories,
        q=q,
        selected_category=cat or "all",
        sort=sort,
        user=g.user,
    )


@app.route("/toggle/<int:task_id>")
@login_required
def toggle(task_id):
    db = get_db()
    cur = db.execute(
        "SELECT completed FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"])
    )
    row = cur.fetchone()
    if not row:
        flash("Задача не найдена", "danger")
        return redirect(url_for("index"))
    new = 0 if row["completed"] else 1
    db.execute(

"UPDATE tasks SET completed = ? WHERE id = ? AND user_id = ?", (new, task_id, g.user["id"])
    )
    db.commit()
    flash("Статус изменён", "success")
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
@login_required
def delete(task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"]))
    db.commit()
    flash("Задача удалена", "info")
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET", "POST"])
@login_required
def edit(task_id):
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category_id = request.form.get("category_id") or None
        deadline = request.form.get("deadline") or None
        completed = 1 if request.form.get("completed") == "on" else 0
        if not title:
            flash("Заголовок обязателен", "danger")
            return redirect(url_for("edit", task_id=task_id))
        db.execute(
            "UPDATE tasks SET title=?, description=?, category_id=?, deadline=?, completed=? WHERE id=? AND user_id=?",
            (title, description, category_id, deadline, completed, task_id, g.user["id"]),
        )
        db.commit()
        flash("Задача обновлена", "success")
        return redirect(url_for("index"))
    cur = db.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"])
    )
    task = cur.fetchone()
    if not task:
        flash("Задача не найдена", "danger")
        return redirect(url_for("index"))
    categories_cur = db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name", (g.user["id"],)
    )
    categories = categories_cur.fetchall()
    return render_template("edit.html", task=task, categories=categories)


@app.route("/home")
def home_redirect():
    if current_user():
        return redirect(url_for("index"))
    return redirect(url_for("login"))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
