import os
import math
import pandas as pd
import pg8000.native
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'supersecretkey')

 
def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ==========================================
# AUTH ROUTES
# ==========================================

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('student_dashboard' if session.get('role') == 'student' else 'staff_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_type = request.form.get('login_type')
        identifier = request.form.get('identifier')
        password = request.form.get('password')
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            if login_type == 'student':
                roll_no_upper = identifier.upper()
                if password != roll_no_upper.lower():
                    flash('Invalid password. Must be your lowercase Roll Number.', 'error')
                    return redirect(url_for('login'))
                cur.execute('SELECT * FROM students WHERE roll_number = %s', (roll_no_upper,))
                student = cur.fetchone()
                if not student:
                    flash('Student not found.', 'error')
                    return redirect(url_for('login'))
                session.update({'user_id': student['id'], 'role': 'student', 'name': student['name']})
                return redirect(url_for('student_dashboard'))
            elif login_type == 'staff':
                cur.execute('SELECT * FROM staff WHERE email = %s', (identifier,))
                staff = cur.fetchone()
                if staff and check_password_hash(staff['password'], password):
                    session.update({
                        'user_id': staff['id'], 'role': 'staff', 'name': staff['name'],
                        'is_mentor': staff['is_mentor'], 'is_hod': staff['is_hod'],
                        'is_cp': staff['is_chairperson'] or staff['is_cochairperson'],
                        'year': staff['batch_year'], 'section': staff['section']
                    })
                    return redirect(url_for('staff_dashboard'))
                flash('Invalid credentials', 'error')
        finally:
            cur.close()
            conn.close()
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name, email = request.form.get('name'), request.form.get('email')
        password = generate_password_hash(request.form.get('password'))
        is_mentor = request.form.get('is_mentor') == 'on'
        is_cp, is_cocp = request.form.get('is_chairperson') == 'on', request.form.get('is_cochairperson') == 'on'
        is_hod = request.form.get('is_hod') == 'on'
        batch_year = request.form.get('batch_year')
        section = request.form.get('section') or None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('''
                INSERT INTO staff (name, email, password, is_mentor, is_chairperson, is_cochairperson, is_hod, batch_year, section) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            ''', (name, email, password, is_mentor, is_cp, is_cocp, is_hod, batch_year, section))
            new_id = cur.fetchone()[0]
            clean_name = name.replace('.', ' ')
            search_name = f"%{max(clean_name.split(), key=len)}%"
            cur.execute("UPDATE students SET mentor_id = %s WHERE excel_mentor_name ILIKE %s AND mentor_id IS NULL", (new_id, search_name))
            conn.commit()
            flash('Registered successfully!', 'success')
            return redirect(url_for('login'))
        except: 
            flash('Registration failed. Email might already exist.', 'error')
        finally: 
            cur.close(); conn.close()
    return render_template('register.html')

# ==========================================
# STAFF ROUTES
# ==========================================

@app.route('/staff/dashboard')
def staff_dashboard():
    if 'user_id' not in session or session.get('role') != 'staff': return redirect(url_for('login'))
    user_id = session['user_id']
    year, section = session.get('year'), session.get('section')
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        cur.execute("SELECT * FROM students WHERE mentor_id = %s OR co_mentor_id = %s ORDER BY roll_number", (user_id, user_id))
        mentee_list = cur.fetchall()

        query_base = """
            SELECT lr.id, lr.student_id, lr.leave_type, lr.reason, lr.status, lr.is_emergency,
            TO_CHAR(lr.from_date, 'YYYY-MM-DD') as from_date_raw,
            TO_CHAR(lr.from_date, 'DD Mon YYYY') as from_date,
            TO_CHAR(lr.to_date, 'DD Mon YYYY') as to_date,
            (lr.to_date - lr.from_date + 1) as duration,
            s.name AS student_name, s.roll_number, s.batch_year, s.section 
        """

        if session.get('is_hod'):
            cur.execute(query_base + """, 
                COALESCE((SELECT SUM(to_date-from_date+1) FROM leave_requests WHERE student_id=s.id AND leave_type='leave' AND status='approved_final' AND from_date>=date_trunc('month', CURRENT_DATE)),0) as month_leaves,
                COALESCE((SELECT SUM(to_date-from_date+1) FROM leave_requests WHERE student_id=s.id AND leave_type='od' AND status='approved_final' AND from_date>=date_trunc('month', CURRENT_DATE)),0) as month_ods
                FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE lr.status = 'pending_hod' ORDER BY lr.created_at ASC""")
        elif session.get('is_cp'):
            cur.execute(query_base + """ FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE (s.batch_year = %s AND UPPER(TRIM(s.section)) = UPPER(TRIM(%s)) AND lr.status = 'pending_cp')
                   OR ((s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor')
                ORDER BY lr.created_at ASC""", (year, section, user_id, user_id))
        else:
            cur.execute(query_base + """ FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE (s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor'
                ORDER BY lr.created_at ASC""", (user_id, user_id))
        
        pending_requests = cur.fetchall()
        
        # Extra lists for CP
        class_list, all_mentors = [], []
        if session.get('is_cp'):
            # 🌟 FIX: Added TRIM() to both student section and session section
            cur.execute("""
                SELECT s.*, st.name as system_mentor_name, st2.name as system_co_mentor_name 
                FROM students s 
                LEFT JOIN staff st ON s.mentor_id=st.id 
                LEFT JOIN staff st2 ON s.co_mentor_id=st2.id 
                WHERE s.batch_year=%s 
                  AND TRIM(UPPER(s.section)) = TRIM(UPPER(%s)) 
                ORDER BY s.roll_number
            """, (year, section))
            class_list = cur.fetchall()
            
            cur.execute("SELECT id, name FROM staff WHERE is_mentor=TRUE ORDER BY name")
            all_mentors = cur.fetchall()

    finally: 
        cur.close(); conn.close()
    return render_template('staff.html', pending_requests=pending_requests, mentee_list=mentee_list, class_list=class_list, all_mentors=all_mentors)



@app.route('/staff/student-details/<int:student_id>', methods=['GET'])
def get_student_details(student_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id, name, roll_number, batch_year, section FROM students WHERE id = %s", (student_id,))
        student_info = cur.fetchone()
        cur.execute("""
            SELECT id, leave_type, 
            TO_CHAR(from_date, 'DD Mon YYYY') as from_date_clean, 
            TO_CHAR(to_date, 'DD Mon YYYY') as to_date_clean, 
            (to_date - from_date + 1) as duration,
            reason, status FROM leave_requests 
            WHERE student_id = %s ORDER BY created_at DESC
        """, (student_id,))
        history = cur.fetchall()
        return jsonify({'student': student_info, 'history': history})
    finally: cur.close(); conn.close()

@app.route('/staff/action', methods=['POST'])
def leave_action():
    if 'user_id' not in session: return redirect(url_for('login'))
    req_id = request.form.get('request_id')
    action = request.form.get('action')
    reason = request.form.get('rejection_reason', 'No reason provided')
    staff_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT status FROM leave_requests WHERE id = %s', (req_id,))
        req = cur.fetchone()
        if not req: return redirect(url_for('staff_dashboard'))
        if action == 'rejected':
            cur.execute("UPDATE leave_requests SET status = 'rejected', rejection_reason = %s WHERE id = %s", (reason, req_id))
        elif action == 'approved':
            if session.get('is_mentor') and req['status'] == 'pending_mentor':
                cur.execute("UPDATE leave_requests SET status = 'pending_cp', mentor_approved_by = %s WHERE id = %s", (staff_id, req_id))
            elif session.get('is_cp') and req['status'] == 'pending_cp':
                cur.execute("UPDATE leave_requests SET status = 'pending_hod', cp_approved_by = %s WHERE id = %s", (staff_id, req_id))
            elif session.get('is_hod') and req['status'] == 'pending_hod':
                cur.execute("UPDATE leave_requests SET status = 'approved_final', hod_approved_by = %s WHERE id = %s", (staff_id, req_id))
        conn.commit()
    finally: cur.close(); conn.close()
    return redirect(url_for('staff_dashboard'))

@app.route('/staff/bulk-assign', methods=['POST'])
def bulk_assign():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))
    file = request.files.get('excel_file')
    if not file: return redirect(url_for('staff_dashboard'))
    try:
        df = pd.read_excel(file)
        df.columns = df.columns.str.lower().str.strip()
        col_map = {
            'roll': next((col for col in df.columns if 'roll' in col or 'reg' in col), None),
            'name': next((col for col in df.columns if 'name' in col), None),
            'mentor': next((col for col in df.columns if 'mentor' in col or 'advisor' in col), None),
            'p_mob': next((col for col in df.columns if 'parent' in col or 'father' in col), None)
        }
        available_cols = {'name': bool(col_map['name']), 'mentor': bool(col_map['mentor']), 'p_mob': bool(col_map['p_mob'])}
        preview_data = []
        for _, row in df.iterrows():
            roll = str(row.get(col_map['roll'])).strip().upper()
            if not roll or roll == 'NAN': continue
            try: calculated_batch = 2000 + int(roll[:2])
            except: calculated_batch = session.get('year')
            preview_data.append({
                'roll_number': roll,
                'name': str(row.get(col_map['name'])).strip() if col_map['name'] else "Unknown",
                'excel_mentor': str(row.get(col_map['mentor'])).strip() if col_map['mentor'] else None,
                'father_mobile': str(row.get(col_map['p_mob'])).strip() if col_map['p_mob'] else None,
                'batch_year': calculated_batch, 'section': session.get('section'), 'department': 'CSE'
            })
        return render_template('preview.html', data=preview_data, available_cols=available_cols)
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('staff_dashboard'))

