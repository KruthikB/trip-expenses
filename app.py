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

# --- CONFIGURATION HELPERS ---

def get_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f)

def get_groups_config():
    config = get_config()
    trip_id = session.get('current_trip')
    if not trip_id: return {}
    return config.get(trip_id, {}).get('groups', {})

def save_groups_config(groups_dict):
    config = get_config()
    trip_id = session.get('current_trip')
    if trip_id not in config: config[trip_id] = {}
    config[trip_id]['groups'] = groups_dict
    save_config(config)

# --- DATA HELPERS ---

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
    """Calculates who pays whom based on net balances."""
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

# --- ROUTES ---

@app.route('/', methods=['GET', 'POST'])
def index():
    # 1. Start or Load Trip
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
            config[trip_name] = {'password': pwd if pwd else None, 'locked': False, 'groups': {}}
            save_config(config)
            
        session['participants'] = [n.strip() for n in request.form.get('names', '').split(',') if n.strip()]
        return redirect(url_for('index'))

    # 2. Redirect to setup if no trip active
    trip_id = session.get('current_trip')
    if not trip_id:
        files = [f for f in os.listdir(TRIPS_FOLDER) if f.startswith('expense_') and f.endswith('.csv')]
        existing_trips = [f.replace('expense_', '').replace('.csv', '') for f in files]
        return render_template('setup.html', existing_trips=existing_trips, config=get_config())

    # 3. Process Trip Data
    participants, df = session.get('participants', []), load_data()
    if not participants and not df.empty:
        participants = [col for col in df.columns if not col.endswith('_Eligible') and col not in ['id', 'Date', 'Description', 'Payer', 'Total Amount']]
        session['participants'] = participants

    totals = {}
    balances = {name: 0.0 for name in participants}
    gross_spending = {name: 0.0 for name in participants} # Track raw cash paid
    active_spenders = {} 

    if not df.empty:
        totals['Total Amount'] = df['Total Amount'].sum()
        for name in participants:
            # Individual Share (what they were supposed to pay)
            col_sum = df[name].sum() if name in df.columns else 0.0
            totals[name] = col_sum
            
            # Gross Spending (actual cash paid out of pocket)
            paid_sum = df[df['Payer'] == name]['Total Amount'].sum()
            gross_spending[name] = round(paid_sum, 2)
            
            # Net Balance (Paid - Share)
            balances[name] = round(paid_sum - col_sum, 2)
            
            if paid_sum > 0 or col_sum > 0:
                active_spenders[name] = balances[name]

    settlements = get_settlements(balances)

    # 4. Multi-Group Logic (Proportional Split)
    groups = get_groups_config()
    
    # Count memberships per participant for dividing debt
    membership_counts = {}
    for members in groups.values():
        for m in members:
            membership_counts[m] = membership_counts.get(m, 0) + 1

    group_stats = {}
    for group_name, members in groups.items():
        g_spent, g_bal, g_gross_cash = 0, 0, 0 # Added g_gross_cash
        member_breakdown = []

        for m in members:
            divisor = membership_counts.get(m, 1)
            
            # Shares (Supposed to pay)
            eff_spent = totals.get(m, 0) / divisor
            # Balance (Paid - Shares)
            eff_bal = balances.get(m, 0) / divisor
            # Actual Cash Paid (Proportional Gross)
            eff_gross = gross_spending.get(m, 0) / divisor
            
            g_spent += eff_spent
            g_bal += eff_bal
            g_gross_cash += eff_gross
            
            member_breakdown.append({
                'name': m, 
                'effective_bal': round(eff_bal, 2), 
                'effective_gross': round(eff_gross, 2), # Pass gross per member
                'divisor': divisor
            })

        group_stats[group_name] = {
            'total_allocated_share': round(g_spent, 2),
            'total_gross_paid': round(g_gross_cash, 2), # The total cash the group paid
            'net_balance': round(g_bal, 2),
            'members': member_breakdown
        }

    group_balances_only = {gn: stats['net_balance'] for gn, stats in group_stats.items()}
    group_settlements = get_settlements(group_balances_only)
    
    # Flatten list of all grouped members for UI logic
    all_grouped_members = [m for sublist in groups.values() for m in sublist]

    wa_text = urllib.parse.quote(f"*Trip: {trip_id.replace('_', ' ')}*\n" + "\n".join(settlements))
    
    return render_template('index.html', 
                           trip_name=trip_id.replace("_", " "), 
                           participants=participants, 
                           expenses=df.to_dict(orient='records'), 
                           balances=balances, 
                           gross_spending=gross_spending, # New variable passed to HTML
                           active_spenders=active_spenders,
                           totals=totals, 
                           settlements=settlements,
                           group_stats=group_stats,
                           group_settlements=group_settlements,
                           grouped_members=all_grouped_members,
                           wa_url=f"https://wa.me/?text={wa_text}",
                           locked=get_config().get(trip_id, {}).get('locked', False))

@app.route('/manage_groups', methods=['POST'])
def manage_groups():
    group_name = request.form.get('group_name')
    members = request.form.getlist('group_members')
    current_groups = get_groups_config()
    if group_name and members:
        current_groups[group_name] = members
        save_groups_config(current_groups)
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
    
    # 1. Prepare Data
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
        # 2. Dynamic PDF Scaling Logic
        pdf_df = df_final.drop(columns=['id'])
        num_cols = len(pdf_df.columns)
        
        # Decide orientation and font size based on column count
        if num_cols > 10:
            pagesize = landscape(letter)
            base_font_size = 6 if num_cols > 15 else 8
            cell_padding = 2
        elif num_cols > 6:
            pagesize = landscape(letter)
            base_font_size = 9
            cell_padding = 4
        else:
            pagesize = portrait(letter)
            base_font_size = 10
            cell_padding = 6

        doc = SimpleDocTemplate(
            output, 
            pagesize=pagesize, 
            rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20
        )
        
        elements = []
        styles = getSampleStyleSheet()
        elements.append(Paragraph(f"<b>Trip Report: {session['current_trip'].replace('_', ' ')}</b>", styles['Title']))
        elements.append(Spacer(1, 0.2*inch))

        # 3. Build Table Data
        data = [pdf_df.columns.values.tolist()] + pdf_df.values.tolist()
        
        # 4. Apply Table Styling with Scaled Font
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.blue),
            ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke),
            ('ALIGN',(0,0),(-1,-1),'CENTER'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), base_font_size), # Dynamic Font Size
            ('BOTTOMPADDING', (0,0), (-1,0), cell_padding),
            ('TOPPADDING', (0,0), (-1,-1), cell_padding),
            ('GRID', (0,0), (-1,-1), 0.5, colors.black),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ]))

        elements.append(t)
        doc.build(elements)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{session['current_trip']}.pdf")

@app.route('/load_trip/<name>')
def load_trip(name):
    session.clear()
    session['current_trip'] = name
    return redirect(url_for('index'))

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
@app.route('/delete_group/<group_name>')
def delete_group(group_name):
    current_groups = get_groups_config()
    if group_name in current_groups:
        del current_groups[group_name]
        save_groups_config(current_groups)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run()