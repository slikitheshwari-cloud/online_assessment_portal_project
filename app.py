from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import json, random, os, datetime, io, sqlite3, hashlib, click, re
from functools import wraps

app = Flask(__name__)
app.secret_key = 'cdc_secret_key_2024'

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
Q_DIR    = os.path.join(BASE_DIR, 'questions')
DB_PATH  = os.path.join(BASE_DIR, 'cdc_assessment.db')   # SQLite file, auto-created
QUESTION_BANKS = {'aptitude': 'aptitude.json', 'reasoning': 'reasoning.json', 'english': 'english.json', 'essay': 'essay_topics.json'}
EXAM_SELECTION_PATH = os.path.join(BASE_DIR, 'exam_selection.json')

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
def read_question_bank(section):
    filename = QUESTION_BANKS.get(section)
    if not filename:
        raise ValueError('Invalid question section.')
    with open(os.path.join(Q_DIR, filename), encoding='utf-8') as question_file:
        questions = json.load(question_file)
    return list(questions.values()) if isinstance(questions, dict) else questions

def save_question_bank(section, questions):
    path = os.path.join(Q_DIR, QUESTION_BANKS[section])
    with open(path, 'w', encoding='utf-8') as question_file:
        json.dump(questions, question_file, indent=2, ensure_ascii=False)
        question_file.write('\n')

def timestamp_now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def question_added_at(question):
    return question.get('added_at') or 'Added before date tracking'

def question_fingerprint(question):
    """Return a normalized question/topic value used to prevent duplicates."""
    text = question.get('topic') if 'topic' in question else question.get('question', '')
    return ' '.join(str(text).casefold().split())

def load_exam_selection():
    """Load the administrator's published exam questions."""
    if not os.path.exists(EXAM_SELECTION_PATH):
        return None
    with open(EXAM_SELECTION_PATH, encoding='utf-8') as selection_file:
        selection = json.load(selection_file)
    return selection if isinstance(selection, dict) else None

def save_exam_selection(selection):
    with open(EXAM_SELECTION_PATH, 'w', encoding='utf-8') as selection_file:
        json.dump(selection, selection_file, indent=2)
        selection_file.write('\n')

def question_upload_date(question):
    """Return the date a question was added, or None for legacy questions."""
    raw_date = str(question.get('added_at', '')).strip()[:10]
    try:
        return datetime.datetime.strptime(raw_date, '%Y-%m-%d').date()
    except ValueError:
        return None

def questions_uploaded_on_or_after(section, date_from):
    """Return questions in a section that were uploaded on or after date_from."""
    return [
        question for question in read_question_bank(section)
        if (question_upload_date(question) or datetime.date.min) >= date_from
    ]

def selected_questions_for_exam(section):
    selection = load_exam_selection()
    if selection is None:
        return read_question_bank(section)
    raw_date = str(selection.get('question_date_from', '')).strip()
    try:
        date_from = datetime.datetime.strptime(raw_date, '%Y-%m-%d').date()
    except ValueError:
        return read_question_bank(section)
    return questions_uploaded_on_or_after(section, date_from)

def shuffled_exam_questions(section):
    """Return the 30 published MCQs used for one candidate's section."""
    questions = selected_questions_for_exam(section)
    random.shuffle(questions)
    # Every multiple-choice section is intentionally capped at 30 questions.
    # Essay is handled separately and is not rendered as a question list.
    return questions[:30]
