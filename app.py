from flask import Flask, render_template, request, redirect, url_for, session, send_file
import pandas as pd
import os
import uuid
import json
import urllib.parse
from io import BytesIO
from reportlab.lib.pagesizes import letter, landscape, portrait
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

app = Flask(__name__)
app.secret_key = 'trip_tracker_final_2025'

TRIPS_FOLDER = 'trips_data'
CONFIG_FILE = 'trips_config.json'

if not os.path.exists(TRIPS_FOLDER):
    os.makedirs(TRIPS_FOLDER)

def get_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f)

def get_trip_file():
    trip_id = session.get('current_trip', 'default')
    return os.path.join(TRIPS_FOLDER, f"expense_{trip_id}.csv")

def load_data():
    file_path = get_trip_file()
    if not os.path.exists(file_path):
        df = pd.DataFrame(columns=['id', 'Date', 'Description', 'Payer', 'Total Amount'])
        df.to_csv(file_path, index=False)
    return pd.read_csv(file_path)

def get_settlements(balances):
    debtors = [[n, abs(b)] for n, b in balances.items() if b < 0]
    creditors = [[n, b] for n, b in balances.items() if b > 0]
    settlements = []
    while debtors and creditors:
        d, c = debtors[0], creditors[0]
        pay = min(d[1], c[1])
        if pay > 0.01:
            settlements.append(f"{d[0]} pays {c[0]}: ₹{round(pay, 2)}")
        d[1] -= pay; c[1] -= pay
        if d[1] < 0.01: debtors.pop(0)
        if c[1] < 0.01: creditors.pop(0)
    return settlements

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST' and 'start_trip' in request.form:
        trip_name = request.form.get('trip_name').replace(" ", "_")
        pwd = request.form.get('password')
        config = get_config()
        
        if trip_name in config and config[trip_name].get('password'):
            if config[trip_name]['password'] != pwd:
                return "Incorrect Password!", 403
        
        session.clear()
        session['current_trip'] = trip_name
        if trip_name not in config:
            config[trip_name] = {'password': pwd if pwd else None, 'locked': False}
            save_config(config)
        session['participants'] = [n.strip() for n in request.form.get('names', '').split(',') if n.strip()]
        return redirect(url_for('index'))

    trip_id = session.get('current_trip')
    if not trip_id:
        files = [f for f in os.listdir(TRIPS_FOLDER) if f.startswith('expense_') and f.endswith('.csv')]
        existing_trips = [f.replace('expense_', '').replace('.csv', '') for f in files]
        return render_template('setup.html', existing_trips=existing_trips, config=get_config())

    participants, df = session.get('participants', []), load_data()
    # Reconstruct participants if loading an existing trip
    if not participants and not df.empty:
        participants = [col for col in df.columns if not col.endswith('_Eligible') and col not in ['id', 'Date', 'Description', 'Payer', 'Total Amount']]
        session['participants'] = participants

    totals, balances = {}, {name: 0.0 for name in participants}
    if not df.empty:
        totals['Total Amount'] = df['Total Amount'].sum()
        for name in participants:
            col_sum = df[name].sum() if name in df.columns else 0.0
            totals[name] = col_sum
            paid_sum = df[df['Payer'] == name]['Total Amount'].sum()
            balances[name] = round(paid_sum - col_sum, 2)

    settlements = get_settlements(balances)
    wa_text = urllib.parse.quote(f"*Trip: {trip_id.replace('_', ' ')}*\n" + "\n".join(settlements))
    
    return render_template('index.html', trip_name=trip_id.replace("_", " "), 
                           participants=participants, expenses=df.to_dict(orient='records'), 
                           balances=balances, totals=totals, settlements=settlements,
                           wa_url=f"https://wa.me/?text={wa_text}",
                           locked=get_config().get(trip_id, {}).get('locked', False))

@app.route('/load_trip/<name>')
def load_trip(name):
    session.clear()
    session['current_trip'] = name
    return redirect(url_for('index'))

@app.route('/save', methods=['POST'])
def save_expense():
    df, exp_id = load_data(), request.form.get('id')
    amount, selected_p = float(request.form.get('amount')), request.form.getlist('split_between')
    share = round(amount / len(selected_p), 2) if selected_p else 0
    data = {'id': exp_id if exp_id else str(uuid.uuid4())[:8], 'Date': request.form.get('date'), 
            'Description': request.form.get('description'), 'Payer': request.form.get('payer'), 'Total Amount': amount}
    for n in session.get('participants', []):
        is_el = n in selected_p
        data[n], data[f"{n}_Eligible"] = (share if is_el else 0.0), ("Yes" if is_el else "No")
    if exp_id and exp_id in df['id'].values:
        for k, v in data.items(): df.loc[df['id'] == exp_id, k] = v
    else:
        df = pd.concat([df, pd.DataFrame([data])], ignore_index=True)
    df.fillna(0).to_csv(get_trip_file(), index=False)
    return redirect(url_for('index'))

@app.route('/download/<type>')
def download(type):
    participants, df = session.get('participants', []), load_data()
    summary = {col: '' for col in df.columns}
    summary['Description'], summary['Total Amount'] = 'TOTALS', df['Total Amount'].sum()
    for p in participants:
        summary[p], summary[f"{p}_Eligible"] = df[p].sum(), "-"
    df_final = pd.concat([df, pd.DataFrame([summary])], ignore_index=True)
    output = BytesIO()
    if type == 'excel':
        df_final.to_excel(output, index=False)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{session['current_trip']}.xlsx")
    else:
        # Dynamic Orientation PDF
        col_count = 4 + (len(participants) * 2)
        page_size = landscape(letter) if col_count > 6 else portrait(letter)
        doc = SimpleDocTemplate(output, pagesize=page_size, margin=0.3*inch)
        elements = [Paragraph(f"<b>Trip Report: {session['current_trip']}</b>", getSampleStyleSheet()['Title'])]
        pdf_df = df_final.drop(columns=['id'])
        data = [pdf_df.columns.values.tolist()] + pdf_df.values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.blue), ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('FONTSIZE', (0,0), (-1,-1), 7 if col_count > 10 else 9)]))
        elements.append(t)
        doc.build(elements)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{session['current_trip']}.pdf")

@app.route('/delete_exp/<id>')
def delete_exp(id):
    df = load_data()
    df[df['id'] != id].to_csv(get_trip_file(), index=False)
    return redirect(url_for('index'))

@app.route('/delete_trip/<name>')
def delete_trip(name):
    file_path = os.path.join(TRIPS_FOLDER, f"expense_{name}.csv")
    if os.path.exists(file_path): os.remove(file_path)
    return redirect(url_for('index'))

@app.route('/new_trip')
def new_trip():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)