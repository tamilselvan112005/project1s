import os
import math
import pandas as pd
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask import  jsonify

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'supersecretkey')

def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- AUTH ROUTES ---
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

                session['user_id'] = student['id']
                session['role'] = 'student'
                session['name'] = student['name']
                return redirect(url_for('student_dashboard'))

            elif login_type == 'staff':
                cur.execute('SELECT * FROM staff WHERE email = %s', (identifier,))
                staff = cur.fetchone()

                try:
                    if staff and check_password_hash(staff['password'], password):
                        session['user_id'] = staff['id']
                        session['role'] = 'staff'
                        session['name'] = staff['name']
                        session['is_mentor'] = staff['is_mentor']
                        session['is_cp'] = staff['is_chairperson'] or staff['is_cochairperson']
                        session['is_hod'] = staff['is_hod']
                        session['year'] = staff['batch_year'] 
                        session['section'] = staff['section']
                        return redirect(url_for('staff_dashboard'))
                    else:
                        flash('Invalid credentials', 'error')
                        return redirect(url_for('login'))
                except ValueError:
                    flash('Old unhashed account detected. Please register a new one.', 'error')
                    return redirect(url_for('login'))
        finally:
            cur.close()
            conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = generate_password_hash(request.form.get('password'))
        is_mentor = request.form.get('is_mentor') == 'on'
        is_cp = request.form.get('is_chairperson') == 'on'
        is_cocp = request.form.get('is_cochairperson') == 'on'
        is_hod = request.form.get('is_hod') == 'on'
        
        year_raw = request.form.get('year_assigned')
        year_assigned = int(year_raw) if year_raw else None
        section = request.form.get('section') or None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('''
                INSERT INTO staff (name, email, password, is_mentor, is_chairperson, is_cochairperson, is_hod, batch_year, section) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) 
                RETURNING id
            ''', (name, email, password, is_mentor, is_cp, is_cocp, is_hod, year_assigned, section))
            
            new_staff_id = cur.fetchone()[0]
            mapped_count = 0

            # 2. AUTO-MAP MENTEES! (Deep-Clean Logic)
            if is_mentor and name:
                # Replace dots with spaces, split into words, and grab the longest word!
                # E.g., 'Anandh.A' -> 'Anandh A' -> 'Anandh'
                clean_name = name.replace('.', ' ')
                longest_word = max(clean_name.split(), key=len)
                search_name = f"%{longest_word}%"
                
                # Search the Excel column (ignoring spaces so it matches perfectly)
                cur.execute("""
                    UPDATE students 
                    SET mentor_id = %s 
                    WHERE REPLACE(excel_mentor_name, ' ', '') ILIKE %s AND mentor_id IS NULL
                """, (new_staff_id, search_name))
                
                mapped_count = cur.rowcount

            conn.commit()
            
            if mapped_count > 0:
                flash(f'Registered successfully! We also auto-assigned {mapped_count} students to you.', 'success')
            else:
                flash('Staff registered successfully!', 'success')
                
            return redirect(url_for('login'))
            
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('Email already registered!', 'error')
        finally:
            cur.close()
            conn.close()
            
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- STAFF ROUTES ---
@app.route('/staff/dashboard')
def staff_dashboard():
    if 'user_id' not in session or session.get('role') != 'staff':
        return redirect(url_for('login'))

    user_id = session['user_id']
    is_hod = session.get('is_hod')
    is_cp = session.get('is_cp')
    is_mentor = session.get('is_mentor')
    year = session.get('year') # This securely holds the batch_year
    section = session.get('section')

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    pending_requests = []
    class_list = []
    mentee_list = []
    all_mentors = []
    total_reqs = 0
    
    try:
        # 🌟 UPGRADE: Checks if the user is the Primary Mentor OR the Co-Mentor
        cur.execute("SELECT * FROM students WHERE mentor_id = %s OR co_mentor_id = %s ORDER BY roll_number", (user_id, user_id))
        mentee_list = cur.fetchall()

        if is_hod:
            cur.execute("""
                SELECT lr.*, s.name AS student_name, s.roll_number, 
                (SELECT COUNT(*) FROM leave_requests WHERE student_id = s.id AND leave_type = 'leave' AND status = 'approved_final' AND EXTRACT(MONTH FROM from_date) = EXTRACT(MONTH FROM CURRENT_DATE)) AS month_leaves,
                (SELECT COUNT(*) FROM leave_requests WHERE student_id = s.id AND leave_type = 'od' AND status = 'approved_final' AND EXTRACT(MONTH FROM from_date) = EXTRACT(MONTH FROM CURRENT_DATE)) AS month_ods
                FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE lr.status = 'pending_hod' ORDER BY lr.created_at ASC
            """)
            pending_requests = cur.fetchall()
            
        elif is_cp:
            cur.execute("SELECT id, name FROM staff WHERE is_mentor = TRUE ORDER BY name")
            all_mentors = cur.fetchall()

            # 🌟 UPGRADE: Checks BOTH mentor slots and uses batch_year
            cur.execute("""
                SELECT lr.*, s.name AS student_name, s.roll_number 
                FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE (s.batch_year = %s AND UPPER(TRIM(s.section)) = UPPER(TRIM(%s)) AND lr.status = 'pending_cp')
                   OR ((s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor')
                ORDER BY lr.created_at ASC
            """, (year, section, user_id, user_id))
            pending_requests = cur.fetchall()
            
            # 🌟 UPGRADE: Now joins the staff table twice to find the names of BOTH mentors for the roster!
            cur.execute("""
                SELECT s.*, st.name as system_mentor_name, st2.name as system_co_mentor_name
                FROM students s 
                LEFT JOIN staff st ON s.mentor_id = st.id
                LEFT JOIN staff st2 ON s.co_mentor_id = st2.id
                WHERE s.batch_year = %s AND UPPER(TRIM(s.section)) = UPPER(TRIM(%s)) 
                ORDER BY s.roll_number
            """, (year, section))
            class_list = cur.fetchall()
            
        elif is_mentor:
            # 🌟 UPGRADE: Checks BOTH mentor slots for standard mentors
            cur.execute("""
                SELECT lr.*, s.name AS student_name, s.roll_number 
                FROM leave_requests lr JOIN students s ON lr.student_id = s.id
                WHERE (s.mentor_id = %s OR s.co_mentor_id = %s) AND lr.status = 'pending_mentor'
                ORDER BY lr.created_at ASC
            """, (user_id, user_id))
            pending_requests = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM leave_requests")
        count_res = cur.fetchone()
        if count_res: total_reqs = count_res['count']
        
    finally:
        cur.close()
        conn.close()

    return render_template('staff.html', 
                           pending_requests=pending_requests, 
                           class_list=class_list,
                           mentee_list=mentee_list, 
                           all_mentors=all_mentors,
                           total_reqs=total_reqs)