def detect_question_section(question_text, options=None):
    """Identify clear aptitude, reasoning, or English questions from their wording.

    An empty result means the question is too general to classify safely, so it is
    not rejected solely on this check.
    """
    option_text = ' '.join((options or {}).values()) if isinstance(options, dict) else ''
    text = f'{question_text} {option_text}'.casefold()
    english_terms = ('synonym', 'antonym', 'grammar', 'adjective', 'adverb', 'noun', 'verb', 'pronoun',
                     'preposition', 'conjunction', 'article', 'tense', 'plural', 'spelling', 'vocabulary',
                     'meaning of the word', 'one word substitution', 'correctly spelt', 'fill in the blank')
    reasoning_terms = ('coding', 'decoding', 'syllogism', 'blood relation', 'direction', 'seating arrangement',
                       'statement and conclusion', 'statement and assumption', 'calendar', 'clock', 'analogy',
                       'classification', 'logical sequence', 'letter series', 'number series', 'figure',
                       'puzzle', 'rank', 'order of persons')
    aptitude_terms = ('percentage', 'profit', 'loss', 'discount', 'simple interest', 'compound interest', 'average',
                      'ratio', 'proportion', 'speed', 'distance', 'time', 'train', 'work', 'pipe', 'mixture',
                      'probability', 'permutation', 'combination', 'area', 'volume', 'mean', 'median', 'age',
                      'investment', 'salary', 'rupees', '₹', 'km', 'kilometre', 'litre')
    if any(term in text for term in english_terms):
        return 'english'
    if any(term in text for term in reasoning_terms):
        return 'reasoning'
    if any(term in text for term in aptitude_terms) or (re.search(r'\d', text) and any(symbol in text for symbol in ('%', '+', '-', '×', '÷', '/', '='))):
        return 'aptitude'
    return None

def validate_question_section(section, question_text, options=None):
    detected_section = detect_question_section(question_text, options)
    if detected_section and detected_section != section:
        article = 'an' if detected_section in ('aptitude', 'english') else 'a'
        raise ValueError(
            f'This looks like {article} {detected_section.title()} question, not {section.title()}. '
            f'Please select the {detected_section.title()} section before adding it.'
        )

def validate_imported_question(question, section):
    if not isinstance(question, dict):
        raise ValueError('Every imported item must be a JSON object.')
    declared_section = str(question.get('section', '')).strip().casefold()
    if declared_section not in QUESTION_BANKS:
        raise ValueError('Each imported item must include a valid section: aptitude, reasoning, english, or essay.')
    if declared_section != section:
        raise ValueError(f'These questions belong to {declared_section.title()}, but you selected {section.title()}. Choose the correct section before importing.')
    if section == 'essay':
        topic = str(question.get('topic', '')).strip()
        if not topic:
            raise ValueError('Each essay item needs a topic.')
        return {'topic': topic, 'added_at': timestamp_now()}
    question_text = str(question.get('question', '')).strip()
    raw_options = question.get('options')
    options = {letter: str(raw_options.get(letter, '')).strip() for letter in 'ABCD'} if isinstance(raw_options, dict) else {}
    answer = str(question.get('answer', '')).strip().upper()
    if not question_text or not all(options.values()) or answer not in options:
        raise ValueError('Each MCQ needs question, options A-D, and answer A-D.')
    validate_question_section(section, question_text, options)
    return {'question': question_text, 'options': options, 'answer': answer, 'added_at': timestamp_now()}

def next_question_id(questions):
    """Return the next numeric ID without failing on legacy data."""
    question_ids = []
    for question in questions:
        try:
            question_ids.append(int(question.get('id', 0)))
        except (TypeError, ValueError):
            continue
    return max(question_ids, default=0) + 1

def remember_question_addition(section, question_ids):
    """Store recent additions in the current admin session for undo."""
    additions = session.get('question_additions', [])
    additions.append({'section': section, 'ids': question_ids})
    session['question_additions'] = additions[-10:]

def remember_question_removal(section, questions):
    """Store one removal action, including every question removed by that action."""
    if isinstance(questions, dict):
        questions = [questions]
    removals = session.get('question_removals', [])
    removals.append({'section': section, 'questions': questions})
    session['question_removals'] = removals[-10:]
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
                return redirect(url_for('dashboard'))
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
    db = get_db()
    results = db.execute("""
        SELECT aptitude_score, reasoning_score, english_score, essay_score,
               total_score, percentage, submitted_at
        FROM results WHERE student_id=?
        ORDER BY submitted_at DESC, id DESC
    """, (session['student_id'],)).fetchall()
    db.close()

    comparison = None
    if len(results) >= 2:
        latest, previous = results[0], results[1]
        sections = (('aptitude', 'Aptitude', 30), ('reasoning', 'Reasoning', 30),
                    ('english', 'English', 30), ('essay', 'Essay', 10))
        comparison = {
            'overall_change': round(latest['percentage'] - previous['percentage'], 2),
            'previous_percentage': previous['percentage'],
            'sections': [
                {'name': label, 'latest': latest[f'{key}_score'],
                 'previous': previous[f'{key}_score'],
                 'change': latest[f'{key}_score'] - previous[f'{key}_score'],
                 'maximum': maximum}
                for key, label, maximum in sections
            ],
        }
    return render_template(
        'dashboard.html',
        student_name=session.get('student_name'),
        roll_number=session.get('roll_number'),
        results=results,
        comparison=comparison,
    )

