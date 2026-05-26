from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import json, random, os, datetime, io, sqlite3, hashlib, click
from functools import wraps

app = Flask(__name__)
app.secret_key = 'cdc_secret_key_2024'

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
Q_DIR    = os.path.join(BASE_DIR, 'questions')
DB_PATH  = os.path.join(BASE_DIR, 'cdc_assessment.db')   # SQLite file, auto-created

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # access columns by name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    """Create tables if they don't exist yet."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            roll_number TEXT NOT NULL UNIQUE,
            password    TEXT NOT NULL,
            email       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id      INTEGER NOT NULL,
            aptitude_score  INTEGER DEFAULT 0,
            reasoning_score INTEGER DEFAULT 0,
            english_score   INTEGER DEFAULT 0,
            essay_score     INTEGER DEFAULT 0,
            total_score     INTEGER DEFAULT 0,
            percentage      REAL    DEFAULT 0.0,
            essay_text      TEXT    DEFAULT '',
            feedback        TEXT    DEFAULT '',
            submitted_at    TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id)
        );
    """)
    conn.commit()
    conn.close()

# Auto-create DB on first run
init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_questions(fname, n=30):
    path = os.path.join(Q_DIR, fname)

    with open(path, encoding='utf-8') as f:
        qs = json.load(f)

    # ✅ FIX 1: dict → list
    if isinstance(qs, dict):
        qs = list(qs.values())

    # safety check
    if not isinstance(qs, list):
        raise ValueError(f"{fname} must contain a list or dict of questions")

    # optional shuffle safely
    random.shuffle(qs)

    return qs[:n]

def generate_feedback(apt, rea, eng, ess, pct):
    msgs = []

    if apt < 15:
        msgs.append("Practice more quantitative aptitude.")

    if rea < 15:
        msgs.append("Work on logical reasoning puzzles.")

    if eng < 15:
        msgs.append("Improve your English grammar and vocabulary.")

    if ess < 5:
        msgs.append("Write longer, structured essays.")

    if pct >= 80:
        msgs.insert(0, "Excellent! You are placement-ready.")

    elif pct >= 60:
        msgs.insert(0, "Good effort! Focus on weaker areas.")

    else:
        msgs.insert(0, "Keep practicing. Consistent effort pays off.")

    return " ".join(msgs)

def load_superusers():
    path = os.path.join(BASE_DIR, 'superusers.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_superusers(users):
    with open(os.path.join(BASE_DIR, 'superusers.json'), 'w') as f:
        json.dump(users, f, indent=2)

# ── Auth decorators ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'student_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('index.html')

# ── Register ──────────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    error = success = None
    if request.method == 'POST':
        name  = request.form['name'].strip()
        roll  = request.form['roll_number'].strip()
        email = request.form.get('email', '').strip()
        pwd   = request.form['password'].strip()
        cpwd  = request.form['confirm_password'].strip()

        if not name or not roll or not pwd:
            error = 'Name, Roll Number and Password are required.'
        elif pwd != cpwd:
            error = 'Passwords do not match.'
        elif len(pwd) < 6:
            error = 'Password must be at least 6 characters.'
        else:
            try:
                db  = get_db(); cur = db.cursor()
                cur.execute("SELECT id FROM students WHERE roll_number=?", (roll,))
                if cur.fetchone():
                    error = f'Roll number {roll} is already registered. Please login.'
                else:
                    cur.execute(
                        "INSERT INTO students (name, roll_number, password, email) VALUES (?,?,?,?)",
                        (name, roll, pwd, email)
                    )
                    db.commit()
                    success = f'Account created for {name}! You can now login.'
                db.close()
            except Exception as e:
                error = f'Error: {e}'
    return render_template('register.html', error=error, success=success)

# ── Login ─────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        roll = request.form['roll_number'].strip()
        pwd  = request.form['password'].strip()
        try:
            db  = get_db(); cur = db.cursor()
            cur.execute("SELECT * FROM students WHERE roll_number=? AND password=?", (roll, pwd))
            student = cur.fetchone()
            db.close()
            if student:
                session['student_id']   = student['id']
                session['student_name'] = student['name']
                session['roll_number']  = student['roll_number']
                return redirect(url_for('exam'))
            error = 'Invalid roll number or password.'
        except Exception as e:
            error = f'Error: {e}'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template(
        'dashboard.html',
        student_name=session.get('student_name')
    )

# ── Exam ──────────────────────────────────────────────────────────────────────
@app.route('/exam')
@login_required
def exam():
    aptitude  = load_questions('aptitude.json')
    reasoning = load_questions('reasoning.json')
    english   = load_questions('english.json')
    with open(os.path.join(Q_DIR, 'essay_topics.json'), encoding='utf-8') as f:
        essay_topic = random.choice(json.load(f))['topic']
    return render_template('exam.html',
        student_name=session['student_name'],
        aptitude=aptitude, reasoning=reasoning,
        english=english, essay_topic=essay_topic
    )

# ── Submit ────────────────────────────────────────────────────────────────────
@app.route('/submit', methods=['POST'])
@login_required
def submit():
    data = request.get_json()

    def score_section(student_answers, fname):
        try:
            with open(os.path.join(Q_DIR, fname), encoding='utf-8') as f:
                questions = json.load(f)

            # Convert dict to list if needed
            if isinstance(questions, dict):
                questions = list(questions.values())

            # Answer key
            key = {}

            for q in questions:
                question_text = q.get('question', '').strip()
                answer = q.get('answer', '').strip()

                if question_text:
                    key[question_text] = answer

            score = 0

            for question, student_answer in student_answers.items():

                question = question.strip()
                student_answer = str(student_answer).strip()

                correct_answer = str(key.get(question, '')).strip()

                if student_answer == correct_answer:
                    score += 1

            return score

        except Exception as e:
            print(f"Error scoring {fname}: {e}")
            return 0

    # Raw scores
    apt_raw = score_section(data.get('aptitude', {}), 'aptitude.json')
    rea_raw = score_section(data.get('reasoning', {}), 'reasoning.json')
    eng_raw = score_section(data.get('english', {}), 'english.json')

    # Convert each section to 30 marks
    # assuming each section contains 30 questions

    apt = round((apt_raw / 30) * 30)
    rea = round((rea_raw / 30) * 30)
    eng = round((eng_raw / 30) * 30)

    # Essay section (10 marks)
    txt = data.get('essay', '').strip()

    word_count = len(txt.split())

    ess = min(10, word_count // 30)

    # Total marks out of 100
    tot = apt + rea + eng + ess

    # Percentage
    pct = round((tot / 100) * 100, 2)

    # Feedback
    fb = generate_feedback(apt, rea, eng, ess, pct)

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        db = get_db()
        cur = db.cursor()

        cur.execute("""
            INSERT INTO results (
                student_id,
                aptitude_score,
                reasoning_score,
                english_score,
                essay_score,
                total_score,
                percentage,
                essay_text,
                feedback,
                submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session['student_id'],
            apt,
            rea,
            eng,
            ess,
            tot,
            pct,
            txt,
            fb,
            now
        ))

        db.commit()
        db.close()

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

    session['last_result'] = {
        'aptitude_score': apt,
        'reasoning_score': rea,
        'english_score': eng,
        'essay_score': ess,
        'total_score': tot,
        'percentage': pct,
        'feedback': fb,
        'name': session['student_name'],
        'roll_number': session['roll_number'],
        'submitted_at': now
    }

    return jsonify({
        'status': 'ok',
        'redirect': url_for('result')
    })
