import os
import math
import pandas as pd
import re

from thefuzz import fuzz, process
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
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = generate_password_hash(request.form.get('password'))
        
        # Proper boolean handling for checkboxes
        is_mentor = request.form.get('is_mentor') == 'on'
        is_cp = request.form.get('is_chairperson') == 'on'
        is_cocp = request.form.get('is_cochairperson') == 'on'
        is_hod = request.form.get('is_hod') == 'on'
        
        batch_year = request.form.get('batch_year') or None
        section = request.form.get('section') or None

        conn = get_db_connection()
        cur = conn.cursor()
        
        try:
            # 1. First, try to insert the staff member
            cur.execute('''
                INSERT INTO staff (name, email, password, is_mentor, is_chairperson, is_cochairperson, is_hod, batch_year, section) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            ''', (name, email, password, is_mentor, is_cp, is_cocp, is_hod, batch_year, section))
            new_id = cur.fetchone()[0]

            # 2. Try the Auto-Match (Wrapped in its own logic to prevent crashes)
            if name:
                clean_name = name.replace('.', ' ')
                name_parts = clean_name.split()
                if name_parts:
                    search_name = f"%{max(name_parts, key=len)}%"
                    cur.execute("""
                        UPDATE students 
                        SET mentor_id = %s 
                        WHERE excel_mentor_name ILIKE %s AND mentor_id IS NULL
                    """, (new_id, search_name))
            
            conn.commit()
            flash('Registered successfully! Please login.', 'success')
            return redirect(url_for('login'))

        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash('This email address is already registered.', 'error')
        except Exception as e:
            conn.rollback()
            # This will show you the ACTUAL error in your terminal
            print(f"Registration Error: {e}") 
            flash('An unexpected error occurred. Please try again.', 'error')
        finally: 
            cur.close()
            conn.close()

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
        # 1. Fetch Mentees with detailed info
        cur.execute("SELECT * FROM students WHERE mentor_id = %s OR co_mentor_id = %s ORDER BY roll_number", (user_id, user_id))
        mentee_list = cur.fetchall()

        # 2. Main Query with Time (IST) and Date Ranges
        query_base = """
        SELECT lr.id, lr.student_id, lr.leave_type, lr.reason, lr.status, lr.is_emergency, lr.proof_file,
        TO_CHAR(lr.from_date, 'DD Mon YYYY') as from_date,
        TO_CHAR(lr.to_date, 'DD Mon YYYY') as to_date,
        TO_CHAR(lr.from_date, 'YYYY-MM-DD') as from_date_raw,
        (lr.to_date - lr.from_date + 1) as duration,
        TO_CHAR(lr.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata', 'DD Mon, hh:mi AM') as applied_at_ist,
        s.name AS student_name, s.roll_number, s.batch_year, s.section,
        -- Stats based on specific student and section
        COALESCE((SELECT SUM(to_date-from_date+1) FROM leave_requests WHERE student_id=s.id AND leave_type='leave' AND status='approved_final' AND from_date>=date_trunc('month', CURRENT_DATE)),0) as month_leaves,
        COALESCE((SELECT SUM(to_date-from_date+1) FROM leave_requests WHERE student_id=s.id AND leave_type='od' AND status='approved_final' AND from_date>=date_trunc('month', CURRENT_DATE)),0) as month_ods
        """

        if session.get('is_hod'):
            cur.execute(query_base + " FROM leave_requests lr JOIN students s ON lr.student_id = s.id WHERE lr.status = 'pending_hod' ORDER BY lr.created_at ASC")
        elif session.get('is_cp'):
            cur.execute(query_base + """ FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE (s.batch_year = %s AND TRIM(UPPER(s.section)) = TRIM(UPPER(%s)) AND lr.status = 'pending_cp')
                   OR ((s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor')
                ORDER BY lr.created_at ASC""", (year, section, user_id, user_id))
        else:
            cur.execute(query_base + " FROM leave_requests lr JOIN students s ON lr.student_id = s.id WHERE (s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor' ORDER BY lr.created_at ASC", (user_id, user_id))
        
        pending_requests = cur.fetchall()
        
        class_list, all_mentors = [], []
        if session.get('is_cp'):
            cur.execute("""
                SELECT s.id, s.roll_number, s.name, s.mentor_id, s.co_mentor_id, m1.name as mentor_name, m2.name as co_mentor_name
                FROM students s LEFT JOIN staff m1 ON s.mentor_id = m1.id LEFT JOIN staff m2 ON s.co_mentor_id = m2.id
                WHERE s.batch_year = %s AND TRIM(UPPER(s.section)) = TRIM(UPPER(%s)) ORDER BY s.roll_number
            """, (year, section))
            class_list = cur.fetchall()
            cur.execute("SELECT id, name FROM staff WHERE is_mentor = TRUE ORDER BY name")
            all_mentors = cur.fetchall()

    finally: cur.close(); conn.close()
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
            reason, status ,proof_file
            FROM leave_requests 
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


        
    