# ── Exam ──────────────────────────────────────────────────────────────────────
@app.route('/exam')
@login_required
def exam():
    aptitude = shuffled_exam_questions('aptitude')
    reasoning = shuffled_exam_questions('reasoning')
    english = shuffled_exam_questions('english')
    essay_topics = shuffled_exam_questions('essay')
    essay_topic = random.choice(essay_topics)['topic'] if essay_topics else None
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

    def score_section(student_answers, section):
        try:
            questions = selected_questions_for_exam(section)

            key = {}

            for q in questions:
                question_id = str(q.get('id', '')).strip()
                answer_key = str(q.get('answer', '')).strip()
                options = q.get('options', {})
                answer_text = str(options.get(answer_key, '')).strip()

                if question_id:
                    key[question_id] = {
                        'answer_key': answer_key.lower(),
                        'answer_text': answer_text.lower()
                    }

            score = 0

            for question_id, student_answer in student_answers.items():
                question_id = str(question_id).strip()
                student_answer = str(student_answer).strip().lower()

                correct = key.get(question_id)

                if correct and student_answer in (correct['answer_key'], correct['answer_text']):
                    score += 1

            return score

        except Exception as e:
            print(f"Error scoring {section}: {e}")
            return 0
    # Raw scores
    apt_raw = score_section(data.get('aptitude', {}), 'aptitude')
    rea_raw = score_section(data.get('reasoning', {}), 'reasoning')
    eng_raw = score_section(data.get('english', {}), 'english')

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
    question_banks = {section: read_question_bank(section) for section in QUESTION_BANKS}
    question_counts = {section: len(questions) for section, questions in question_banks.items()}
    removals = session.get('question_removals', [])
    latest_removal = removals[-1] if removals else None
    return render_template('admin.html', results=results, students_count=students_count, db_error=db_error,
                           question_banks=question_banks, question_counts=question_counts,
                           total_questions=sum(question_counts.values()), exam_selection=load_exam_selection(),
                           question_added_at=question_added_at, latest_removal=latest_removal,
                           message=request.args.get('message'), error=request.args.get('error'))

@app.route('/admin/questions/publish-exam', methods=['POST'])
@admin_required
def publish_exam_selection():
    question_date_from = request.form.get('question_date_from', '').strip()
    try:
        date_from = datetime.datetime.strptime(question_date_from, '%Y-%m-%d').date()
    except ValueError:
        return redirect(url_for('admin_panel', error='Enter a valid question upload date.'))
    minimums = {'aptitude': 30, 'reasoning': 30, 'english': 30, 'essay': 1}
    shortages = []
    for section, minimum in minimums.items():
        available = len(questions_uploaded_on_or_after(section, date_from))
        if available < minimum:
            label = 'Essay topic' if section == 'essay' else section.title()
            shortages.append(f'{label}: {available}/{minimum}')
    if shortages:
        return redirect(url_for(
            'admin_panel',
            error='Inadequate questions for this date. Select another date or upload more questions. ' +
                  ' | '.join(shortages)
        ))
    save_exam_selection({
        'question_date_from': date_from.isoformat(),
        'published_at': timestamp_now()
    })
    return redirect(url_for('admin_panel', message=f'Published all questions uploaded on or after {date_from.strftime("%d %b %Y")}.'))
