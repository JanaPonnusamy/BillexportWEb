from flask import Flask, request, redirect, url_for, render_template, session, send_from_directory, jsonify
import os
import json
import pandas as pd
import logging
import requests
import csv
import chardet
import subprocess


app = Flask(__name__)
app.secret_key = 'your-secret-key'
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'txt'}

# Logging
logging.basicConfig(filename='flask_debug.log', level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def load_users():
    with open("users.json") as f:
        return json.load(f)


def load_bill_dataframe(filepath):
    # Detect encoding
    with open(filepath, 'rb') as f:
        result = chardet.detect(f.read())
    encoding = result['encoding']

    # Read and clean quotes
    with open(filepath, 'r', encoding=encoding) as f:
        content = f.read().replace('""', '"')

    temp_path = filepath + '_clean.csv'
    with open(temp_path, 'w', encoding=encoding) as f:
        f.write(content)

    first_line = content.splitlines()[0]
    if '\t' in first_line:
        delimiter = '\t'
    elif ',' in first_line:
        delimiter = ','
    elif ' ' in first_line:
        delimiter = r'\s+'
    else:
        delimiter = ','

    df = pd.read_csv(temp_path, sep=delimiter, engine='python', quoting=csv.QUOTE_NONE)

    # Clean column names
    df.columns = df.columns.str.strip().str.replace('"', '', regex=False).str.replace("'", '', regex=False)

    # Clean string cells
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].astype(str).apply(lambda x: x.replace('"', '').strip())

    # Convert dates to short format
    for col in ['TransactionDate', 'BillDate']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce', dayfirst=True).dt.strftime('%d/%m/%y')

    # Remove exact duplicates
    df.drop_duplicates(inplace=True)

    # Convert numeric columns
    for col in ['BillAmount', 'PurchasePrice', 'DiscountPercentage', 'MRP']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    app.logger.debug(f"✅ Final cleaned columns: {df.columns.tolist()}")
    return df


@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        users = load_users()
        username = request.form['username']
        password = request.form['password']

        user_info = users.get(username)
        if user_info and user_info['password'] == password:
            session['user'] = username
            session['customercode'] = user_info.get('customercode')
            session['customername'] = user_info.get('customername')
            return redirect('/index')

        return render_template('login.html', error="Invalid username or password")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')

@app.route('/index')
def index():
    if 'user' not in session:
        return redirect('/')

    if session['user'] == 'admin':
        return render_template('admin.html')

    filepath = os.path.join(UPLOAD_FOLDER, 'BillData.csv')
    if not os.path.exists(filepath):
        return "❌ BillData.csv not found", 404

    try:
        df = load_bill_dataframe(filepath)
    except Exception as e:
        return f"❌ Error loading CSV: {e}", 500

    if 'BNumber' not in df.columns or 'CustomerCode' not in df.columns:
        return f"❌ Required columns missing: {df.columns.tolist()}", 500

    user_code = session.get('customercode')
    user_df = df[df['CustomerCode'].astype(str) == str(user_code)]

    bill_numbers = sorted(user_df['BNumber'].dropna().unique())[::-1]

    bill_data_summary = []
    for bill_no in bill_numbers:
        bill_rows = user_df[user_df['BNumber'] == bill_no]
        bill_date = bill_rows['BillDate'].iloc[0] if 'BillDate' in bill_rows.columns else 'N/A'
        bill_amount = bill_rows['BillAmount'].iloc[0] if 'BillAmount' in bill_rows.columns else 0
        bill_data_summary.append({
            'bill_no': str(bill_no),
            'date': str(bill_date),
            'amount': float(bill_amount)
        })

    return render_template('index.html',
                           session=session,
                           bill_data=bill_data_summary)


@app.route('/api/bill/<bill_no>')
def api_bill_detail(bill_no):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    filepath = os.path.join(UPLOAD_FOLDER, 'BillData.csv')
    if not os.path.exists(filepath):
        return jsonify({'error': 'BillData.csv not found'}), 404

    try:
        df = load_bill_dataframe(filepath)

        if 'BNumber' not in df.columns:
            return jsonify({'error': 'Missing BNumber in CSV'}), 500

        selected = df[df['BNumber'] == bill_no]
        if selected.empty:
            return jsonify({'error': 'Bill not found'}), 404

        bill_date = selected['BillDate'].iloc[0] if 'BillDate' in selected.columns else 'N/A'
        bill_amount = selected['BillAmount'].iloc[0] if 'BillAmount' in selected.columns else 0.0
        html = selected.to_html(classes='table table-striped', index=False)

        return jsonify({
            'bill_no': str(bill_no),
            'date': str(bill_date),
            'amount': float(bill_amount),
            'html': html
        })

    except Exception as e:
        app.logger.exception("Failed to fetch bill detail")
        return jsonify({'error': f"Exception: {str(e)}"}), 500

@app.route('/download/<bill_number>/<ext>')
def download(bill_number, ext):
    if 'user' not in session:
        return redirect('/')
    if ext not in ALLOWED_EXTENSIONS:
        return "Invalid file type", 400

    filepath = os.path.join(UPLOAD_FOLDER, 'BillData.csv')
    if not os.path.exists(filepath):
        return "BillData.csv not found", 404

    df = load_bill_dataframe(filepath)
    if 'BNumber' not in df.columns:
        return "Missing 'BNumber' column", 500

    selected = df[df['BNumber'] == bill_number]
    if selected.empty:
        return "No data found", 404

    bill_number_clean = bill_number.replace('"', '').replace("'", '')
    temp_file = os.path.join(UPLOAD_FOLDER, f"Bill_{bill_number_clean}.{ext}")

    if ext == 'csv':
        selected.to_csv(temp_file, index=False)
    elif ext == 'xlsx':
        selected.to_excel(temp_file, index=False)
    elif ext == 'txt':
        selected.to_csv(temp_file, sep='\t', index=False)

    return send_from_directory(UPLOAD_FOLDER, os.path.basename(temp_file), as_attachment=True)

from flask import Response

@app.route('/trigger-fetch', methods=['GET'])
def trigger_fetch():
    if 'user' not in session or session['user'] != 'admin':
        return "Unauthorized", 403

    try:
        # Step 1: Call VB.NET API
        response = requests.get("http://122.252.246.181:8080/fetch", timeout=10)
        fetched_data = response.text

        # Step 2: Copy BillData.csv into current repo folder (optional if already inside repo)
        src_path = r"D:\VBDOTNET\BillExportWeb\uploads\BillData.csv"
        dst_path = os.path.join(os.getcwd(), 'uploads', 'BillData.csv')  # target inside repo

        # Ensure uploads folder exists in repo
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with open(src_path, 'rb') as src_file:
            with open(dst_path, 'wb') as dst_file:
                dst_file.write(src_file.read())

        # Step 3: Git commit and push
        repo_path = os.getcwd()
        subprocess.run(["git", "config", "user.name", "JanaPonnusamy"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "janaponnusamy@gmail.com"], cwd=repo_path, check=True)
        subprocess.run(["git", "add", "uploads/BillData.csv"], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-m", "Auto-update BillData.csv from VB.NET"], cwd=repo_path, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=repo_path, check=True)

        return f"✅ Data fetched & pushed to GitHub. VB.NET says: {fetched_data}"

    except subprocess.CalledProcessError as git_err:
        app.logger.exception("❌ Git command failed")
        return f"❌ Git error: {git_err}", 500

    except Exception as e:
        app.logger.exception("❌ Fetch failed")
        return f"❌ Failed to fetch or push: {str(e)}", 500


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404

if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    app.run(debug=True, host='0.0.0.0', port=5000)