# --- HELPER: FUZZY MATCH LOGIC ---
def get_best_mentor_match(excel_name, staff_list):
    if not excel_name or str(excel_name).lower() == 'nan':
        return None, "No Match", "-"

    # 1. Clean the Excel name (remove titles like DR, MRS)
    clean_excel = re.sub(r'\b(DR|MR|MRS|MS|PROF)\b\.?', '', str(excel_name), flags=re.IGNORECASE).strip()
    
    # 2. Get list of names from DB
    staff_names = [s['name'] for s in staff_list]
    
    # 3. Fuzzy match (Score cutoff 75/100 to be safe)
    # token_sort_ratio ignores the order of names (e.g., "Anandh A" == "A Anandh")
    match = process.extractOne(clean_excel, staff_names, scorer=fuzz.token_sort_ratio, score_cutoff=75)
    
    if match:
        matched_name = match[0]
        staff_id = next(s['id'] for s in staff_list if s['name'] == matched_name)
        return staff_id, "Matched", matched_name
    
    return None, "No Match", "-"

# --- ROUTE 1: PREVIEW ---
@app.route('/staff/bulk-assign', methods=['POST'])
def bulk_assign():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))
    file = request.files.get('excel_file')
    if not file: return redirect(url_for('staff_dashboard'))
    
    try:
        # Fetch current system mentors for the AI to compare against
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, name FROM staff WHERE is_mentor = TRUE")
        system_staff = cur.fetchall()
        cur.close(); conn.close()

        df = pd.read_excel(file, dtype=str)
        df.columns = df.columns.str.lower().str.strip()
        
        col_map = {
            'roll': next((col for col in df.columns if 'roll' in col), None),
            'name': next((col for col in df.columns if 'name' in col and 'mentor' not in col), None),
            'mentor': next((col for col in df.columns if 'mentor' in col), None),
            'p_mob': next((col for col in df.columns if 'parent' in col or 'ph' in col or 'mobile' in col), None)
        }

        preview_data = []
        for _, row in df.iterrows():
            roll = str(row.get(col_map['roll'])).strip().upper()
            if not roll or roll == 'nan': continue

            # Phone Cleaning (.0 and nan)
            raw_phone = str(row.get(col_map['p_mob'])).strip().replace('.0', '')
            clean_phone = "" if raw_phone.lower() == 'nan' else raw_phone

            # AI Fuzzy Matching
            excel_mentor = str(row.get(col_map['mentor'])).strip()
            m_id, status, sys_name = get_best_mentor_match(excel_mentor, system_staff)

            preview_data.append({
                'roll_number': roll,
                'name': str(row.get(col_map['name'])).strip(),
                'excel_mentor': excel_mentor,
                'match_status': status,
                'system_name': sys_name,
                'matched_id': m_id, # ID found by AI
                'father_mobile': clean_phone,
                'batch_year': session.get('year'),
                'section': session.get('section')
            })
            
        return render_template('preview.html', data=preview_data)
        
    except Exception as e:
        flash(f'Excel Error: {str(e)}', 'error')
        return redirect(url_for('staff_dashboard'))