# ── Result ────────────────────────────────────────────────────────────────────
@app.route('/result')
@login_required
def result():
    data = None
    try:
        db  = get_db(); cur = db.cursor()
        cur.execute("""
            SELECT r.*, s.name, s.roll_number FROM results r
            JOIN students s ON r.student_id = s.id
            WHERE r.student_id=? ORDER BY r.submitted_at DESC LIMIT 1
        """, (session['student_id'],))
        row = cur.fetchone()
        data = dict(row) if row else None
        db.close()
    except Exception:
        pass
    if not data:
        data = session.get('last_result', {})
    return render_template('result.html', data=data)

# ── Admin Login ───────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        uname  = request.form['username'].strip()
        pwd    = request.form['password'].strip()
        hashed = hashlib.sha256(pwd.encode()).hexdigest()
        users  = load_superusers()

        if uname in users and users[uname]['password_hash'] == hashed:
            session['is_admin'] = True
            session['admin_name'] = uname
            return redirect(url_for('admin_panel'))
        elif uname == 'admin' and pwd == 'admin123':   # default fallback
            session['is_admin'] = True
            session['admin_name'] = 'admin'
            return redirect(url_for('admin_panel'))
        else:
            error = 'Invalid credentials.'
    return render_template('admin_login.html', error=error)