@app.route('/staff/action', methods=['POST'])
def leave_action():
    if 'user_id' not in session or session.get('role') != 'staff': return redirect(url_for('login'))
    
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
        current_status = req['status']

        if action == 'rejected':
            cur.execute("UPDATE leave_requests SET status = 'rejected', rejection_reason = %s WHERE id = %s", (reason, req_id))
            flash('Request rejected.', 'success')
        elif action == 'approved':
            if session.get('is_mentor') and current_status == 'pending_mentor':
                cur.execute("UPDATE leave_requests SET status = 'pending_cp', mentor_approved_by = %s WHERE id = %s", (staff_id, req_id))
            elif session.get('is_cp') and current_status == 'pending_cp':
                cur.execute("UPDATE leave_requests SET status = 'pending_hod', cp_approved_by = %s WHERE id = %s", (staff_id, req_id))
            elif session.get('is_hod') and current_status == 'pending_hod':
                cur.execute("UPDATE leave_requests SET status = 'approved_final', hod_approved_by = %s WHERE id = %s", (staff_id, req_id))
            flash('Request approved and moved to next stage.', 'success')
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('staff_dashboard'))



@app.route('/staff/bulk-assign', methods=['POST'])
def bulk_assign():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))
    
    if 'excel_file' not in request.files:
        flash('No file uploaded', 'error')
        return redirect(url_for('staff_dashboard'))
        
    file = request.files['excel_file']
    if file.filename == '':
        return redirect(url_for('staff_dashboard'))

    try:
        df = pd.read_excel(file)
        df.columns = df.columns.str.lower().str.strip()

        col_map = {
            'roll': next((col for col in df.columns if 'roll' in col or 'reg' in col), None),
            'name': next((col for col in df.columns if 'name' in col), None),
            'mentor': next((col for col in df.columns if 'mentor' in col or 'advisor' in col), None),
            's_mob': next((col for col in df.columns if 'student mobile' in col or 'student ph' in col), None),
            'p_mob': next((col for col in df.columns if 'parent mobile' in col or 'parent ph' in col), None)
        }

        if not col_map['roll']:
            flash("Error: Could not find 'Roll Number' column.", "error")
            return redirect(url_for('staff_dashboard'))

        # 🌟 Auto-Detect Dictionary (Tells the HTML which checkboxes to enable!)
        available_cols = {
            'name': bool(col_map['name']),
            'mentor': bool(col_map['mentor']),
            's_mob': bool(col_map['s_mob']),
            'p_mob': bool(col_map['p_mob'])
        }

        preview_data = []
        for index, row in df.iterrows():
            if pd.isna(row.get(col_map['roll'])): continue

            def clean_val(val):
                if pd.isna(val) or str(val).strip().lower() == 'nan': return None
                if isinstance(val, float): return str(int(val)) 
                return str(val).strip()

            preview_data.append({
                'roll_number': clean_val(row.get(col_map['roll'])),
                'name': clean_val(row.get(col_map['name'])) if available_cols['name'] else None,
                'excel_mentor': clean_val(row.get(col_map['mentor'])) if available_cols['mentor'] else None,
                'student_mobile': clean_val(row.get(col_map['s_mob'])) if available_cols['s_mob'] else None,
                'parent_mobile': clean_val(row.get(col_map['p_mob'])) if available_cols['p_mob'] else None
            })

        return render_template('preview.html', data=preview_data, year=session.get('year'), section=session.get('section'), available_cols=available_cols)

    except Exception as e:
        flash(f'Error reading Excel: {str(e)}', 'error')
        return redirect(url_for('staff_dashboard'))