# --- ROUTE 2: CONFIRM (PROTECTS MANUAL "NONE" CHANGES) ---
@app.route('/staff/bulk-confirm', methods=['POST'])
def bulk_confirm():
    if not session.get('is_cp'): return redirect(url_for('login'))
    
    students = json.loads(request.form.get('data_json'))
    upd_name = request.form.get('import_name')
    upd_mentor = request.form.get('import_mentor')
    upd_p_mob = request.form.get('import_p_mob')
        
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        for s in students:
            cur.execute("SELECT name, mentor_id, father_mobile FROM students WHERE roll_number = %s", (s['roll_number'],))
            exists = cur.fetchone()
            
            if exists:
                # 1. Update Name?
                final_name = s['name'] if upd_name else exists['name']
                # 2. Update Phone?
                final_phone = s['father_mobile'] if upd_p_mob else exists['father_mobile']
                
                # 3. Update Mentor? (ONLY if checkbox is checked AND AI found a match)
                # This protects your manual "None" changes!
                final_mentor_id = s['matched_id'] if (upd_mentor and s['matched_id']) else exists['mentor_id']

                cur.execute("""
                    UPDATE students 
                    SET name=%s, excel_mentor_name=%s, father_mobile=%s, mentor_id=%s
                    WHERE roll_number = %s
                """, (final_name, s['excel_mentor'], final_phone, final_mentor_id, s['roll_number']))
            else:
                # New Student Insert
                cur.execute("""
                    INSERT INTO students (roll_number, name, excel_mentor_name, father_mobile, mentor_id, batch_year, section) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (s['roll_number'], s['name'], s['excel_mentor'], s['father_mobile'], s['matched_id'], s['batch_year'], s['section']))
        
        conn.commit()
        flash(f'Successfully synced {len(students)} students!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Database Error: {str(e)}', 'error')
    finally:
        cur.close(); conn.close()
        
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
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 🌟 FIXED QUERY: Added spaces and explicit FROM clause
        cur.execute("""
            SELECT *, 
            TO_CHAR(from_date, 'DD Mon YYYY') as from_date_clean, 
            TO_CHAR(to_date, 'DD Mon YYYY') as to_date_clean,
            (to_date - from_date + 1) as duration,
            proof_file 
            FROM leave_requests 
            WHERE student_id = %s 
            ORDER BY created_at DESC
        """, (user_id,))
        reqs = cur.fetchall()

        # 2. Weekly Quota
        cur.execute("""SELECT SUM(to_date-from_date+1) FROM leave_requests 
                    WHERE student_id=%s AND leave_type='leave' AND status!='rejected' 
                    AND from_date>=date_trunc('week', CURRENT_DATE)""", (user_id,))
        w = cur.fetchone()['sum'] or 0

        # 3. Monthly Quota (🌟 Added this to fix your error)
        cur.execute("""SELECT SUM(to_date-from_date+1) FROM leave_requests 
                    WHERE student_id=%s AND leave_type='leave' AND status!='rejected' 
                    AND from_date>=date_trunc('month', CURRENT_DATE)""", (user_id,))
        m = cur.fetchone()['sum'] or 0

        # 4. Semester Quota (🌟 Added this to fix your error)
        cur.execute("""SELECT SUM(to_date-from_date+1) FROM leave_requests 
                    WHERE student_id=%s AND leave_type='leave' AND status!='rejected' 
                    AND from_date >= CURRENT_DATE - INTERVAL '6 months'""", (user_id,))
        s = cur.fetchone()['sum'] or 0

    finally:
        cur.close(); conn.close()

    # Make sure all three variables are passed here!
    return render_template('student.html', 
                           requests=reqs, 
                           weekly_leaves=w, 
                           monthly_leaves=m, 
                           semester_leaves=s)
@app.route('/student/delete-request/<int:req_id>', methods=['POST'])
def delete_request(req_id):
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Check if the request exists, belongs to the student, and is NOT approved/rejected yet
        cur.execute("""
            SELECT proof_file FROM leave_requests 
            WHERE id = %s AND student_id = %s AND status LIKE 'pending_%%'
        """, (req_id, user_id))
        row = cur.fetchone()

        if row:
            # Optional: Delete the physical file from the uploads folder
            proof_filename = row[0]
            if proof_filename:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], proof_filename)
                if os.path.exists(file_path):
                    os.remove(file_path)

            cur.execute("DELETE FROM leave_requests WHERE id = %s", (req_id,))
            conn.commit()
            flash('Request cancelled and deleted successfully.', 'success')
        else:
            flash('Unable to delete request. It may have already been processed.', 'error')
            
    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'error')
    finally:
        cur.close(); conn.close()

    return redirect(url_for('student_dashboard'))
# ==========================================
# STUDENT APPLY (Fixed order)
# ==========================================
@app.route('/student/apply', methods=['POST'])
def student_apply():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('login'))

    user_id = session['user_id']
    l_type = request.form.get('leave_type')
    f_date = request.form.get('from_date')
    t_date = request.form.get('to_date')
    reason = request.form.get('reason')
    is_emergency = request.form.get('is_emergency') == 'on'

    filename = None
    if 'proof' in request.files:
        file = request.files['proof']
        if file and file.filename != '':
            ext = file.filename.rsplit('.', 1)[1].lower()
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            filename = secure_filename(f"student_{user_id}_{timestamp}.{ext}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 🌟 FIXED: match placeholders (%s) to column order
        cur.execute("""
            INSERT INTO leave_requests 
            (student_id, leave_type, from_date, to_date, reason, status, is_emergency, proof_file)
            VALUES (%s, %s, %s, %s, %s, 'pending_mentor', %s, %s)
        """, (user_id, l_type, f_date, t_date, reason, is_emergency, filename))
        
        conn.commit()
        flash('Leave applied successfully!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'error')
    finally:
        cur.close(); conn.close()

    return redirect(url_for('student_dashboard'))

@app.route('/staff/update-mentors', methods=['POST'])
def update_mentors():
    if not session.get('is_cp'): return redirect(url_for('login'))
    sid = request.form.get('student_id')
    m1 = request.form.get('mentor_id') or None
    m2 = request.form.get('co_mentor_id') or None
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE students SET mentor_id = %s, co_mentor_id = %s WHERE id = %s", (m1, m2, sid))
    conn.commit(); cur.close(); conn.close()
    flash('Mentors updated!', 'success')
    return redirect(url_for('staff_dashboard'))

@app.route('/staff/my-history')
def staff_action_history():
    user_id = session.get('user_id')
    cur = get_db_connection().cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT s.name as student_name, s.roll_number, s.batch_year, s.section, lr.proof_file,
        TO_CHAR(lr.from_date, 'DD Mon YYYY') as from_date_clean, (lr.to_date - lr.from_date + 1) as duration,
        lr.status as final_status, TO_CHAR(lr.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata', 'DD Mon, hh:mi AM') as processed_at 
        FROM leave_requests lr JOIN students s ON lr.student_id = s.id
        WHERE lr.mentor_approved_by = %s OR lr.cp_approved_by = %s OR lr.hod_approved_by = %s
        ORDER BY lr.created_at DESC LIMIT 100 
    """, (user_id, user_id, user_id))
    hist = cur.fetchall(); cur.close()
    return jsonify(hist)
