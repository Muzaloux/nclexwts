from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
LEGACY_ACCOUNTS_FILE = BASE_DIR / "local_accounts.json"


def load_local_env() -> None:
    env_file = BASE_DIR / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env()

app = Flask(__name__)
app.secret_key = os.getenv("NCLEX_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=5),
    MAX_CONTENT_LENGTH=64 * 1024,
)

QUESTION_BANKS = {
    "type_one": {"label": "NCLEX Sequential Exam", "file": "exam_questions.json"},
    "main": {"label": "NCLEX Sequential Exam", "file": "exam_questions.json"},
}

ACCESS_KEYS = {
    "TYPEONE123456": {"key": "TYPE-ONE-2026", "plan": "type_one"},
    "MAIN987654": {"key": "MAIN-PLAN-2026", "plan": "main"},
}

ORIENTATION_SLIDES = [
    ("Welcome to the NCLEX Simulation Exam", "This guided simulation recreates the focus, timing, and one-way progression of a professional licensure exam."),
    ("How the NCLEX works", "Questions assess clinical judgment, safety, prioritisation, and nursing knowledge. Read the complete stem before evaluating every option."),
    ("Question navigation rules", "You will move forward only. Submitting an answer permanently locks it; previous questions cannot be reopened or changed."),
    ("Break policy", "A break becomes available after three continuous hours. Your position is securely saved when an eligible break begins."),
    ("Exam integrity rules", "Remain in fullscreen, keep this tab active, and do not use outside resources. Focus changes and fullscreen exits are logged."),
    ("Scoring overview", "Your result is calculated after all questions are completed. Rationales appear immediately only when Learning Mode is enabled."),
    ("Final instructions", "Confirm your device is powered, your connection is stable, and interruptions are minimised. The five-hour timer begins when you start."),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
# Database — PostgreSQL via psycopg2
# ---------------------------------------------------------------------------

class _PGConn:
    """Thin wrapper that gives psycopg2 the same connection.execute() interface
    used throughout the app (originally written against sqlite3)."""

    def __init__(self) -> None:
        self._conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def execute(self, sql: str, params=()):
        # sqlite3 uses ? placeholders; psycopg2 uses %s
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(sql, params if params else None)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()


def db() -> _PGConn:
    return _PGConn()


def init_db() -> None:
    schema_statements = [
        """CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT,
            registration_id TEXT NOT NULL,
            att_number TEXT NOT NULL,
            exam_type TEXT NOT NULL CHECK(exam_type IN ('RN','CNA','LPN')),
            phone TEXT NOT NULL,
            plan TEXT NOT NULL,
            last_login_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(email, att_number)
        )""",
        """CREATE TABLE IF NOT EXISTS exam_sessions (
            id TEXT PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            device_hash TEXT,
            status TEXT NOT NULL DEFAULT 'created',
            agreement_accepted_at TEXT,
            orientation_complete INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            expires_at TEXT,
            break_started_at TEXT,
            break_seconds INTEGER NOT NULL DEFAULT 0,
            current_qid INTEGER NOT NULL DEFAULT 1,
            completed_at TEXT,
            invalidated_at TEXT,
            score REAL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS answers (
            session_id TEXT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
            qid INTEGER NOT NULL,
            answer_json TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            answered_at TEXT NOT NULL,
            PRIMARY KEY(session_id, qid)
        )""",
        """CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            session_id TEXT,
            candidate_id INTEGER,
            event TEXT NOT NULL,
            details TEXT,
            ip_hash TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        "INSERT INTO settings(key,value) VALUES ('show_answers_immediately','0') ON CONFLICT DO NOTHING",
        # Migrate exam_type constraint to RN/CNA/LPN
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'candidates_exam_type_check'
                  AND conrelid = 'candidates'::regclass
                  AND pg_get_constraintdef(oid) NOT LIKE '%CNA%'
            ) THEN
                ALTER TABLE candidates DROP CONSTRAINT candidates_exam_type_check;
                ALTER TABLE candidates ADD CONSTRAINT candidates_exam_type_check
                    CHECK (exam_type IN ('RN','CNA','LPN'));
            END IF;
        END $$""",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS session_token TEXT",
        "ALTER TABLE exam_sessions ADD COLUMN IF NOT EXISTS token_verified INTEGER NOT NULL DEFAULT 0",
    ]
    with db() as connection:
        for stmt in schema_statements:
            connection.execute(stmt)

        # Migrate legacy JSON accounts if the file exists locally
        if LEGACY_ACCOUNTS_FILE.exists():
            try:
                legacy = json.loads(LEGACY_ACCOUNTS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                legacy = {}
            for account in legacy.values():
                email = account.get("email", "").strip().lower()
                att = account.get("att_number", "").strip().upper()
                if email and att:
                    connection.execute(
                        """INSERT INTO candidates
                           (full_name,email,registration_id,att_number,exam_type,phone,plan,created_at)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT DO NOTHING""",
                        (
                            account.get("candidate_name") or "Legacy Candidate",
                            email,
                            f"LEGACY-{att[-6:]}",
                            att,
                            "RN",
                            "Not provided",
                            account.get("plan", "type_one"),
                            iso(),
                        ),
                    )


def normalize_question(raw: dict) -> dict:
    if "stem" in raw:
        return {
            "id": raw["id"],
            "domain": raw.get("domain"),
            "difficulty": raw.get("difficulty"),
            "type": raw.get("type", "MCQ"),
            "is_sata": raw.get("type", "").upper() == "SATA",
            "image_prompt": raw.get("image_prompt"),
            "image": raw.get("image"),
            "scenario": raw.get("scenario"),
            "question": raw["stem"],
            "options": raw["options"],
            "answer": raw["correct_answers"],
            "explanation": raw.get("rationale_correct", ""),
        }
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    options = [{"letter": letters[i], "text": text} for i, text in enumerate(raw["options"])]
    answer_text = raw["answer"] if isinstance(raw["answer"], list) else [raw["answer"]]
    return {
        "id": raw["id"],
        "domain": raw.get("domain"),
        "difficulty": raw.get("difficulty"),
        "type": raw.get("type", "single"),
        "is_sata": isinstance(raw["answer"], list) or raw.get("type", "").upper() == "SATA",
        "image_prompt": raw.get("image_prompt"),
        "image": raw.get("image"),
        "scenario": raw.get("scenario"),
        "question": raw["question"],
        "options": options,
        "answer": [o["letter"] for o in options if o["text"] in answer_text],
        "explanation": raw.get("explanation", ""),
    }


def load_questions(plan: str) -> list[dict]:
    bank = QUESTION_BANKS.get(plan, QUESTION_BANKS["type_one"])
    raw = json.loads((BASE_DIR / bank["file"]).read_text(encoding="utf-8"))
    return [normalize_question(question) for question in raw]


def answers_match(selected: list[str], correct: list[str]) -> bool:
    return sorted(selected or []) == sorted(correct or [])


def setting(key: str, default: str = "0") -> str:
    with db() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def current_candidate():
    candidate_id = session.get("candidate_id")
    if not candidate_id:
        return None
    with db() as connection:
        return connection.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()


def current_exam():
    exam_id = session.get("exam_session_id")
    if not exam_id:
        return None
    with db() as connection:
        return connection.execute("SELECT * FROM exam_sessions WHERE id=?", (exam_id,)).fetchone()


def latest_open_exam(candidate_id: int):
    with db() as connection:
        return connection.execute(
            """SELECT * FROM exam_sessions
               WHERE candidate_id=? AND status IN ('created','active','break')
               ORDER BY created_at DESC LIMIT 1""",
            (candidate_id,),
        ).fetchone()


def log_event(event: str, details: str = "", exam_id: str | None = None, candidate_id: int | None = None) -> None:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0]
    ip_hash = hashlib.sha256(ip.strip().encode()).hexdigest()[:16]
    with db() as connection:
        connection.execute(
            "INSERT INTO activity_log(session_id,candidate_id,event,details,ip_hash,created_at) VALUES(?,?,?,?,?,?)",
            (exam_id or session.get("exam_session_id"), candidate_id or session.get("candidate_id"), event, details[:500], ip_hash, iso()),
        )


def candidate_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_candidate():
            session.clear()
            flash("Please verify your identity to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def exam_required(view):
    @wraps(view)
    @candidate_required
    def wrapped(*args, **kwargs):
        exam = current_exam()
        if not exam:
            return redirect(url_for("dashboard"))
        if not exam["agreement_accepted_at"]:
            return redirect(url_for("agreement"))
        if not exam["orientation_complete"]:
            return redirect(url_for("orientation"))
        return view(*args, **kwargs)
    return wrapped


def create_exam_session(candidate_id: int) -> str:
    exam_id = str(uuid.uuid4())
    with db() as connection:
        connection.execute(
            "INSERT INTO exam_sessions(id,candidate_id,status,created_at) VALUES(?,?,?,?)",
            (exam_id, candidate_id, "created", iso()),
        )
    session["exam_session_id"] = exam_id
    return exam_id


def attach_candidate_session(candidate_id: int) -> str:
    exam = latest_open_exam(candidate_id)
    if exam:
        session["exam_session_id"] = exam["id"]
        return exam["id"]
    return create_exam_session(candidate_id)


def post_login_destination():
    exam = current_exam()
    if not exam or not exam["agreement_accepted_at"]:
        return redirect(url_for("agreement"))
    if not exam["orientation_complete"]:
        return redirect(url_for("orientation"))
    return redirect(url_for("dashboard"))


def validate_identity(form) -> list[str]:
    errors = []
    if len(form.get("full_name", "").strip()) < 3:
        errors.append("Enter your full legal name.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form.get("email", "").strip()):
        errors.append("Enter a valid email address.")
    if not re.fullmatch(r"[A-Za-z0-9-]{5,30}", form.get("registration_id", "").strip()):
        errors.append("Registration ID must be 5–30 letters, numbers, or hyphens.")
    if not re.fullmatch(r"[A-Za-z0-9]{8,30}", form.get("att_number", "").strip()):
        errors.append("Enter a valid ATT number (8–30 letters or numbers).")
    if form.get("exam_type") not in {"RN", "CNA", "LPN"}:
        errors.append("Select RN, CNA, or LPN.")
    if not re.fullmatch(r"[+()\d\s-]{7,24}", form.get("phone", "").strip()):
        errors.append("Enter a valid phone number.")
    return errors


def validate_password(form) -> list[str]:
    password = form.get("password", "")
    confirm = form.get("confirm_password", "")
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        errors.append("Password must include at least one letter and one number.")
    if password != confirm:
        errors.append("Password confirmation does not match.")
    return errors


def compact(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


@app.context_processor
def template_context():
    return {"candidate": current_candidate(), "exam": current_exam(), "now_year": utcnow().year}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        reg = compact(request.form.get("registration_id", ""))
        att = compact(request.form.get("att_number", ""))
        with db() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE att_number=? AND registration_id=?",
                (att.upper(), reg.upper()),
            ).fetchone()
        if not candidate:
            # Fallback: compare normalised values in Python to tolerate case/spacing
            with db() as connection:
                rows = connection.execute("SELECT * FROM candidates").fetchall()
            candidate = next(
                (r for r in rows if compact(r["att_number"]) == att and compact(r["registration_id"]) == reg),
                None,
            )
        if candidate:
            session.clear()
            session.permanent = True
            session["candidate_id"] = candidate["id"]
            attach_candidate_session(candidate["id"])
            with db() as connection:
                connection.execute("UPDATE candidates SET last_login_at=? WHERE id=?", (iso(), candidate["id"]))
            log_event("login", "Candidate identity verified")
            return post_login_destination()
        flash("No candidate record matches those credentials.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        flash("Candidate records are issued before examination. Sign in with your assigned exam credentials.", "warning")
        return redirect(url_for("login"))
    if request.method == "POST":
        errors = validate_identity(request.form)
        errors.extend(validate_password(request.form))
        att = request.form.get("att_number", "").strip().upper()
        access = ACCESS_KEYS.get(att)
        if not access or not secrets.compare_digest(access["key"], request.form.get("access_key", "").strip().upper()):
            errors.append("ATT number or package key is not valid.")
        if not errors:
            password_hash = generate_password_hash(request.form["password"])
            values = (
                request.form["full_name"].strip(),
                request.form["email"].strip().lower(),
                password_hash,
                request.form["registration_id"].strip().upper(),
                att,
                request.form["exam_type"],
                request.form["phone"].strip(),
                access["plan"],
                iso(),
            )
            try:
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO candidates(full_name,email,password_hash,registration_id,att_number,exam_type,phone,plan,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?) RETURNING id""",
                        values,
                    )
                    candidate_id = cursor.fetchone()["id"]
            except psycopg2.IntegrityError:
                with db() as connection:
                    existing = connection.execute(
                        "SELECT * FROM candidates WHERE email=? AND att_number=?",
                        (request.form["email"].strip().lower(), att),
                    ).fetchone()
                    if existing and not existing["password_hash"]:
                        connection.execute(
                            """UPDATE candidates
                               SET full_name=?, password_hash=?, registration_id=?, exam_type=?, phone=?, plan=?
                               WHERE id=?""",
                            (
                                request.form["full_name"].strip(),
                                password_hash,
                                request.form["registration_id"].strip().upper(),
                                request.form["exam_type"],
                                request.form["phone"].strip(),
                                access["plan"],
                                existing["id"],
                            ),
                        )
                        candidate_id = existing["id"]
                    else:
                        candidate_id = None
                if candidate_id is None:
                    errors.append("An account already exists for this email and ATT number. Please sign in.")
                else:
                    session.clear()
                    session.permanent = True
                    session["candidate_id"] = candidate_id
                    exam_id = attach_candidate_session(candidate_id)
                    log_event("candidate_secured", "Existing identity record secured with password", exam_id, candidate_id)
                    return redirect(url_for("agreement"))
            else:
                session.clear()
                session.permanent = True
                session["candidate_id"] = candidate_id
                exam_id = create_exam_session(candidate_id)
                log_event("candidate_registered", "Identity record created", exam_id, candidate_id)
                return redirect(url_for("agreement"))
        for error in errors:
            flash(error, "error")
    return render_template("register.html")


@app.route("/agreement", methods=["GET", "POST"])
@candidate_required
def agreement():
    exam = current_exam()
    if not exam or exam["status"] in {"completed", "invalidated"}:
        create_exam_session(current_candidate()["id"])
        exam = current_exam()
    if request.method == "POST":
        if request.form.get("accept") != "yes":
            flash("You must confirm and accept the agreement before continuing.", "error")
        else:
            with db() as connection:
                connection.execute("UPDATE exam_sessions SET agreement_accepted_at=? WHERE id=?", (iso(), exam["id"]))
            log_event("agreement_accepted", "Candidate accepted exam rules")
            return redirect(url_for("orientation"))
    return render_template("agreement.html")


@app.route("/orientation")
@candidate_required
def orientation():
    exam = current_exam()
    if not exam or not exam["agreement_accepted_at"]:
        return redirect(url_for("agreement"))
    return render_template("orientation.html", slides=ORIENTATION_SLIDES)


@app.post("/orientation/complete")
@candidate_required
def complete_orientation():
    exam = current_exam()
    if not exam or not exam["agreement_accepted_at"]:
        abort(403)
    with db() as connection:
        connection.execute("UPDATE exam_sessions SET orientation_complete=1 WHERE id=?", (exam["id"],))
    log_event("orientation_completed", "All seven slides viewed")
    candidate = current_candidate()
    if candidate.get("session_token") and not exam.get("token_verified"):
        return redirect(url_for("exam_token"))
    return redirect(url_for("dashboard"))


@app.route("/")
@candidate_required
def dashboard():
    exam = current_exam()
    if not exam:
        create_exam_session(current_candidate()["id"])
        return redirect(url_for("agreement"))
    if not exam["agreement_accepted_at"]:
        return redirect(url_for("agreement"))
    if not exam["orientation_complete"]:
        return redirect(url_for("orientation"))
    candidate = current_candidate()
    if candidate.get("session_token") and not exam.get("token_verified"):
        return redirect(url_for("exam_token"))
    questions = load_questions(candidate["plan"])
    with db() as connection:
        answered = connection.execute("SELECT COUNT(*) count FROM answers WHERE session_id=?", (exam["id"],)).fetchone()["count"]
    return render_template(
        "dashboard.html",
        total=len(questions),
        answered=answered,
        plan_label=QUESTION_BANKS[candidate["plan"]]["label"],
        remaining_seconds=remaining_seconds(exam),
    )


def remaining_seconds(exam) -> int:
    if not exam or not exam["expires_at"]:
        return 5 * 60 * 60
    return max(0, int((parse_dt(exam["expires_at"]) - utcnow()).total_seconds()))


def continuous_seconds(exam) -> int:
    if not exam or not exam["started_at"]:
        return 0
    return max(0, int((utcnow() - parse_dt(exam["started_at"])).total_seconds()) - int(exam["break_seconds"] or 0))


@app.route("/exam/token", methods=["GET", "POST"])
@candidate_required
def exam_token():
    exam = current_exam()
    candidate = current_candidate()
    if not exam or not exam["agreement_accepted_at"] or not exam["orientation_complete"]:
        return redirect(url_for("agreement"))
    if not candidate.get("session_token") or exam.get("token_verified"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        entered = request.form.get("token", "").strip()
        if secrets.compare_digest(entered, str(candidate["session_token"])):
            with db() as connection:
                connection.execute("UPDATE exam_sessions SET token_verified=1 WHERE id=?", (exam["id"],))
            log_event("token_verified", "Session token accepted")
            return redirect(url_for("dashboard"))
        flash("Incorrect session token. Please contact your administrator.", "error")
    return render_template("token.html")


@app.post("/exam/start")
@exam_required
def start_exam():
    exam = current_exam()
    candidate = current_candidate()
    if candidate["session_token"] and not exam["token_verified"]:
        return redirect(url_for("exam_token"))
    if exam["status"] == "created":
        started = utcnow()
        fingerprint = request.form.get("device_fingerprint", "")[:256]
        device_hash = hashlib.sha256(fingerprint.encode()).hexdigest() if fingerprint else None
        with db() as connection:
            connection.execute(
                "UPDATE exam_sessions SET status='active',started_at=?,expires_at=?,device_hash=? WHERE id=?",
                (iso(started), iso(started + timedelta(hours=5)), device_hash, exam["id"]),
            )
        log_event("exam_started", "Five-hour session timer activated")
    return redirect(url_for("exam_question", qid=current_exam()["current_qid"]))


@app.route("/exam/question/<int:qid>", methods=["GET", "POST"])
@exam_required
def exam_question(qid: int):
    exam = current_exam()
    candidate = current_candidate()
    if candidate.get("session_token") and not exam.get("token_verified"):
        return redirect(url_for("exam_token"))
    questions = load_questions(candidate["plan"])
    if exam["status"] == "created":
        return redirect(url_for("dashboard"))
    if exam["status"] == "break":
        return redirect(url_for("dashboard"))
    if exam["status"] == "completed":
        return redirect(url_for("results"))
    if exam["status"] == "invalidated":
        flash("This session was invalidated and must restart from Question 1.", "error")
        return redirect(url_for("restart_exam"))
    if remaining_seconds(exam) <= 0:
        with db() as connection:
            connection.execute("UPDATE exam_sessions SET status='completed',completed_at=? WHERE id=?", (iso(), exam["id"]))
        log_event("session_timeout", "Five-hour limit reached")
        return redirect(url_for("results"))
    if qid != exam["current_qid"] or not 1 <= qid <= len(questions):
        flash("One-way navigation is active. Previous and future questions are locked.", "warning")
        return redirect(url_for("exam_question", qid=exam["current_qid"]))

    question = questions[qid - 1]
    with db() as connection:
        answer_row = connection.execute("SELECT * FROM answers WHERE session_id=? AND qid=?", (exam["id"], qid)).fetchone()
        answered_count = connection.execute("SELECT COUNT(*) count FROM answers WHERE session_id=?", (exam["id"],)).fetchone()["count"]
    learning_mode = setting("show_answers_immediately") == "1"

    if request.method == "POST":
        if answer_row:
            flash("That answer is already locked.", "warning")
            return redirect(url_for("exam_question", qid=qid))
        selected = request.form.getlist("answer")
        valid_letters = {option["letter"] for option in question["options"]}
        if not selected or not set(selected).issubset(valid_letters):
            flash("Select an answer before continuing.", "error")
        else:
            is_correct = answers_match(selected, question["answer"])
            with db() as connection:
                connection.execute(
                    "INSERT INTO answers(session_id,qid,answer_json,is_correct,answered_at) VALUES(?,?,?,?,?)",
                    (exam["id"], qid, json.dumps(selected), int(is_correct), iso()),
                )
            log_event("answer_locked", f"Question {qid} submitted")
            if learning_mode:
                return redirect(url_for("exam_question", qid=qid))
            return advance_exam(exam, qid, len(questions))

    selected = json.loads(answer_row["answer_json"]) if answer_row else []
    return render_template(
        "exam.html",
        question=question,
        qid=qid,
        total=len(questions),
        selected=selected,
        answer_row=answer_row,
        show_feedback=bool(answer_row and learning_mode),
        is_correct=bool(answer_row and answer_row["is_correct"]),
        remaining_seconds=remaining_seconds(exam),
        answered_count=answered_count,
        break_eligible=continuous_seconds(exam) >= 3 * 60 * 60,
    )


def advance_exam(exam, qid: int, total: int):
    if qid >= total:
        with db() as connection:
            result = connection.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(is_correct),0) correct FROM answers WHERE session_id=?", (exam["id"],)
            ).fetchone()
            score = round((result["correct"] / total) * 100, 1) if total else 0
            connection.execute(
                "UPDATE exam_sessions SET status='completed',completed_at=?,score=? WHERE id=?",
                (iso(), score, exam["id"]),
            )
        log_event("exam_completed", f"Score {score}%")
        return redirect(url_for("results"))
    with db() as connection:
        connection.execute("UPDATE exam_sessions SET current_qid=? WHERE id=?", (qid + 1, exam["id"]))
    return redirect(url_for("exam_question", qid=qid + 1))


@app.post("/exam/next")
@exam_required
def next_question():
    exam = current_exam()
    qid = exam["current_qid"]
    with db() as connection:
        answered = connection.execute("SELECT 1 FROM answers WHERE session_id=? AND qid=?", (exam["id"], qid)).fetchone()
    if not answered:
        abort(409)
    return advance_exam(exam, qid, len(load_questions(current_candidate()["plan"])))


@app.post("/exam/break")
@exam_required
def take_break():
    exam = current_exam()
    if exam["status"] != "active" or continuous_seconds(exam) < 3 * 60 * 60:
        flash("A break becomes available after three continuous hours.", "warning")
        return redirect(url_for("exam_question", qid=exam["current_qid"]))
    with db() as connection:
        connection.execute("UPDATE exam_sessions SET status='break',break_started_at=? WHERE id=?", (iso(), exam["id"]))
    log_event("break_started", "Eligible break started")
    return redirect(url_for("dashboard"))


@app.post("/exam/resume")
@exam_required
def resume_exam():
    exam = current_exam()
    if exam["status"] != "break":
        return redirect(url_for("dashboard"))
    elapsed = max(0, int((utcnow() - parse_dt(exam["break_started_at"])).total_seconds()))
    with db() as connection:
        connection.execute(
            "UPDATE exam_sessions SET status='active',break_seconds=break_seconds+?,break_started_at=NULL WHERE id=?",
            (elapsed, exam["id"]),
        )
    log_event("exam_resumed", f"Resumed after {elapsed} seconds")
    return redirect(url_for("exam_question", qid=exam["current_qid"]))


@app.post("/api/activity")
@candidate_required
def activity():
    data = request.get_json(silent=True) or {}
    allowed = {"tab_hidden", "fullscreen_exit", "copy_attempt", "context_menu", "browser_exit", "heartbeat"}
    event = data.get("event")
    if event not in allowed:
        abort(400)
    log_event(event, str(data.get("details", "")))
    if event == "browser_exit":
        exam = current_exam()
        if exam and exam["status"] == "active" and continuous_seconds(exam) < 3 * 60 * 60:
            with db() as connection:
                connection.execute("UPDATE exam_sessions SET status='invalidated',invalidated_at=? WHERE id=?", (iso(), exam["id"]))
    return ("", 204)


@app.route("/results")
@exam_required
def results():
    exam = current_exam()
    if exam["status"] != "completed":
        return redirect(url_for("dashboard"))
    questions = load_questions(current_candidate()["plan"])
    with db() as connection:
        stats = connection.execute(
            "SELECT COUNT(*) answered, COALESCE(SUM(is_correct),0) correct FROM answers WHERE session_id=?", (exam["id"],)
        ).fetchone()
    return render_template("results.html", total=len(questions), stats=stats)


@app.route("/review")
@exam_required
def review():
    exam = current_exam()
    if exam["status"] != "completed":
        abort(403)
    questions = load_questions(current_candidate()["plan"])
    with db() as connection:
        rows = connection.execute("SELECT * FROM answers WHERE session_id=? ORDER BY qid", (exam["id"],)).fetchall()
    answer_map = {row["qid"]: row for row in rows}
    items = []
    for question in questions:
        row = answer_map.get(question["id"])
        selected = json.loads(row["answer_json"]) if row else []
        option_map = {o["letter"]: o["text"] for o in question["options"]}
        items.append({
            "question": question,
            "selected": selected,
            "user_answer": "; ".join(option_map.get(x, x) for x in selected) or "No answer",
            "correct_answer": "; ".join(option_map.get(x, x) for x in question["answer"]),
            "is_correct": bool(row and row["is_correct"]),
        })
    return render_template("review.html", items=items)


@app.route("/restart", methods=["GET", "POST"])
@candidate_required
def restart_exam():
    candidate = current_candidate()
    session.pop("exam_session_id", None)
    create_exam_session(candidate["id"])
    flash("A new session has been created. Begin again with the agreement.", "info")
    return redirect(url_for("agreement"))


@app.route("/logout")
def logout():
    if session.get("candidate_id"):
        log_event("logout", "Candidate signed out")
    session.clear()
    return redirect(url_for("login"))


def admin_authorized() -> bool:
    return bool(session.get("admin_authenticated"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        expected = os.getenv("NCLEX_ADMIN_PASSWORD", "admin-nclex-2026")
        if secrets.compare_digest(request.form.get("password", ""), expected):
            session.clear()
            session["admin_authenticated"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect administrator password.", "error")
    return render_template("admin_login.html")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_authorized():
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as connection:
        candidates = connection.execute(
            """SELECT c.*, COUNT(DISTINCT s.id) sessions,
                      MAX(s.completed_at) last_completed, MAX(s.score) best_score
               FROM candidates c LEFT JOIN exam_sessions s ON s.candidate_id=c.id
               GROUP BY c.id ORDER BY c.created_at DESC"""
        ).fetchall()
        sessions = connection.execute(
            """SELECT s.*, c.full_name,c.registration_id,c.att_number,c.exam_type,
                      (SELECT COUNT(*) FROM answers a WHERE a.session_id=s.id) answered
               FROM exam_sessions s JOIN candidates c ON c.id=s.candidate_id
               ORDER BY s.created_at DESC LIMIT 100"""
        ).fetchall()
        activities = connection.execute(
            """SELECT a.*,c.full_name FROM activity_log a LEFT JOIN candidates c ON c.id=a.candidate_id
               ORDER BY a.created_at DESC LIMIT 50"""
        ).fetchall()
    completed = [s for s in sessions if s["status"] == "completed"]
    average = round(sum(s["score"] or 0 for s in completed) / len(completed), 1) if completed else 0
    return render_template(
        "admin.html",
        candidates=candidates,
        sessions=sessions,
        activities=activities,
        average=average,
        learning_mode=setting("show_answers_immediately") == "1",
    )


@app.post("/admin/candidate/<int:candidate_id>/token")
@admin_required
def admin_set_token(candidate_id):
    token = request.form.get("session_token", "").strip() or None
    with db() as connection:
        connection.execute("UPDATE candidates SET session_token=? WHERE id=?", (token, candidate_id))
    flash("Session token updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/settings")
@admin_required
def admin_settings():
    value = "1" if request.form.get("show_answers_immediately") == "on" else "0"
    with db() as connection:
        connection.execute(
            "INSERT INTO settings(key,value) VALUES('show_answers_immediately',?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (value,),
        )
    flash("Answer visibility setting updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export.csv")
@admin_required
def admin_export():
    with db() as connection:
        rows = connection.execute(
            """SELECT c.full_name,c.email,c.registration_id,c.att_number,c.exam_type,c.phone,
                      s.id session_id,s.status,s.started_at,s.completed_at,s.score
               FROM candidates c LEFT JOIN exam_sessions s ON s.candidate_id=c.id
               ORDER BY c.full_name,s.created_at"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["full_name", "email", "registration_id", "att_number", "exam_type", "phone", "session_id", "status", "started_at", "completed_at", "score"])
    writer.writerows([tuple(row.values()) for row in rows])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=nclex-results.csv"})


@app.route("/health")
def health():
    try:
        with db() as connection:
            connection.execute("SELECT 1")
        return jsonify(status="ok", database=True)
    except Exception:
        return jsonify(status="error", database=False), 500


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
