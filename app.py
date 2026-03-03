from datetime import datetime, timedelta
import csv
import io
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
    Response,
)
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.secret_key = "offline_debug_contest_secret_key"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "database.db")

CONTEST_DURATION_MINUTES = 60
CONTEST_START_TIME = datetime.now()

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

DEFAULT_FAULTY_CODE = '''def find_max(nums):
    max_num = 0
    for n in nums:
        if n < max_num:
            max_num = n
    return max_num

nums = [3, 7, 2, 9, 5]
print(find_max(nums))
'''

DEFAULT_CORRECT_CODE = '''def find_max(nums):
    max_num = nums[0]
    for n in nums:
        if n > max_num:
            max_num = n
    return max_num

nums = [3, 7, 2, 9, 5]
print(find_max(nums))
'''

DEFAULT_EXPECTED_OUTPUT = "9"
DEFAULT_LANGUAGE = "python"

SUPPORTED_LANGUAGES = {
    "python": "Python",
    "javascript": "JavaScript",
    "c": "C",
    "cpp": "C++",
    "java": "Java",
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    cursor = db.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            roll_no TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question_id INTEGER,
            language TEXT NOT NULL DEFAULT 'python',
            code TEXT NOT NULL,
            output TEXT,
            score INTEGER NOT NULL,
            submission_time TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (question_id) REFERENCES contest_questions (id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS contest_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'python',
            faulty_code TEXT NOT NULL,
            correct_code TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    submission_columns = [row["name"] for row in cursor.execute("PRAGMA table_info(submissions)").fetchall()]
    if "question_id" not in submission_columns:
        cursor.execute("ALTER TABLE submissions ADD COLUMN question_id INTEGER")
    if "language" not in submission_columns:
        cursor.execute("ALTER TABLE submissions ADD COLUMN language TEXT NOT NULL DEFAULT 'python'")

    question_columns = [row["name"] for row in cursor.execute("PRAGMA table_info(contest_questions)").fetchall()]
    if "language" not in question_columns:
        cursor.execute("ALTER TABLE contest_questions ADD COLUMN language TEXT NOT NULL DEFAULT 'python'")

    existing_questions = cursor.execute("SELECT id FROM contest_questions LIMIT 1").fetchone()
    if not existing_questions:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO contest_questions (title, language, faulty_code, correct_code, expected_output, points, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Question 1",
                DEFAULT_LANGUAGE,
                DEFAULT_FAULTY_CODE,
                DEFAULT_CORRECT_CODE,
                DEFAULT_EXPECTED_OUTPUT,
                10,
                now,
                now,
            ),
        )

    legacy_problem_exists = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contest_problem'"
    ).fetchone()
    if legacy_problem_exists:
        legacy_problem = cursor.execute(
            "SELECT faulty_code, correct_code, expected_output, updated_at FROM contest_problem WHERE id = 1"
        ).fetchone()
        if legacy_problem:
            first_question = cursor.execute(
                "SELECT id FROM contest_questions ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if first_question:
                cursor.execute(
                    """
                    UPDATE contest_questions
                    SET faulty_code = ?, correct_code = ?, expected_output = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        legacy_problem["faulty_code"],
                        legacy_problem["correct_code"],
                        legacy_problem["expected_output"],
                        legacy_problem["updated_at"],
                        first_question["id"],
                    ),
                )
        cursor.execute("DROP TABLE contest_problem")

    missing_question_rows = cursor.execute(
        "SELECT id FROM submissions WHERE question_id IS NULL"
    ).fetchall()
    if missing_question_rows:
        first_question = cursor.execute("SELECT id FROM contest_questions ORDER BY id ASC LIMIT 1").fetchone()
        if first_question:
            cursor.execute(
                "UPDATE submissions SET question_id = ? WHERE question_id IS NULL",
                (first_question["id"],),
            )
    db.commit()
    db.close()


def get_questions():
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, expected_output, points, created_at, updated_at
        FROM contest_questions
        ORDER BY id ASC
        """
    ).fetchall()


def get_question_by_id(question_id):
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, expected_output, points, created_at, updated_at
        FROM contest_questions
        WHERE id = ?
        """,
        (question_id,),
    ).fetchone()


def get_default_question():
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, expected_output, points, created_at, updated_at
        FROM contest_questions
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()


def get_contest_end_time():
    return CONTEST_START_TIME + timedelta(minutes=CONTEST_DURATION_MINUTES)


def get_time_left_seconds():
    remaining = int((get_contest_end_time() - datetime.now()).total_seconds())
    return max(0, remaining)


def is_contest_locked():
    return get_time_left_seconds() <= 0


def require_student_login():
    return "user_id" in session and not session.get("is_admin", False)


def require_admin_login():
    return session.get("is_admin", False)


def run_student_code(code_text, expected_output, language):
    temp_file_path = None
    language = (language or DEFAULT_LANGUAGE).lower()
    if language not in SUPPORTED_LANGUAGES:
        return "Unsupported language selected", 0

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            if language == "python":
                temp_file_path = os.path.join(temp_dir, "main.py")
                with open(temp_file_path, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                result = subprocess.run(
                    [sys.executable, temp_file_path],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=temp_dir,
                )

            elif language == "javascript":
                if not shutil.which("node"):
                    return "Node.js is not installed on server", 0

                temp_file_path = os.path.join(temp_dir, "main.js")
                with open(temp_file_path, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                result = subprocess.run(
                    ["node", temp_file_path],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=temp_dir,
                )

            elif language == "c":
                if not shutil.which("gcc"):
                    return "GCC is not installed on server", 0

                source_file = os.path.join(temp_dir, "main.c")
                binary_file = os.path.join(temp_dir, "main.exe")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                compile_result = subprocess.run(
                    ["gcc", source_file, "-o", binary_file],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=temp_dir,
                )
                if compile_result.returncode != 0:
                    return (compile_result.stderr or "Compilation error").strip(), 0

                result = subprocess.run(
                    [binary_file],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=temp_dir,
                )

            elif language == "cpp":
                if not shutil.which("g++"):
                    return "G++ is not installed on server", 0

                source_file = os.path.join(temp_dir, "main.cpp")
                binary_file = os.path.join(temp_dir, "main.exe")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                compile_result = subprocess.run(
                    ["g++", source_file, "-o", binary_file],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=temp_dir,
                )
                if compile_result.returncode != 0:
                    return (compile_result.stderr or "Compilation error").strip(), 0

                result = subprocess.run(
                    [binary_file],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=temp_dir,
                )

            elif language == "java":
                if not shutil.which("javac") or not shutil.which("java"):
                    return "Java JDK is not installed on server", 0

                source_file = os.path.join(temp_dir, "Main.java")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                compile_result = subprocess.run(
                    ["javac", source_file],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=temp_dir,
                )
                if compile_result.returncode != 0:
                    return (compile_result.stderr or "Compilation error").strip(), 0

                result = subprocess.run(
                    ["java", "-cp", temp_dir, "Main"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=temp_dir,
                )

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.returncode != 0:
                output = stderr if stderr else "Runtime error"
                return output, 0

            score = 10 if stdout == expected_output.strip() else 0
            return stdout, score
        except subprocess.TimeoutExpired:
            return "Execution timed out", 0
        except Exception:
            return "Execution failed", 0


@app.route("/")
def index():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    if session.get("user_id"):
        return redirect(url_for("contest"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        roll_no = request.form.get("roll_no", "").strip()
        password = request.form.get("password", "")

        if not name or not email or not roll_no or not password:
            flash("All fields are required.", "error")
            return render_template("register.html")

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (name, email, roll_no, password_hash) VALUES (?, ?, ?, ?)",
                (name, email, roll_no, generate_password_hash(password)),
            )
            db.commit()
            flash("Registration successful. Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Roll Number already exists.", "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        roll_no = request.form.get("roll_no", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE roll_no = ?", (roll_no,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["roll_no"] = user["roll_no"]
            session["is_admin"] = False
            return redirect(url_for("contest"))

        flash("Invalid Roll Number or Password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/contest")
def contest():
    if not require_student_login():
        return redirect(url_for("login"))

    db = get_db()
    user_id = session["user_id"]

    questions = get_questions()
    if not questions:
        return render_template(
            "contest.html",
            questions=[],
            selected_question=None,
            faulty_code="",
            expected_output="",
            time_left=get_time_left_seconds(),
            locked=is_contest_locked(),
            already_correct=False,
            last_submission=None,
        )

    selected_question_id = request.args.get("q", type=int)
    selected_question = get_question_by_id(selected_question_id) if selected_question_id else None
    if not selected_question:
        selected_question = questions[0]

    already_correct = (
        db.execute(
            "SELECT id FROM submissions WHERE user_id = ? AND question_id = ? AND score > 0 ORDER BY id DESC LIMIT 1",
            (user_id, selected_question["id"]),
        ).fetchone()
        is not None
    )

    last_submission = db.execute(
        "SELECT output, score, submission_time FROM submissions WHERE user_id = ? AND question_id = ? ORDER BY id DESC LIMIT 1",
        (user_id, selected_question["id"]),
    ).fetchone()

    time_left = get_time_left_seconds()
    locked = is_contest_locked()

    return render_template(
        "contest.html",
        questions=questions,
        supported_languages=SUPPORTED_LANGUAGES,
        selected_question=selected_question,
        faulty_code=selected_question["faulty_code"],
        expected_output=selected_question["expected_output"],
        time_left=time_left,
        locked=locked,
        already_correct=already_correct,
        last_submission=last_submission,
    )


@app.route("/submit", methods=["POST"])
def submit():
    if not require_student_login():
        return redirect(url_for("login"))

    if is_contest_locked():
        flash("Contest is locked. Time is over.", "error")
        return redirect(url_for("contest"))

    db = get_db()
    user_id = session["user_id"]

    question_id = request.form.get("question_id", type=int)
    question = get_question_by_id(question_id) if question_id else None
    if not question:
        flash("Invalid question selected.", "error")
        return redirect(url_for("contest"))

    selected_language = request.form.get("language", DEFAULT_LANGUAGE).lower().strip()
    if selected_language not in SUPPORTED_LANGUAGES:
        selected_language = question["language"] if question["language"] in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    already_correct = db.execute(
        "SELECT id FROM submissions WHERE user_id = ? AND question_id = ? AND score > 0 ORDER BY id DESC LIMIT 1",
        (user_id, question_id),
    ).fetchone()
    if already_correct:
        flash("Submission blocked. You already solved this question.", "error")
        return redirect(url_for("contest", q=question_id))

    code = request.form.get("code", "")
    if not code.strip():
        flash("Code cannot be empty.", "error")
        return redirect(url_for("contest", q=question_id))

    output, score = run_student_code(code, question["expected_output"], selected_language)
    final_score = question["points"] if score > 0 else 0

    db.execute(
        "INSERT INTO submissions (user_id, question_id, language, code, output, score, submission_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            question_id,
            selected_language,
            code,
            output,
            final_score,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    if final_score > 0:
        flash(f"Correct! Score: {final_score}", "success")
    else:
        flash("Wrong output. Score: 0", "error")
    return redirect(url_for("contest", q=question_id))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.clear()
            session["is_admin"] = True
            session["admin_username"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin credentials.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
def admin_dashboard():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    users = db.execute("SELECT id, name, email, roll_no FROM users ORDER BY id ASC").fetchall()

    submissions = db.execute(
        """
        SELECT s.id, q.title AS question_title, s.language, u.name, u.roll_no, s.score, s.output, s.submission_time
        FROM submissions s
        JOIN users u ON s.user_id = u.id
        LEFT JOIN contest_questions q ON s.question_id = q.id
        ORDER BY s.submission_time DESC
        """
    ).fetchall()

    leaderboard = db.execute(
        """
        SELECT u.name, u.roll_no, COALESCE(SUM(best.best_score), 0) AS total_score,
               COALESCE(MIN(best.first_submission_time), '-') AS first_submission_time
        FROM users u
        LEFT JOIN (
            SELECT user_id, question_id, MAX(score) AS best_score, MIN(submission_time) AS first_submission_time
            FROM submissions
            GROUP BY user_id, question_id
        ) best ON u.id = best.user_id
        GROUP BY u.id
        ORDER BY total_score DESC, first_submission_time ASC
        """
    ).fetchall()
    questions = get_questions()
    stats = {
        "users": len(users),
        "submissions": len(submissions),
        "questions": len(questions),
    }

    return render_template(
        "admin_dashboard.html",
        users=users,
        submissions=submissions,
        leaderboard=leaderboard,
        questions=questions,
        supported_languages=SUPPORTED_LANGUAGES,
        stats=stats,
    )


@app.route("/admin/question/create", methods=["POST"])
def admin_create_question():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    title = request.form.get("title", "").strip()
    language = request.form.get("language", DEFAULT_LANGUAGE).lower().strip()
    faulty_code = request.form.get("faulty_code", "").strip()
    correct_code = request.form.get("correct_code", "").strip()
    expected_output = request.form.get("expected_output", "").strip()
    points = request.form.get("points", type=int)

    if not title or not faulty_code or not correct_code or not expected_output:
        flash("Title, faulty code, correct code, and expected output are all required.", "error")
        return redirect(url_for("admin_dashboard"))

    if not points or points < 1:
        points = 10
    if language not in SUPPORTED_LANGUAGES:
        language = DEFAULT_LANGUAGE

    db = get_db()
    db.execute(
        """
        INSERT INTO contest_questions (title, language, faulty_code, correct_code, expected_output, points, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            language,
            faulty_code,
            correct_code,
            expected_output,
            points,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    flash("Question added successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/question/<int:question_id>/update", methods=["POST"])
def admin_update_question(question_id):
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    title = request.form.get("title", "").strip()
    language = request.form.get("language", DEFAULT_LANGUAGE).lower().strip()
    faulty_code = request.form.get("faulty_code", "").strip()
    correct_code = request.form.get("correct_code", "").strip()
    expected_output = request.form.get("expected_output", "").strip()
    points = request.form.get("points", type=int)

    if not title or not faulty_code or not correct_code or not expected_output:
        flash("All question fields are required.", "error")
        return redirect(url_for("admin_dashboard"))

    if not points or points < 1:
        points = 10
    if language not in SUPPORTED_LANGUAGES:
        language = DEFAULT_LANGUAGE

    db = get_db()
    existing = db.execute("SELECT id FROM contest_questions WHERE id = ?", (question_id,)).fetchone()
    if not existing:
        flash("Question not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        """
        UPDATE contest_questions
        SET title = ?, language = ?, faulty_code = ?, correct_code = ?, expected_output = ?, points = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            language,
            faulty_code,
            correct_code,
            expected_output,
            points,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            question_id,
        ),
    )
    db.commit()
    flash("Question updated successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/question/<int:question_id>/delete", methods=["POST"])
def admin_delete_question(question_id):
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    total_questions = db.execute("SELECT COUNT(*) AS count FROM contest_questions").fetchone()["count"]
    if total_questions <= 1:
        flash("At least one question must remain in the contest.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM submissions WHERE question_id = ?", (question_id,))
    db.execute("DELETE FROM contest_questions WHERE id = ?", (question_id,))
    db.commit()
    flash("Question deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/submission/<int:submission_id>/delete", methods=["POST"])
def admin_delete_submission(submission_id):
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    exists = db.execute("SELECT id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    if not exists:
        flash("Submission not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
    db.commit()
    flash("Submission deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/submissions/clear", methods=["POST"])
def admin_clear_submissions():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    db.execute("DELETE FROM submissions")
    db.commit()
    flash("All submissions deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM submissions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("User and related submissions deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/clear", methods=["POST"])
def admin_clear_users():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    db.execute("DELETE FROM submissions")
    db.execute("DELETE FROM users")
    db.commit()
    flash("All users and submissions deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export")
def admin_export_csv():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    db = get_db()
    rows = db.execute(
        """
        SELECT u.name, u.email, u.roll_no, q.title, s.language, s.score, s.output, s.submission_time
        FROM submissions s
        JOIN users u ON s.user_id = u.id
        LEFT JOIN contest_questions q ON s.question_id = q.id
        ORDER BY s.submission_time DESC
        """
    ).fetchall()

    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Name", "Email", "Roll Number", "Question", "Language", "Score", "Output", "Submission Time"])
    for row in rows:
        writer.writerow([
            row["name"],
            row["email"],
            row["roll_no"],
            row["title"],
            row["language"],
            row["score"],
            row["output"],
            row["submission_time"],
        ])

    csv_data = stream.getvalue()
    stream.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=contest_results.csv"},
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