# ==========================================
# PARENT APP API (ANDROID)
# ========================================

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
    
    # Always good practice to handle connections with try/finally
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Enhanced query matching the staff dashboard format
        cur.execute("""
            SELECT 
                id, 
                leave_type, 
                reason, 
                status, 
                is_emergency,
                TO_CHAR(from_date, 'DD Mon YYYY') as from_date,
                TO_CHAR(to_date, 'DD Mon YYYY') as to_date,
                (to_date - from_date + 1) as duration,
                TO_CHAR(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata', 'DD Mon, hh:mi AM') as applied_at_ist
            FROM leave_requests 
            WHERE student_id=%s 
            ORDER BY created_at DESC
        """, (data.get('student_id'),))
        
        leaves = cur.fetchall()
        
        cur.execute("""
            SELECT title as holiday_name, 
                   TO_CHAR(date, 'DD Mon YYYY') as holiday_date 
            FROM college_holidays 
            WHERE date >= CURRENT_DATE
        """)
        holidays = cur.fetchall()
        
        return jsonify({
            "success": True, 
            "child_leaves": leaves, 
            "college_holidays": holidays
        })
        
    except Exception as e:
        print("Error fetching parent dashboard:", e)
        return jsonify({"success": False, "message": "Server error"}), 500
        
    finally:
        cur.close()
        conn.close()

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)