@app.route('/admin/questions/import', methods=['POST'])
@admin_required
def import_questions():
    section = request.form.get('section', '')
    uploaded_file = request.files.get('questions_file')
    try:
        if not uploaded_file or not uploaded_file.filename or not uploaded_file.filename.lower().endswith('.json'):
            raise ValueError('Choose a JSON file to import.')
        imported_questions = json.load(uploaded_file)
        if not isinstance(imported_questions, list):
            raise ValueError('The JSON file must contain an array of questions.')
        questions = read_question_bank(section)
        valid_questions = []
        validation_errors = []
        existing_fingerprints = {question_fingerprint(question) for question in questions}
        imported_fingerprints = set()
        duplicates_rejected = 0
        for item_number, question in enumerate(imported_questions, start=1):
            try:
                validated_question = validate_imported_question(question, section)
                fingerprint = question_fingerprint(validated_question)
                if fingerprint in existing_fingerprints or fingerprint in imported_fingerprints:
                    duplicates_rejected += 1
                    continue
                imported_fingerprints.add(fingerprint)
                valid_questions.append(validated_question)
            except ValueError as error:
                if isinstance(question, dict):
                    question_preview = str(question.get('question') or question.get('topic') or 'No question text').strip()
                else:
                    question_preview = 'Invalid item'
                question_preview = question_preview[:140] + ('...' if len(question_preview) > 140 else '')
                validation_errors.append(f'Item {item_number} ("{question_preview}"): {error}')
        if validation_errors:
            duplicate_note = f' {duplicates_rejected} duplicate question(s) were also rejected.' if duplicates_rejected else ''
            raise ValueError('No questions were imported. Please correct these item(s): ' + ' | '.join(validation_errors) + duplicate_note)
        if not valid_questions:
            raise ValueError(f'No questions were imported. {duplicates_rejected} question(s) were rejected because they are duplicates.')
        next_id = next_question_id(questions)
        added_ids = []
        for question in valid_questions:
            question['id'] = next_id
            added_ids.append(next_id)
            next_id += 1
            questions.append(question)
        save_question_bank(section, questions)
        remember_question_addition(section, added_ids)
        duplicate_message = f' {duplicates_rejected} duplicate question(s) were rejected.' if duplicates_rejected else ''
        return redirect(url_for('admin_panel', message=f'{len(valid_questions)} questions imported into {section.title()}.{duplicate_message}'))
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return redirect(url_for('admin_panel', error=f'Import failed: {error}'))

@app.route('/admin/questions/add', methods=['POST'])
@admin_required
def add_question():
    section = request.form.get('section', '').strip().lower()
    try:
        if section not in QUESTION_BANKS:
            raise ValueError('Choose a valid question section.')
        if section == 'essay':
            question = {'topic': request.form.get('topic', '').strip(), 'added_at': timestamp_now()}
            if not question['topic']:
                raise ValueError('Enter an essay topic.')
        else:
            options = {letter: request.form.get(f'option_{letter.lower()}', '').strip() for letter in 'ABCD'}
            question = {
                'question': request.form.get('question', '').strip(),
                'options': options,
                'answer': request.form.get('answer', '').strip().upper(),
                'added_at': timestamp_now()
            }
            if not question['question'] or not all(options.values()) or question['answer'] not in options:
                raise ValueError('Enter a question, all four options, and a valid correct answer.')
            validate_question_section(section, question['question'], options)
        questions = read_question_bank(section)
        if question_fingerprint(question) in {question_fingerprint(item) for item in questions}:
            raise ValueError('This question already exists in the selected section and was not added.')
        question['id'] = next_question_id(questions)
        questions.append(question)
        save_question_bank(section, questions)
        remember_question_addition(section, [question['id']])
        return redirect(url_for('admin_panel', message='Question added. You can undo it below if needed.'))
    except (OSError, ValueError) as error:
        return redirect(url_for('admin_panel', error=f'Question was not added: {error}'))

@app.route('/admin/questions/<section>/<int:question_id>/delete', methods=['POST'])
@admin_required
def delete_question(section, question_id):
    try:
        questions = read_question_bank(section)
        removed_question = next((question for question in questions if str(question.get('id')) == str(question_id)), None)
        updated_questions = [question for question in questions if str(question.get('id')) != str(question_id)]
        if len(updated_questions) == len(questions):
            raise ValueError('Question not found.')
        save_question_bank(section, updated_questions)
        remember_question_removal(section, removed_question)
        return redirect(url_for('admin_panel', message='Question removed. You can restore the latest removed question below if needed.'))
    except (OSError, ValueError) as error:
        return redirect(url_for('admin_panel', error=f'Question was not removed: {error}'))