# ── Admin Panel ───────────────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_panel():
    results = []; students_count = 0; db_error = None
    try:
        db  = get_db(); cur = db.cursor()
        cur.execute("""
            SELECT r.id, s.name, s.roll_number, r.aptitude_score, r.reasoning_score,
                   r.english_score, r.essay_score, r.total_score, r.percentage, r.submitted_at
            FROM results r JOIN students s ON r.student_id = s.id
            ORDER BY r.submitted_at DESC
        """)
        results = [dict(r) for r in cur.fetchall()]
        students_count = cur.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        db.close()
    except Exception as e:
        db_error = str(e)
    return render_template('admin.html', results=results,
                           students_count=students_count, db_error=db_error)

@app.route('/admin/add_student', methods=['POST'])
@admin_required
def add_student():
    try:
        db = get_db()
        db.execute("INSERT INTO students (name, roll_number, password, email) VALUES (?,?,?,?)",
            (request.form['name'].strip(), request.form['roll_number'].strip(),
             request.form['password'].strip(), request.form.get('email','').strip()))
        db.commit(); db.close()
    except Exception:
        pass
    return redirect(url_for('admin_panel'))

@app.route('/admin/export')
@admin_required
def export_results():
    import pandas as pd
    db  = get_db(); cur = db.cursor()
    cur.execute("""
        SELECT s.name, s.roll_number, r.aptitude_score, r.reasoning_score,
               r.english_score, r.essay_score, r.total_score, r.percentage, r.submitted_at
        FROM results r JOIN students s ON r.student_id = s.id
    """)
    rows = [dict(r) for r in cur.fetchall()]; db.close()
    buf  = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    return send_file(buf, download_name='cdc_results.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('home'))

# ── Flask CLI Commands ────────────────────────────────────────────────────────

@app.cli.command('createsuperuser')
def createsuperuser():
    """Create an admin superuser interactively."""
    click.echo(click.style('\n  CDC — Create Superuser', fg='yellow', bold=True))
    click.echo('  ' + '─' * 36)
    username = click.prompt('  Username', default='admin')
    while True:
        password = click.prompt('  Password', hide_input=True)
        if len(password) < 6:
            click.echo(click.style('  ✗ Min 6 characters.', fg='red')); continue
        if click.prompt('  Confirm Password', hide_input=True) != password:
            click.echo(click.style('  ✗ Passwords do not match.', fg='red'))
        else:
            break
    users = load_superusers()
    if username in users:
        if not click.confirm(f'  "{username}" exists. Overwrite?', default=False):
            click.echo('  Cancelled.'); return
    users[username] = {
        'password_hash': hashlib.sha256(password.encode()).hexdigest(),
        'created_at': str(datetime.datetime.now())
    }
    save_superusers(users)
    click.echo(click.style(f'\n  ✓ Superuser "{username}" created!', fg='green', bold=True))
    click.echo(click.style('  Login at: http://localhost:5000/admin/login\n', fg='cyan'))

@app.cli.command('listsuperusers')
def listsuperusers():
    """List all superusers."""
    users = load_superusers()
    if not users:
        click.echo(click.style('  No superusers. Run: flask createsuperuser', fg='yellow')); return
    click.echo(click.style(f'\n  Superusers ({len(users)}):', fg='yellow', bold=True))
    for u, info in users.items():
        click.echo(f'  • {u}  —  {info["created_at"][:19]}')
    click.echo()

@app.cli.command('deletesuperuser')
@click.argument('username')
def deletesuperuser(username):
    """Delete a superuser. Usage: flask deletesuperuser <username>"""
    users = load_superusers()
    if username not in users:
        click.echo(click.style(f'  "{username}" not found.', fg='red')); return
    del users[username]; save_superusers(users)
    click.echo(click.style(f'  ✓ "{username}" deleted.', fg='green'))

@app.cli.command('createdb')
def createdb():
    """Recreate all database tables."""
    init_db()
    click.echo(click.style(f'  ✓ Database ready: {DB_PATH}', fg='green', bold=True))

# ─────────────────────────────────────────────────────────────────────────────
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