@app.route('/staff/bulk-confirm', methods=['POST'])
def bulk_confirm():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))

    data_json = request.form.get('data_json')
    students_to_update = json.loads(data_json)
    year = session.get('year')
    section = session.get('section')

    # 🌟 Read the CP's Checkbox Choices!
    import_name = request.form.get('import_name') == '1'
    import_mentor = request.form.get('import_mentor') == '1'
    import_s_mob = request.form.get('import_s_mob') == '1'
    import_p_mob = request.form.get('import_p_mob') == '1'

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor) 
    updated = inserted = auto_mapped = 0
    
    try:
        for student in students_to_update:
            cur.execute("SELECT id FROM students WHERE UPPER(roll_number) = UPPER(%s)", (student['roll_number'],))
            exists = cur.fetchone()
            
            if exists:
                # 🌟 Dynamic UPDATE Builder (Only updates what CP checked!)
                update_fields = []
                params = []
                
                if import_name and student['name']: 
                    update_fields.append("name = %s")
                    params.append(student['name'])
                if import_mentor and student['excel_mentor']: 
                    update_fields.append("excel_mentor_name = %s")
                    params.append(student['excel_mentor'])
                if import_s_mob and student['student_mobile']: 
                    update_fields.append("student_mobile = %s")
                    params.append(student['student_mobile'])
                if import_p_mob and student['parent_mobile']: 
                    update_fields.append("parent_mobile = %s")
                    params.append(student['parent_mobile'])

                if update_fields:
                    query = f"UPDATE students SET {', '.join(update_fields)} WHERE id = %s"
                    params.append(exists['id'])
                    cur.execute(query, tuple(params))
                    updated += 1
            else:
                # 🌟 Dynamic INSERT Builder (For brand new students)
                insert_cols = ["roll_number", "batch_year", "section"]
                insert_vals = ["%s", "%s", "%s"]
                params = [student['roll_number'], year, section]

                if import_name: insert_cols.append("name"); insert_vals.append("%s"); params.append(student['name'])
                if import_mentor: insert_cols.append("excel_mentor_name"); insert_vals.append("%s"); params.append(student['excel_mentor'])
                if import_s_mob: insert_cols.append("student_mobile"); insert_vals.append("%s"); params.append(student['student_mobile'])
                if import_p_mob: insert_cols.append("parent_mobile"); insert_vals.append("%s"); params.append(student['parent_mobile'])

                query = f"INSERT INTO students ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"
                cur.execute(query, tuple(params))
                inserted += 1

        # ONLY run the Magic Auto-Mapper if they actually imported the Mentor column!
        if import_mentor:
            cur.execute("SELECT id, name FROM staff WHERE is_mentor = TRUE")
            for mentor in cur.fetchall():
                clean_name = mentor['name'].replace('.', ' ')
                search_name = f"%{max(clean_name.split(), key=len) if clean_name.split() else ''}%"
                
                if search_name != "%%":
                    cur.execute("UPDATE students SET mentor_id = %s WHERE REPLACE(excel_mentor_name, ' ', '') ILIKE %s AND mentor_id IS NULL", (mentor['id'], search_name))
                    auto_mapped += cur.rowcount
                    cur.execute("UPDATE students SET co_mentor_id = %s WHERE REPLACE(excel_mentor_name, ' ', '') ILIKE %s AND mentor_id IS NOT NULL AND mentor_id != %s AND co_mentor_id IS NULL", (mentor['id'], search_name, mentor['id']))
                    auto_mapped += cur.rowcount
            
        conn.commit()
        flash(f'Success! {updated} updated, {inserted} new. Auto-Assigned {auto_mapped} mentors!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Database error: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('staff_dashboard'))

@app.route('/staff/update-student-mentor', methods=['POST'])
def update_student_mentor():
    if not session.get('is_cp'): 
        return redirect(url_for('staff_dashboard'))

    student_id = request.form.get('student_id')
    mentor_id = request.form.get('mentor_id')
    co_mentor_id = request.form.get('co_mentor_id')

    # If they select "-- None --", we turn it into a SQL NULL
    mentor_id = mentor_id if mentor_id else None
    co_mentor_id = co_mentor_id if co_mentor_id else None

    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            UPDATE students 
            SET mentor_id = %s, co_mentor_id = %s 
            WHERE id = %s
        """, (mentor_id, co_mentor_id, student_id))
        conn.commit()
        flash('Mentors manually updated!', 'success')
    except Exception as e:
        conn.rollback()
        flash('Error updating mentors.', 'error')
        print("Database error:", e) # Good for debugging!
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('staff_dashboard'))

@app.route('/student/dashboard')
def student_dashboard():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 1. Fetch their entire history
        cur.execute("SELECT * FROM leave_requests WHERE student_id = %s ORDER BY created_at DESC", (user_id,))
        requests = cur.fetchall()

        # 2. LIVE QUOTA TRACKER (Counts only 'leaves', not ODs, ignoring rejected ones)
        
        # Weekly (Monday to Sunday)
        cur.execute("""
            SELECT COUNT(*) FROM leave_requests 
            WHERE student_id = %s AND leave_type = 'leave' AND status != 'rejected' 
            AND from_date >= date_trunc('week', CURRENT_DATE)
        """, (user_id,))
        weekly_leaves = cur.fetchone()['count']

        # Monthly (1st to end of month)
        cur.execute("""
            SELECT COUNT(*) FROM leave_requests 
            WHERE student_id = %s AND leave_type = 'leave' AND status != 'rejected' 
            AND from_date >= date_trunc('month', CURRENT_DATE)
        """, (user_id,))
        monthly_leaves = cur.fetchone()['count']

        # Semester (Rolling 6 months)
        cur.execute("""
            SELECT COUNT(*) FROM leave_requests 
            WHERE student_id = %s AND leave_type = 'leave' AND status != 'rejected' 
            AND from_date >= CURRENT_DATE - INTERVAL '6 months'
        """, (user_id,))
        semester_leaves = cur.fetchone()['count']

    finally:
        cur.close()
        conn.close()

    return render_template('student.html', 
                           requests=requests, 
                           weekly_leaves=weekly_leaves, 
                           monthly_leaves=monthly_leaves, 
                           semester_leaves=semester_leaves)

@app.route('/student/apply', methods=['POST'])
def apply_leave():
    if 'user_id' not in session or session.get('role') != 'student':
        return redirect(url_for('login'))

    user_id = session['user_id']
    leave_type = request.form.get('leave_type')
    from_date_str = request.form.get('from_date')
    to_date_str = request.form.get('to_date')
    reason = request.form.get('reason')
    
    # Handle File Upload
    proof_file = request.files.get('proof_file')
    proof_filename = None
    if proof_file and proof_file.filename != '':
        filename = secure_filename(proof_file.filename)
        proof_filename = f"student_{user_id}_{filename}" 
        proof_file.save(os.path.join(app.config['UPLOAD_FOLDER'], proof_filename))

    # Calculate Emergency (If applying for TODAY or earlier)
    from_date_obj = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    today = datetime.today().date()
    
    is_emergency = False
    status = 'pending_mentor' # Default routing
    
    if from_date_obj <= today:
        is_emergency = True
        status = 'pending_cp' # EMERGENCY BYPASS! Skips Mentor.

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO leave_requests (student_id, leave_type, from_date, to_date, reason, status, is_emergency, proof_file_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, leave_type, from_date_str, to_date_str, reason, status, is_emergency, proof_filename))
        conn.commit()
        
        if is_emergency:
            flash('🚨 Emergency Leave submitted! Routed directly to your CP.', 'success')
        else:
            flash('Leave application submitted to your Mentor.', 'success')
            
    except Exception as e:
        conn.rollback()
        flash(f'Error submitting application: {e}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('student_dashboard'))

@app.route('/staff/student-details/<int:student_id>', methods=['GET'])
def get_student_details(student_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Get Student Info
        cur.execute("SELECT id, name, roll_number, batch_year, section FROM students WHERE id = %s", (student_id,))
        student_info = cur.fetchone()

        # 2. Get their Leave History (Approved or Rejected)
        cur.execute("""
            SELECT id, leave_type, from_date, to_date, reason, status 
            FROM leave_requests 
            WHERE student_id = %s AND status IN ('approved_final', 'rejected', 'processed_rejected')
            ORDER BY created_at DESC
        """, (student_id,))
        leave_history = cur.fetchall()

        return jsonify({
            'student': student_info,
            'history': leave_history
        })
    finally:
        cur.close()
        conn.close()
@app.route('/staff/mark-absent', methods=['POST'])
def mark_absent():
    if not session.get('is_cp'): return redirect(url_for('staff_dashboard'))

    roll_numbers = request.form.get('absent_roll_numbers') # e.g., "23UCS002, 23UCS008"
    date = request.form.get('absent_date') # The date they were absent

    roll_list = [r.strip() for r in roll_numbers.split(',')]

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for roll in roll_list:
            # First, find the student ID from the roll number
            cur.execute("SELECT id FROM students WHERE roll_number = %s", (roll,))
            student = cur.fetchone()

            if student:
                # Insert a forced 'leave' record. 
                # Set status to 'approved_final' so it counts against them immediately.
                cur.execute("""
                    INSERT INTO leave_requests (student_id, leave_type, from_date, to_date, reason, status)
                    VALUES (%s, 'leave', %s, %s, 'Uninformed Absence marked by CP', 'approved_final')
                """, (student[0], date, date))

        conn.commit()
        flash('Absences recorded successfully.', 'success')
    except Exception as e:
        conn.rollback()
        flash('Error recording absences.', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('staff_dashboard'))

from flask import jsonify, request

# ==========================================
# PARENT ANDROID APP API ROUTES
# ==========================================

@app.route('/api/parent/login', methods=['POST'])
def api_parent_login():
    # Android Studio will send data as JSON
    data = request.get_json()
    student_name = data.get('student_name')
    father_mobile = data.get('father_mobile')

    if not student_name or not father_mobile:
        return jsonify({"success": False, "message": "Missing name or mobile number"}), 400

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # LOWER() makes it case-insensitive (e.g., "Bharath" matches "BHARATH")
        # Ensure your database column name matches exactly (e.g., father_mobile)
        cur.execute("""
            SELECT id, name, roll_number 
            FROM students 
            WHERE LOWER(name) = LOWER(%s) AND father_mobile = %s
        """, (student_name.strip(), father_mobile.strip()))
        
        student = cur.fetchone()
        
        if student:
            return jsonify({
                "success": True, 
                "message": "Login successful",
                "student_id": student['id'],
                "student_name": student['name'],
                "roll_number": student['roll_number']
            }), 200
        else:
            return jsonify({"success": False, "message": "Invalid student name or father's mobile number"}), 401
            
    finally:
        cur.close()
        conn.close()


@app.route('/api/parent/dashboard', methods=['POST'])
def api_parent_dashboard():
    data = request.get_json()
    student_id = data.get('student_id')

    if not student_id:
        return jsonify({"success": False, "message": "Student ID required"}), 400

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 1. Get leave requests made by their child
        cur.execute("""
            SELECT id, leave_type, from_date, to_date, reason, status, created_at
            FROM leave_requests 
            WHERE student_id = %s 
            ORDER BY created_at DESC
        """, (student_id,))
        child_leaves = cur.fetchall()

        # 2. Get official holidays/leaves given by the college 
        # (Assuming you have a 'college_holidays' table)
        cur.execute("""
            SELECT title, date, description 
            FROM college_holidays 
            WHERE date >= CURRENT_DATE 
            ORDER BY date ASC
        """)
        college_holidays = cur.fetchall()

        return jsonify({
            "success": True,
            "child_leaves": child_leaves,
            "college_holidays": college_holidays
        }), 200
        
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)