@app.route('/admin/questions/undo-removal', methods=['POST'])
@admin_required
def undo_last_question_removal():
    removals = session.get('question_removals', [])
    if not removals:
        return redirect(url_for('admin_panel', error='There are no recently removed questions to restore in this session.'))
    last_removal = removals[-1]
    section = last_removal['section']
    available_questions = last_removal.get('questions', [last_removal.get('question')])
    requested_ids = {str(question_id) for question_id in request.form.getlist('question_ids')}
    restore_all = request.form.get('restore_all') == 'true'
    questions_to_restore = available_questions if restore_all else [
        question for question in available_questions if str(question.get('id')) in requested_ids
    ]
    if not questions_to_restore:
        return redirect(url_for('admin_panel', error='Select at least one removed question to restore.'))
    try:
        questions = read_question_bank(section)
        existing_ids = {str(item.get('id')) for item in questions}
        duplicates = [question for question in questions_to_restore if str(question.get('id')) in existing_ids]
        if duplicates:
            raise ValueError('One or more of these questions are already in the question bank.')
        questions.extend(questions_to_restore)
        save_question_bank(section, questions)
        restored_ids = {str(question.get('id')) for question in questions_to_restore}
        remaining_questions = [question for question in available_questions if str(question.get('id')) not in restored_ids]
        if remaining_questions:
            removals[-1] = {'section': section, 'questions': remaining_questions}
        else:
            removals.pop()
        session['question_removals'] = removals
        count = len(questions_to_restore)
        remaining_note = f' {len(remaining_questions)} item(s) remain available to restore.' if remaining_questions else ''
        return redirect(url_for('admin_panel', message=f'Restored {count} question(s).{remaining_note}'))
    except (OSError, ValueError) as error:
        return redirect(url_for('admin_panel', error=f'Restore failed: {error}'))

@app.route('/admin/questions/undo-last', methods=['POST'])
@admin_required
def undo_last_question_addition():
    additions = session.get('question_additions', [])
    if not additions:
        return redirect(url_for('admin_panel', error='There are no recently added questions to undo in this session.'))
    last_addition = additions.pop()
    section, question_ids = last_addition['section'], {str(question_id) for question_id in last_addition['ids']}
    try:
        questions = read_question_bank(section)
        removed_questions = [question for question in questions if str(question.get('id')) in question_ids]
        updated_questions = [question for question in questions if str(question.get('id')) not in question_ids]
        removed_count = len(removed_questions)
        save_question_bank(section, updated_questions)
        if removed_questions:
            remember_question_removal(section, removed_questions)
        session['question_additions'] = additions
        suffix = '' if removed_count == 1 else 's'
        return redirect(url_for('admin_panel', message=f'Undid the last addition and removed {removed_count} question{suffix}. You can restore the latest removed question if needed.'))
    except (OSError, ValueError) as error:
        return redirect(url_for('admin_panel', error=f'Undo failed: {error}'))
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

@app.route('/stats')
def stats():

    conn = sqlite3.connect('cdc_assessment.db')
    cursor = conn.cursor()

    # total tests
    cursor.execute("SELECT COUNT(*) FROM results")
    total_tests = cursor.fetchone()[0]

    # tests per day
    cursor.execute("""
        SELECT DATE(submitted_at), COUNT(*)
        FROM results
        GROUP BY DATE(submitted_at)
        ORDER BY DATE(submitted_at)
    """)

    rows = cursor.fetchall()

    labels = [row[0] for row in rows]
    counts = [row[1] for row in rows]

    conn.close()

    return jsonify({
        "total_tests": total_tests,
        "labels": labels,
        "counts": counts
    })
# ─────────────────────────────────────────────────────────────────────────────
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2000))
    app.run(host="0.0.0.0", port=port, debug=False)