@app.route('/staff/bulk-confirm', methods=['POST'])
def bulk_confirm():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))
    students = json.loads(request.form.get('data_json'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        for s in students:
            cur.execute("SELECT id FROM students WHERE UPPER(roll_number) = UPPER(%s)", (s['roll_number'],))
            exists = cur.fetchone()
            if exists:
                cur.execute("UPDATE students SET name=%s, excel_mentor_name=%s, father_mobile=%s, batch_year=%s, section=%s, department=%s WHERE id=%s",
                            (s['name'], s['excel_mentor'], s['father_mobile'], s['batch_year'], s['section'], s['department'], exists[0]))
            else:
                cur.execute("INSERT INTO students (roll_number, name, excel_mentor_name, father_mobile, batch_year, section, department) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (s['roll_number'], s['name'], s['excel_mentor'], s['father_mobile'], s['batch_year'], s['section'], s['department']))
        conn.commit(); flash('Import Successful', 'success')
    finally: cur.close(); conn.close()
    return redirect(url_for('staff_dashboard'))

@app.route('/staff/mark-absent', methods=['POST'])
def mark_absent():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))
    rolls, f_date, t_date = request.form.get('absent_roll_numbers'), request.form.get('from_date'), request.form.get('to_date')
    e_type = request.form.get('entry_type')
    reason = f"Manual Entry: {'Call Permission' if e_type == 'informed' else 'Uninformed Absence'}"
    conn = get_db_connection(); cur = conn.cursor()
    try:
        for r in [x.strip() for x in rolls.split(',') if x.strip()]:
            cur.execute("SELECT id FROM students WHERE UPPER(roll_number)=UPPER(%s)", (r,))
            sid = cur.fetchone()
            if sid:
                cur.execute("INSERT INTO leave_requests (student_id, leave_type, from_date, to_date, reason, status, cp_approved_by) VALUES (%s, 'leave', %s, %s, %s, 'approved_final', %s)", (sid[0], f_date, t_date, reason, session['user_id']))
        conn.commit(); flash('Records saved.', 'success')
    finally: cur.close(); conn.close()
    return redirect(url_for('staff_dashboard'))

# ==========================================
# STUDENT ROUTES
# ==========================================

@app.route('/student/dashboard')
def student_dashboard():
    user_id = session.get('user_id')
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT *, TO_CHAR(from_date, 'DD Mon YYYY') as from_date_clean, 
                TO_CHAR(to_date, 'DD Mon YYYY') as to_date_clean,
                (to_date - from_date + 1) as duration FROM leave_requests WHERE student_id = %s ORDER BY created_at DESC""", (user_id,))
    reqs = cur.fetchall()
    cur.execute("SELECT SUM(to_date-from_date+1) FROM leave_requests WHERE student_id=%s AND leave_type='leave' AND status!='rejected' AND from_date>=date_trunc('week', CURRENT_DATE)", (user_id,))
    w = cur.fetchone()['sum'] or 0
    cur.close(); conn.close()
    return render_template('student.html', requests=reqs, weekly_leaves=w)


@app.route('/staff/my-history')
def staff_action_history():
    user_id = session.get('user_id')
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT s.name as student_name, s.roll_number, s.batch_year, s.section,
        TO_CHAR(lr.from_date, 'YYYY-MM-DD') as from_date_raw,
        TO_CHAR(lr.from_date, 'DD Mon YYYY') as from_date_clean,
        TO_CHAR(lr.to_date, 'DD Mon YYYY') as to_date_clean,
        (lr.to_date - lr.from_date + 1) as duration,
        lr.status as final_status,
        -- 🌟 CHANGED: updated_at -> created_at
        TO_CHAR(lr.created_at, 'DD Mon, HH:MI AM') as processed_at 
        FROM leave_requests lr JOIN students s ON lr.student_id = s.id
        WHERE lr.mentor_approved_by = %s OR lr.cp_approved_by = %s OR lr.hod_approved_by = %s
        ORDER BY lr.created_at DESC LIMIT 100 
    """, (user_id, user_id, user_id))
    hist = cur.fetchall(); cur.close(); conn.close()
    return jsonify(hist)
# ==========================================
# PARENT APP API (ANDROID)
# ==========================================

@app.route('/api/parent/login', methods=['POST'])
def api_parent_login():
    data = request.get_json()
    cur = get_db_connection().cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name, roll_number FROM students WHERE LOWER(name)=LOWER(%s) AND father_mobile=%s", (data.get('student_name'), data.get('father_mobile')))
    student = cur.fetchone()
    if student: return jsonify({"success": True, "student_id": student['id'], "student_name": student['name'], "roll_number": student['roll_number']})
    return jsonify({"success": False, "message": "Invalid details"}), 401

@app.route('/api/parent/dashboard', methods=['POST'])
def api_parent_dashboard():
    data = request.get_json()
    cur = get_db_connection().cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT leave_type, from_date, to_date, status, reason FROM leave_requests WHERE student_id=%s ORDER BY created_at DESC", (data.get('student_id'),))
    leaves = cur.fetchall()
    cur.execute("SELECT title as holiday_name, TO_CHAR(date, 'DD Mon YYYY') as holiday_date FROM college_holidays WHERE date >= CURRENT_DATE")
    holidays = cur.fetchall()
    return jsonify({"success": True, "child_leaves": leaves, "college_holidays": holidays})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)