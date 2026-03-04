from datetime import datetime, timedelta
import csv
import io
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time

import requests
from flask_socketio import SocketIO, emit, join_room

from flask import (
    Flask,
    flash,
    g,
    jsonify,
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
DATABASE = os.path.join(tempfile.gettempdir(), "database.db") if IS_VERCEL else os.path.join(BASE_DIR, "database.db")
DB_INITIALIZED = False


def ensure_db_initialized():
    global DB_INITIALIZED
    if DB_INITIALIZED:
        return
    init_db()
    DB_INITIALIZED = True

CONTEST_DURATION_MINUTES = 30
CONTEST_ID = 1

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
DEFAULT_PROGRAM_INPUT = ""

SUPPORTED_LANGUAGES = {
    "python": "Python",
    "javascript": "JavaScript",
    "c": "C",
    "cpp": "C++",
    "java": "Java",
}

JUDGE0_URL = os.getenv("JUDGE0_URL", "https://judge0-ce.p.rapidapi.com/submissions")
JUDGE0_API_KEY = os.getenv("JUDGE0_API_KEY", "")
JUDGE0_API_HOST = os.getenv("JUDGE0_API_HOST", "judge0-ce.p.rapidapi.com")
JUDGE0_POLL_INTERVAL_SECONDS = 0.5
JUDGE0_MAX_POLLS = 10
LOCAL_EXEC_TIMEOUT_SECONDS = 2
JUDGE0_EXEC_TIMEOUT_SECONDS = 2
JUDGE0_LANGUAGE_IDS = {
    "python": 71,
    "javascript": 63,
    "c": 50,
    "cpp": 54,
    "java": 62,
}


def get_db():
    ensure_db_initialized()
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
            input_data TEXT NOT NULL DEFAULT '',
            expected_output TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS code_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            language TEXT NOT NULL DEFAULT 'python',
            code TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, question_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (question_id) REFERENCES contest_questions (id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            contest_id INTEGER NOT NULL,
            start_time TEXT,
            end_time TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            is_disqualified INTEGER NOT NULL DEFAULT 0,
            disqualification_reason TEXT,
            UNIQUE(user_id, contest_id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )

    submission_columns = [row["name"] for row in cursor.execute("PRAGMA table_info(submissions)").fetchall()]
    if "question_id" not in submission_columns:
        cursor.execute("ALTER TABLE submissions ADD COLUMN question_id INTEGER")
    if "language" not in submission_columns:
        cursor.execute("ALTER TABLE submissions ADD COLUMN language TEXT NOT NULL DEFAULT 'python'")

    question_columns = [row["name"] for row in cursor.execute("PRAGMA table_info(contest_questions)").fetchall()]
    question_column_definitions = {
        "title": "TEXT NOT NULL DEFAULT 'Question'",
        "language": "TEXT NOT NULL DEFAULT 'python'",
        "faulty_code": "TEXT NOT NULL DEFAULT ''",
        "correct_code": "TEXT NOT NULL DEFAULT ''",
        "input_data": "TEXT NOT NULL DEFAULT ''",
        "expected_output": "TEXT NOT NULL DEFAULT ''",
        "points": "INTEGER NOT NULL DEFAULT 10",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column_name, column_type in question_column_definitions.items():
        if column_name not in question_columns:
            cursor.execute(f"ALTER TABLE contest_questions ADD COLUMN {column_name} {column_type}")

    user_contest_columns = [row["name"] for row in cursor.execute("PRAGMA table_info(user_contests)").fetchall()]
    user_contest_column_definitions = {
        "user_id": "INTEGER NOT NULL",
        "contest_id": "INTEGER NOT NULL DEFAULT 1",
        "start_time": "TEXT",
        "end_time": "TEXT",
        "violation_count": "INTEGER NOT NULL DEFAULT 0",
        "is_disqualified": "INTEGER NOT NULL DEFAULT 0",
        "disqualification_reason": "TEXT",
    }
    for column_name, column_type in user_contest_column_definitions.items():
        if column_name not in user_contest_columns:
            cursor.execute(f"ALTER TABLE user_contests ADD COLUMN {column_name} {column_type}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("UPDATE contest_questions SET title = 'Question' WHERE title IS NULL OR TRIM(title) = ''")
    cursor.execute("UPDATE contest_questions SET language = ? WHERE language IS NULL OR TRIM(language) = ''", (DEFAULT_LANGUAGE,))
    cursor.execute("UPDATE contest_questions SET faulty_code = '' WHERE faulty_code IS NULL")
    cursor.execute("UPDATE contest_questions SET correct_code = '' WHERE correct_code IS NULL")
    cursor.execute("UPDATE contest_questions SET input_data = '' WHERE input_data IS NULL")
    cursor.execute("UPDATE contest_questions SET expected_output = '' WHERE expected_output IS NULL")
    cursor.execute("UPDATE contest_questions SET points = 10 WHERE points IS NULL OR points < 1")
    cursor.execute("UPDATE contest_questions SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''", (now,))
    cursor.execute("UPDATE contest_questions SET updated_at = ? WHERE updated_at IS NULL OR TRIM(updated_at) = ''", (now,))

    existing_questions = cursor.execute("SELECT id FROM contest_questions LIMIT 1").fetchone()
    if not existing_questions:
        cursor.execute(
            """
            INSERT INTO contest_questions (title, language, faulty_code, correct_code, input_data, expected_output, points, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Question 1",
                DEFAULT_LANGUAGE,
                DEFAULT_FAULTY_CODE,
                DEFAULT_CORRECT_CODE,
                DEFAULT_PROGRAM_INPUT,
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
                    SET faulty_code = ?, correct_code = ?, input_data = ?, expected_output = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        legacy_problem["faulty_code"],
                        legacy_problem["correct_code"],
                        "",
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
    socketio.emit("leaderboard_update", {"updated": True})
    db.close()


def get_questions():
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, input_data, expected_output, points, created_at, updated_at
        FROM contest_questions
        ORDER BY id ASC
        """
    ).fetchall()


def get_question_by_id(question_id):
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, input_data, expected_output, points, created_at, updated_at
        FROM contest_questions
        WHERE id = ?
        """,
        (question_id,),
    ).fetchone()


def get_default_question():
    db = get_db()
    return db.execute(
        """
        SELECT id, title, language, faulty_code, correct_code, input_data, expected_output, points, created_at, updated_at
        FROM contest_questions
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()


def get_code_draft(user_id, question_id):
    db = get_db()
    return db.execute(
        """
        SELECT language, code, updated_at
        FROM code_drafts
        WHERE user_id = ? AND question_id = ?
        """,
        (user_id, question_id),
    ).fetchone()


def save_code_draft(user_id, question_id, language, code):
    db = get_db()
    safe_language = (language or DEFAULT_LANGUAGE).lower().strip()
    if safe_language not in SUPPORTED_LANGUAGES:
        safe_language = DEFAULT_LANGUAGE
    db.execute(
        """
        INSERT INTO code_drafts (user_id, question_id, language, code, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, question_id)
        DO UPDATE SET language = excluded.language,
                      code = excluded.code,
                      updated_at = excluded.updated_at
        """,
        (
            user_id,
            question_id,
            safe_language,
            code,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()


def _parse_datetime_or_none(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _fmt_datetime(value):
    if not value:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


def get_user_contest_record(user_id, contest_id=CONTEST_ID):
    db = get_db()
    record = db.execute(
        """
        SELECT user_id, contest_id, start_time, end_time, violation_count, is_disqualified, disqualification_reason
        FROM user_contests
        WHERE user_id = ? AND contest_id = ?
        """,
        (user_id, contest_id),
    ).fetchone()
    if record:
        return record

    db.execute(
        """
        INSERT INTO user_contests (user_id, contest_id, start_time, end_time, violation_count, is_disqualified, disqualification_reason)
        VALUES (?, ?, NULL, NULL, 0, 0, NULL)
        """,
        (user_id, contest_id),
    )
    db.commit()
    return db.execute(
        """
        SELECT user_id, contest_id, start_time, end_time, violation_count, is_disqualified, disqualification_reason
        FROM user_contests
        WHERE user_id = ? AND contest_id = ?
        """,
        (user_id, contest_id),
    ).fetchone()


def start_user_contest(user_id, contest_id=CONTEST_ID):
    record = get_user_contest_record(user_id, contest_id)
    if record["start_time"]:
        return record

    now = datetime.now()
    end_time = now + timedelta(minutes=CONTEST_DURATION_MINUTES)
    db = get_db()
    db.execute(
        """
        UPDATE user_contests
        SET start_time = ?, end_time = ?
        WHERE user_id = ? AND contest_id = ?
        """,
        (_fmt_datetime(now), _fmt_datetime(end_time), user_id, contest_id),
    )
    db.commit()
    return get_user_contest_record(user_id, contest_id)


def get_user_remaining_seconds(user_id, contest_id=CONTEST_ID):
    record = get_user_contest_record(user_id, contest_id)
    if not record["start_time"] or not record["end_time"]:
        return CONTEST_DURATION_MINUTES * 60
    end_time = _parse_datetime_or_none(record["end_time"])
    if not end_time:
        return 0
    return max(0, int((end_time - datetime.now()).total_seconds()))


def auto_submit_unsubmitted_answers(user_id):
    db = get_db()
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for question in get_questions():
        already_submitted = db.execute(
            "SELECT id FROM submissions WHERE user_id = ? AND question_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, question["id"]),
        ).fetchone()
        if already_submitted:
            continue

        draft = get_code_draft(user_id, question["id"])
        db.execute(
            """
            INSERT INTO submissions (user_id, question_id, language, code, output, score, submission_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                question["id"],
                draft["language"] if draft else question["language"],
                draft["code"] if draft else question["faulty_code"],
                "Auto-submitted due to contest timeout",
                0,
                now_text,
            ),
        )
    db.commit()


def get_user_contest_status(user_id, contest_id=CONTEST_ID):
    record = get_user_contest_record(user_id, contest_id)
    started = bool(record["start_time"])
    remaining = get_user_remaining_seconds(user_id, contest_id)
    is_disqualified = bool(record["is_disqualified"])
    contest_over = started and remaining <= 0

    if contest_over:
        auto_submit_unsubmitted_answers(user_id)

    return {
        "started": started,
        "remaining_seconds": remaining if started else CONTEST_DURATION_MINUTES * 60,
        "is_disqualified": is_disqualified,
        "violation_count": int(record["violation_count"] or 0),
        "disqualification_reason": record["disqualification_reason"] or "",
        "contest_over": contest_over,
    }


def register_violation(user_id, reason, contest_id=CONTEST_ID):
    record = get_user_contest_record(user_id, contest_id)
    next_count = int(record["violation_count"] or 0) + 1
    disqualify_now = next_count >= 3

    db = get_db()
    db.execute(
        """
        UPDATE user_contests
        SET violation_count = ?,
            is_disqualified = CASE WHEN ? THEN 1 ELSE is_disqualified END,
            disqualification_reason = CASE WHEN ? THEN ? ELSE disqualification_reason END
        WHERE user_id = ? AND contest_id = ?
        """,
        (
            next_count,
            1 if disqualify_now else 0,
            1 if disqualify_now else 0,
            reason,
            user_id,
            contest_id,
        ),
    )
    db.commit()
    return {
        "violation_count": next_count,
        "is_disqualified": disqualify_now,
    }


def ensure_submission_allowed(user_id):
    status = get_user_contest_status(user_id)
    if status["is_disqualified"]:
        return False, "Disqualified", "You have been disqualified due to suspicious activity."
    if not status["started"]:
        return False, "Not Started", "Contest not started. Click Participate Now."
    if status["contest_over"]:
        return False, "Time Over", "Contest Time Over"
    return True, "ok", ""


def contest_restriction_response(status, message):
    return {
        "status": status,
        "message": message,
        "output": "",
        "error": message,
    }


def require_student_login():
    return "user_id" in session and not session.get("is_admin", False)


def require_admin_login():
    return session.get("is_admin", False)


OUTPUT_LABEL_PATTERN = re.compile(r"^(\s*)(output:?|result:|answer:)(.*)$", re.IGNORECASE)


def clean_output(output):
    normalized = (output or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines = []

    for raw_line in normalized.split("\n"):
        line = raw_line.rstrip()
        label_match = OUTPUT_LABEL_PATTERN.match(line)
        if label_match:
            leading_spaces = label_match.group(1)
            remaining_text = label_match.group(3)
            if remaining_text == "":
                continue
            line = f"{leading_spaces}{remaining_text}"
        cleaned_lines.append(line)

    while cleaned_lines and cleaned_lines[0] == "":
        cleaned_lines.pop(0)
    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()

    return "\n".join(cleaned_lines)


def build_evaluation_response(status, message, output=""):
    cleaned_message = (message or "").strip()
    cleaned_output = clean_output(output)
    return {
        "status": status,
        "message": "Accepted" if status == "success" else cleaned_message,
        "output": cleaned_output,
        "error": "" if status == "success" else cleaned_message,
    }


def output_matches_expected(program_output, expected_output):
    return clean_output(program_output) == clean_output(expected_output)


def has_forbidden_input_usage(code_text, language):
    source = (code_text or "").lower()
    if language == "c":
        return "scanf" in source
    if language == "cpp":
        return "cin" in source or "scanf" in source
    if language == "python":
        return "input(" in source
    if language == "javascript":
        return "readline" in source or "prompt(" in source
    if language == "java":
        return "scanner" in source or "bufferedreader" in source
    return False


def _judge0_headers():
    headers = {
        "Content-Type": "application/json",
    }
    if JUDGE0_API_KEY:
        headers["X-RapidAPI-Key"] = JUDGE0_API_KEY
        headers["X-RapidAPI-Host"] = JUDGE0_API_HOST
    return headers


def _judge0_fetch_result_by_token(token):
    request_url = f"{JUDGE0_URL}/{token}?base64_encoded=false"
    response = requests.get(request_url, headers=_judge0_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def evaluate_code_with_judge0(user_code, language, expected_output, program_input=""):
    language_id = JUDGE0_LANGUAGE_IDS.get(language)
    if not language_id:
        return build_evaluation_response(
            "runtime_error",
            f"Unsupported language selected: {language}",
            "",
        )

    if not JUDGE0_API_KEY:
        return build_evaluation_response("runtime_error", "Judge0 API key is not configured", "")

    payload = {
        "source_code": user_code,
        "language_id": language_id,
        "stdin": program_input or "",
        "cpu_time_limit": JUDGE0_EXEC_TIMEOUT_SECONDS,
        "wall_time_limit": JUDGE0_EXEC_TIMEOUT_SECONDS,
    }

    try:
        submit_url = f"{JUDGE0_URL}?base64_encoded=false&wait=true"
        response = requests.post(submit_url, json=payload, headers=_judge0_headers(), timeout=20)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as error:
        return build_evaluation_response("runtime_error", str(error), "")

    status_info = result.get("status") or {}
    status_id = status_info.get("id")
    token = result.get("token")
    poll_count = 0
    while status_id in (1, 2) and token and poll_count < JUDGE0_MAX_POLLS:
        time.sleep(JUDGE0_POLL_INTERVAL_SECONDS)
        try:
            result = _judge0_fetch_result_by_token(token)
        except requests.RequestException as error:
            return build_evaluation_response("runtime_error", str(error), "")
        status_info = result.get("status") or {}
        status_id = status_info.get("id")
        poll_count += 1

    if status_id in (1, 2):
        return build_evaluation_response("timeout", "Time Limit Exceeded", "")

    compile_output = (result.get("compile_output") or "").strip()
    stderr_output = (result.get("stderr") or "").strip()
    stdout_output = result.get("stdout") or ""

    if status_id == 6 or compile_output:
        return build_evaluation_response("compile_error", compile_output or "Compilation failed", "")

    if status_id == 5:
        return build_evaluation_response("timeout", "Time Limit Exceeded", "")

    if status_id in (7, 8, 9, 10, 11, 12, 13, 14) or stderr_output:
        return build_evaluation_response(
            "runtime_error",
            stderr_output or "Runtime Error",
            stdout_output,
        )

    if output_matches_expected(stdout_output, expected_output):
        return build_evaluation_response("success", "Accepted", stdout_output)

    return build_evaluation_response(
        "wrong_output",
        "Wrong Answer",
        stdout_output,
    )


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate_code():
    if not require_student_login():
        return jsonify(build_evaluation_response("runtime_error", "Please login first", "")), 401

    user_id = session["user_id"]
    allowed, status, message = ensure_submission_allowed(user_id)
    if not allowed:
        return jsonify(contest_restriction_response(status, message)), 403

    payload = request.get_json(silent=True) or request.form
    question_id_raw = payload.get("question_id") if hasattr(payload, "get") else None
    try:
        question_id = int(question_id_raw)
    except (TypeError, ValueError):
        question_id = None
    question = get_question_by_id(question_id) if question_id else None
    if not question:
        return jsonify(build_evaluation_response("runtime_error", "Invalid question selected.", "")), 400

    user_code = payload.get("user_code") or payload.get("code") or ""
    selected_language = (payload.get("language") or question["language"] or DEFAULT_LANGUAGE).lower().strip()
    if selected_language not in SUPPORTED_LANGUAGES:
        selected_language = question["language"] if question["language"] in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    if not user_code.strip():
        return jsonify(build_evaluation_response("runtime_error", "Code cannot be empty.", "")), 400

    save_code_draft(user_id, question_id, selected_language, user_code)

    result = run_student_code(
        user_code,
        question["expected_output"],
        selected_language,
        question["input_data"],
    )
    if result["status"] == "success":
        result["message"] = "No errors detected. You can submit your code."
        result["error"] = ""
    return jsonify(result)


def _run_compiled_c_binary(executable_path, program_input, working_dir):
    process = subprocess.Popen(
        [executable_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=working_dir,
    )

    try:
        stdout_text, stderr_text = process.communicate(
            input=program_input or "",
            timeout=LOCAL_EXEC_TIMEOUT_SECONDS,
        )
        return {
            "timed_out": False,
            "returncode": process.returncode,
            "stdout": stdout_text or "",
            "stderr": (stderr_text or "").strip(),
        }
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_text, stderr_text = process.communicate()
        return {
            "timed_out": True,
            "returncode": process.returncode,
            "stdout": stdout_text or "",
            "stderr": (stderr_text or "").strip(),
        }


def run_student_code(code_text, expected_output, language, program_input=""):
    language = (language or DEFAULT_LANGUAGE).lower().strip()
    if language not in SUPPORTED_LANGUAGES:
        return build_evaluation_response("runtime_error", "Unsupported language selected", "")

    input_payload = program_input if program_input is not None else ""
    if not input_payload.strip() and has_forbidden_input_usage(code_text, language):
        return build_evaluation_response("runtime_error", "Input not allowed for this problem", "")

    # On serverless hosts (like Vercel), C compilers are usually unavailable.
    # Prefer Judge0 for C execution when API credentials are configured.
    if language == "c" and JUDGE0_API_KEY:
        return evaluate_code_with_judge0(code_text, language, expected_output, input_payload)

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            if language == "c":
                if not shutil.which("gcc"):
                    return build_evaluation_response(
                        "compile_error",
                        "GCC is not installed on server.",
                        "",
                    )

                source_file = os.path.join(temp_dir, "user_code.c")
                executable_file = os.path.join(temp_dir, "user_code.exe")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)

                compile_result = subprocess.run(
                    ["gcc", source_file, "-o", executable_file],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=temp_dir,
                )
                if compile_result.returncode != 0:
                    compiler_error = (compile_result.stderr or compile_result.stdout or "Compilation Error").strip()
                    return build_evaluation_response("compile_error", compiler_error, "")

                execution_result = _run_compiled_c_binary(executable_file, program_input, temp_dir)
                if execution_result["timed_out"]:
                    return build_evaluation_response("timeout", "Time Limit Exceeded", execution_result["stdout"])

                stdout_output = execution_result["stdout"]
                stderr_text = execution_result["stderr"]

                if execution_result["returncode"] != 0:
                    return build_evaluation_response(
                        "runtime_error",
                        stderr_text or "Runtime Error",
                        stdout_output,
                    )

                if output_matches_expected(stdout_output, expected_output):
                    return build_evaluation_response("success", "Accepted", stdout_output)

                return build_evaluation_response(
                    "wrong_output",
                    "Wrong Answer",
                    stdout_output,
                )

            if language == "python":
                source_file = os.path.join(temp_dir, "main.py")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)
                execution_result = subprocess.run(
                    [sys.executable, source_file],
                    capture_output=True,
                    text=True,
                    input=input_payload,
                    timeout=LOCAL_EXEC_TIMEOUT_SECONDS,
                    cwd=temp_dir,
                )
            elif language == "javascript":
                if not shutil.which("node"):
                    return build_evaluation_response("runtime_error", "Node.js is not installed on server", "")
                source_file = os.path.join(temp_dir, "main.js")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)
                execution_result = subprocess.run(
                    ["node", source_file],
                    capture_output=True,
                    text=True,
                    input=input_payload,
                    timeout=LOCAL_EXEC_TIMEOUT_SECONDS,
                    cwd=temp_dir,
                )
            elif language == "cpp":
                if not shutil.which("g++"):
                    return build_evaluation_response("compile_error", "G++ is not installed on server", "")
                source_file = os.path.join(temp_dir, "main.cpp")
                executable_file = os.path.join(temp_dir, "main.exe")
                with open(source_file, "w", encoding="utf-8") as temp_file:
                    temp_file.write(code_text)
                compile_result = subprocess.run(
                    ["g++", source_file, "-o", executable_file],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=temp_dir,
                )
                if compile_result.returncode != 0:
                    return build_evaluation_response(
                        "compile_error",
                        (compile_result.stderr or compile_result.stdout or "Compilation Error").strip(),
                        "",
                    )
                execution_result = subprocess.run(
                    [executable_file],
                    capture_output=True,
                    text=True,
                    input=input_payload,
                    timeout=LOCAL_EXEC_TIMEOUT_SECONDS,
                    cwd=temp_dir,
                )
            else:
                if not shutil.which("javac") or not shutil.which("java"):
                    return build_evaluation_response("compile_error", "Java JDK is not installed on server", "")
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
                    return build_evaluation_response(
                        "compile_error",
                        (compile_result.stderr or compile_result.stdout or "Compilation Error").strip(),
                        "",
                    )
                execution_result = subprocess.run(
                    ["java", "-cp", temp_dir, "Main"],
                    capture_output=True,
                    text=True,
                    input=input_payload,
                    timeout=LOCAL_EXEC_TIMEOUT_SECONDS,
                    cwd=temp_dir,
                )

            stdout_output = execution_result.stdout or ""
            stderr_text = (execution_result.stderr or "").strip()
            if execution_result.returncode != 0:
                return build_evaluation_response(
                    "runtime_error",
                    stderr_text or "Runtime Error",
                    stdout_output,
                )

            if output_matches_expected(stdout_output, expected_output):
                return build_evaluation_response("success", "Accepted", stdout_output)

            return build_evaluation_response(
                "wrong_output",
                "Wrong Answer",
                stdout_output,
            )
        except subprocess.TimeoutExpired:
            return build_evaluation_response("timeout", "Time Limit Exceeded", "")
        except Exception as error:
            return build_evaluation_response("runtime_error", str(error), "")


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
    contest_status = get_user_contest_status(user_id)

    questions = get_questions()
    if not questions:
        return render_template(
            "contest.html",
            questions=[],
            selected_question=None,
            faulty_code="",
            question_input="",
            expected_output="",
            time_left=contest_status["remaining_seconds"],
            locked=(not contest_status["started"]) or contest_status["contest_over"] or contest_status["is_disqualified"],
            already_correct=False,
            last_submission=None,
            contest_started=contest_status["started"],
            is_disqualified=contest_status["is_disqualified"],
            contest_over=contest_status["contest_over"],
            violation_count=contest_status["violation_count"],
            disqualification_reason=contest_status["disqualification_reason"],
        )

    selected_question_id = request.args.get("q", type=int)
    selected_question = get_question_by_id(selected_question_id) if selected_question_id else None
    if not selected_question:
        selected_question = questions[0]

    draft = get_code_draft(user_id, selected_question["id"])
    editor_code = draft["code"] if draft and draft["code"].strip() else selected_question["faulty_code"]
    selected_language = (
        draft["language"]
        if draft and draft["language"] in SUPPORTED_LANGUAGES
        else selected_question["language"]
    )

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

    time_left = contest_status["remaining_seconds"]
    locked = (not contest_status["started"]) or contest_status["contest_over"] or contest_status["is_disqualified"]
    compile_feedback = session.get("compile_feedback")
    if compile_feedback and compile_feedback.get("question_id") == selected_question["id"]:
        session.pop("compile_feedback", None)
    else:
        compile_feedback = None

    return render_template(
        "contest.html",
        questions=questions,
        supported_languages=SUPPORTED_LANGUAGES,
        selected_question=selected_question,
        faulty_code=selected_question["faulty_code"],
        editor_code=editor_code,
        selected_language=selected_language,
        expected_output=selected_question["expected_output"],
        question_input=selected_question["input_data"],
        time_left=time_left,
        locked=locked,
        already_correct=already_correct,
        last_submission=last_submission,
        compile_feedback=compile_feedback,
        contest_started=contest_status["started"],
        is_disqualified=contest_status["is_disqualified"],
        contest_over=contest_status["contest_over"],
        violation_count=contest_status["violation_count"],
        disqualification_reason=contest_status["disqualification_reason"],
    )


@app.route("/contest/start", methods=["POST"])
def start_contest():
    if not require_student_login():
        return redirect(url_for("login"))

    user_id = session["user_id"]
    start_user_contest(user_id)
    status = get_user_contest_status(user_id)
    socketio.emit("contest_status", status, room=f"user_{user_id}")
    return redirect(url_for("contest"))


@app.route("/api/contest/status", methods=["GET"])
def api_contest_status():
    if not require_student_login():
        return jsonify({"status": "runtime_error", "message": "Please login first"}), 401

    user_id = session["user_id"]
    status = get_user_contest_status(user_id)
    return jsonify(
        {
            "status": "ok",
            "message": "Contest status fetched",
            **status,
        }
    )


@app.route("/api/contest/violation", methods=["POST"])
def api_contest_violation():
    if not require_student_login():
        return jsonify({"status": "runtime_error", "message": "Please login first"}), 401

    user_id = session["user_id"]
    payload = request.get_json(silent=True) or request.form
    reason = (payload.get("reason") or "Suspicious activity detected").strip()
    violation_result = register_violation(user_id, reason)
    contest_status = get_user_contest_status(user_id)

    if violation_result["is_disqualified"]:
        socketio.emit(
            "contest_disqualified",
            {
                "status": "Disqualified",
                "message": "You have been disqualified due to suspicious activity.",
            },
            room=f"user_{user_id}",
        )

    socketio.emit("contest_status", contest_status, room=f"user_{user_id}")

    return jsonify(
        {
            "status": "Disqualified" if violation_result["is_disqualified"] else "ok",
            "message": "You have been disqualified due to suspicious activity."
            if violation_result["is_disqualified"]
            else "Violation recorded",
            "violation_count": violation_result["violation_count"],
            "is_disqualified": violation_result["is_disqualified"],
        }
    )


@socketio.on("join_contest")
def ws_join_contest(_payload=None):
    if not session.get("user_id"):
        emit("contest_status", {"status": "runtime_error", "message": "Please login first"})
        return
    user_id = session["user_id"]
    join_room(f"user_{user_id}")
    emit("contest_status", get_user_contest_status(user_id))


@app.route("/compile", methods=["POST"])
def compile_code():
    if not require_student_login():
        return redirect(url_for("login"))

    user_id = session["user_id"]
    allowed, status, message = ensure_submission_allowed(user_id)
    if not allowed:
        flash(message, "error")
        return redirect(url_for("contest"))

    question_id = request.form.get("question_id", type=int)
    question = get_question_by_id(question_id) if question_id else None
    if not question:
        flash("Invalid question selected.", "error")
        return redirect(url_for("contest"))

    selected_language = request.form.get("language", DEFAULT_LANGUAGE).lower().strip()
    if selected_language not in SUPPORTED_LANGUAGES:
        selected_language = question["language"] if question["language"] in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    code = request.form.get("code", "")
    if not code.strip():
        session["compile_feedback"] = {
            "question_id": question_id,
            "status": "error",
            "message": "Code cannot be empty.",
            "details": "Code cannot be empty.",
        }
        return redirect(url_for("contest", q=question_id))

    save_code_draft(user_id, question_id, selected_language, code)

    result = run_student_code(code, question["expected_output"], selected_language, question["input_data"])
    if result["status"] == "success":
        session["compile_feedback"] = {
            "question_id": question_id,
            "status": "success",
            "message": "No errors detected. You can submit your code.",
            "details": "",
        }
    else:
        details = ""
        if result["status"] == "wrong_output":
            details = (
                f"Your Output:\n{result['output']}\n\n"
                f"Expected Output:\n{question['expected_output']}"
            )
        elif result["output"]:
            details = f"Program Output:\n{result['output']}"
        session["compile_feedback"] = {
            "question_id": question_id,
            "status": "error",
            "message": result["message"],
            "details": details,
        }

    return redirect(url_for("contest", q=question_id))


@app.route("/submit", methods=["POST"])
def submit():
    if not require_student_login():
        return redirect(url_for("login"))

    db = get_db()
    user_id = session["user_id"]

    allowed, status, message = ensure_submission_allowed(user_id)
    if not allowed:
        flash(message, "error")
        return redirect(url_for("contest"))

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

    save_code_draft(user_id, question_id, selected_language, code)

    result = run_student_code(code, question["expected_output"], selected_language, question["input_data"])
    final_score = question["points"] if result["status"] == "success" else 0
    submission_output = result["output"] or result["message"]

    db.execute(
        "INSERT INTO submissions (user_id, question_id, language, code, output, score, submission_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            question_id,
            selected_language,
            code,
            submission_output,
            final_score,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    if final_score > 0:
        flash(f"Correct! Score: {final_score}", "success")
    elif result["status"] == "compile_error":
        flash(result["message"], "error")
    elif result["status"] == "timeout":
        flash("Time Limit Exceeded", "error")
    elif result["status"] == "runtime_error":
        flash("Runtime Error", "error")
    else:
        flash("Wrong Answer", "error")
    return redirect(url_for("contest", q=question_id))


@app.route("/api/submit", methods=["POST"])
def api_submit_code():
    if not require_student_login():
        return jsonify(build_evaluation_response("runtime_error", "Please login first", "")), 401

    db = get_db()
    user_id = session["user_id"]

    allowed, status, message = ensure_submission_allowed(user_id)
    if not allowed:
        return jsonify(contest_restriction_response(status, message)), 403

    payload = request.get_json(silent=True) or request.form

    question_id_raw = payload.get("question_id") if hasattr(payload, "get") else None
    try:
        question_id = int(question_id_raw)
    except (TypeError, ValueError):
        question_id = None

    question = get_question_by_id(question_id) if question_id else None
    if not question:
        return jsonify(build_evaluation_response("runtime_error", "Invalid question selected.", "")), 400

    selected_language = (payload.get("language") or DEFAULT_LANGUAGE).lower().strip()
    if selected_language not in SUPPORTED_LANGUAGES:
        selected_language = question["language"] if question["language"] in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    code = payload.get("code") or payload.get("user_code") or ""
    if not code.strip():
        return jsonify(build_evaluation_response("runtime_error", "Code cannot be empty.", "")), 400

    already_correct = db.execute(
        "SELECT id FROM submissions WHERE user_id = ? AND question_id = ? AND score > 0 ORDER BY id DESC LIMIT 1",
        (user_id, question_id),
    ).fetchone()
    if already_correct:
        return jsonify(build_evaluation_response("runtime_error", "Submission blocked. Already solved this question.", "")), 409

    save_code_draft(user_id, question_id, selected_language, code)

    result = run_student_code(code, question["expected_output"], selected_language, question["input_data"])
    final_score = question["points"] if result["status"] == "success" else 0
    submission_output = result["output"] or result["message"]

    db.execute(
        "INSERT INTO submissions (user_id, question_id, language, code, output, score, submission_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            question_id,
            selected_language,
            code,
            submission_output,
            final_score,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    response = {
        "status": result["status"],
        "message": result["message"],
        "output": result["output"],
        "error": result["error"],
        "score": final_score,
        "leaderboard_updated": True,
    }
    socketio.emit("leaderboard_update", {"updated": True})
    return jsonify(response)


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
        LEFT JOIN user_contests uc ON u.id = uc.user_id AND uc.contest_id = ?
        LEFT JOIN (
            SELECT user_id, question_id, MAX(score) AS best_score, MIN(submission_time) AS first_submission_time
            FROM submissions
            GROUP BY user_id, question_id
        ) best ON u.id = best.user_id
        WHERE COALESCE(uc.is_disqualified, 0) = 0
        GROUP BY u.id
        ORDER BY total_score DESC, first_submission_time ASC
        """
    , (CONTEST_ID,)).fetchall()
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
    input_data = request.form.get("input_data", "")
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
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """
            INSERT INTO contest_questions (title, language, faulty_code, correct_code, input_data, expected_output, points, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                language,
                faulty_code,
                correct_code,
                input_data,
                expected_output,
                points,
                now,
                now,
            ),
        )
        db.commit()
        flash("Question added successfully.", "success")
    except sqlite3.Error as error:
        db.rollback()
        flash(f"Unable to add question: {error}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/question/<int:question_id>/update", methods=["POST"])
def admin_update_question(question_id):
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    title = request.form.get("title", "").strip()
    language = request.form.get("language", DEFAULT_LANGUAGE).lower().strip()
    faulty_code = request.form.get("faulty_code", "").strip()
    correct_code = request.form.get("correct_code", "").strip()
    input_data = request.form.get("input_data", "")
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

    try:
        db.execute(
            """
            UPDATE contest_questions
            SET title = ?, language = ?, faulty_code = ?, correct_code = ?, input_data = ?, expected_output = ?, points = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                language,
                faulty_code,
                correct_code,
                input_data,
                expected_output,
                points,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                question_id,
            ),
        )
        db.commit()
        flash("Question updated successfully.", "success")
    except sqlite3.Error as error:
        db.rollback()
        flash(f"Unable to update question: {error}", "error")
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
    db.execute("DELETE FROM code_drafts WHERE question_id = ?", (question_id,))
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
    db.execute("DELETE FROM code_drafts")
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
    db.execute("DELETE FROM code_drafts WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM user_contests WHERE user_id = ?", (user_id,))
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
    db.execute("DELETE FROM code_drafts")
    db.execute("DELETE FROM user_contests")
    db.execute("DELETE FROM users")
    db.commit()
    flash("All users and submissions deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/delete-by-prefix", methods=["POST"])
def admin_delete_users_by_prefix():
    if not require_admin_login():
        return redirect(url_for("admin_login"))

    prefix = request.form.get("roll_prefix", "").strip()
    if not prefix:
        flash("Roll prefix is required.", "error")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    users = db.execute(
        "SELECT id FROM users WHERE roll_no LIKE ?",
        (f"{prefix}%",),
    ).fetchall()

    if not users:
        flash(f"No users found with roll prefix '{prefix}'.", "error")
        return redirect(url_for("admin_dashboard"))

    user_ids = [row["id"] for row in users]
    placeholders = ",".join(["?"] * len(user_ids))

    db.execute(f"DELETE FROM submissions WHERE user_id IN ({placeholders})", user_ids)
    db.execute(f"DELETE FROM code_drafts WHERE user_id IN ({placeholders})", user_ids)
    db.execute(f"DELETE FROM user_contests WHERE user_id IN ({placeholders})", user_ids)
    db.execute(f"DELETE FROM users WHERE id IN ({placeholders})", user_ids)
    db.commit()

    flash(f"Deleted {len(user_ids)} user(s) with roll prefix '{prefix}'.", "success")
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
    ensure_db_initialized()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
