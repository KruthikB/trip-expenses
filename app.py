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

os.makedirs(TRIPS_FOLDER, exist_ok=True)

# ---------------- CONFIG HELPERS ---------------- #

def get_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)

def get_groups_config():
    config = get_config()
    trip_id = session.get('current_trip')
    if not trip_id:
        return {}
    return config.get(trip_id, {}).get('groups', {})

@app.route('/manage_groups', methods=['POST'])
def manage_groups():
    group_name = request.form.get('group_name')
    members = request.form.getlist('group_members')

    if not group_name or not members:
        return redirect(url_for('index'))

    groups = get_groups_config()
    groups[group_name] = members
    save_groups_config(groups)

    return redirect(url_for('index'))
def save_groups_config(groups_dict):
    config = get_config()
    trip_id = session.get('current_trip')
    if trip_id not in config:
        config[trip_id] = {}
    config[trip_id]['groups'] = groups_dict
    save_config(config)

# ---------------- DATA HELPERS ---------------- #

def get_trip_file():
    return os.path.join(TRIPS_FOLDER, f"expense_{session.get('current_trip')}.csv")

def load_data():
    path = get_trip_file()
    if not os.path.exists(path):
        df = pd.DataFrame(columns=['id','Date','Description','Payer','Total Amount'])
        df.to_csv(path, index=False)
    return pd.read_csv(path)

# ---------------- GROUP NET SETTLEMENT (UNCHANGED) ---------------- #

def get_settlements(balances):
    EPS = 0.01
    debtors, creditors = [], []

    for name, bal in balances.items():
        bal = round(bal, 2)
        if bal < -EPS:
            debtors.append([name, -bal])
        elif bal > EPS:
            creditors.append([name, bal])

    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)

    settlements = []
    i = j = 0

    while i < len(debtors) and j < len(creditors):
        d, da = debtors[i]
        c, ca = creditors[j]
        pay = round(min(da, ca), 2)

        settlements.append(f"{d} pays {c}: ₹{pay}")
        debtors[i][1] -= pay
        creditors[j][1] -= pay

        if debtors[i][1] < EPS: i += 1
        if creditors[j][1] < EPS: j += 1

    return settlements

# ---------------- ✅ PAYER-WISE + NET PER PAIR ---------------- #

def get_payer_wise_settlements(df, participants):
    raw = {}

    for _, row in df.iterrows():
        payer = row['Payer']
        total = float(row['Total Amount'])

        eligible = [
            p for p in participants
            if f"{p}_Eligible" in df.columns and row[f"{p}_Eligible"] == "Yes"
        ]

        if not eligible:
            continue

        share = round(total / len(eligible), 2)

        for p in eligible:
            if p != payer:
                raw[(p, payer)] = raw.get((p, payer), 0) + share

    net = {}
    visited = set()

    for (a, b), amt in raw.items():
        if (a, b) in visited:
            continue

        reverse = raw.get((b, a), 0)
        diff = round(amt - reverse, 2)

        if diff > 0:
            net[(a, b)] = diff
        elif diff < 0:
            net[(b, a)] = abs(diff)

        visited.add((a, b))
        visited.add((b, a))

    return [f"{x} pays {y}: ₹{amt}" for (x, y), amt in net.items()]


def get_inter_group_settlements_from_individual(settlements, groups):
    """
    settlements: list like ['A pays B: ₹100']
    groups: dict { group_name: [members] }
    """

    # Build member → group lookup
    member_group = {}
    for g, members in groups.items():
        for m in members:
            member_group[m] = g

    raw = {}

    for s in settlements:
        frm, rest = s.split(" pays ")
        to, amt = rest.split(": ₹")
        amt = float(amt)

        g_from = member_group.get(frm)
        g_to = member_group.get(to)

        # Ignore if same group or ungrouped
        if not g_from or not g_to or g_from == g_to:
            continue

        raw[(g_from, g_to)] = raw.get((g_from, g_to), 0) + amt

    # Net opposite group flows
    net = {}
    processed = set()

    for (g1, g2), amt in raw.items():
        if (g1, g2) in processed:
            continue

        reverse = raw.get((g2, g1), 0)
        diff = round(amt - reverse, 2)

        if diff > 0:
            net[(g1, g2)] = diff
        elif diff < 0:
            net[(g2, g1)] = abs(diff)

        processed.add((g1, g2))
        processed.add((g2, g1))

    return [f"{g1} pays {g2}: ₹{amt}" for (g1, g2), amt in net.items()]

# ---------------- ROUTES ---------------- #

@app.route('/', methods=['GET','POST'])
def index():
    if request.method == 'POST' and 'start_trip' in request.form:
        name = request.form.get('trip_name').replace(" ", "_")
        pwd = request.form.get('password')
        cfg = get_config()

        if name in cfg and cfg[name].get('password') and cfg[name]['password'] != pwd:
            return "Wrong password", 403

        session.clear()
        session['current_trip'] = name
        session['participants'] = [
            n.strip() for n in request.form.get('names','').split(',')
            if n.strip()
        ]

        if name not in cfg:
            cfg[name] = {'password': pwd, 'locked': False, 'groups': {}}
            save_config(cfg)

        return redirect(url_for('index'))

    # ---------- SETUP PAGE ----------
    if 'current_trip' not in session:
        trips = [
            f.replace('expense_','').replace('.csv','')
            for f in os.listdir(TRIPS_FOLDER)
            if f.startswith('expense_')
        ]
        return render_template(
            'setup.html',
            existing_trips=trips,
            config=get_config()
        )

    # ---------- LOAD DATA ----------
    df = load_data()
    participants = session.get('participants')

    if not participants:
        participants = [
            c for c in df.columns
            if c not in ['id','Date','Description','Payer','Total Amount']
            and not c.endswith('_Eligible')
        ]
        session['participants'] = participants

    totals = {'Total Amount': df['Total Amount'].sum()}
    balances = {}
    gross_spending = {}
    active_spenders = {}

    # ---------- PER-PERSON CALCULATION ----------
    for p in participants:
        share = df[p].sum() if p in df.columns else 0.0
        paid = df[df['Payer'] == p]['Total Amount'].sum()

        totals[p] = round(share, 2)
        balances[p] = round(paid - share, 2)
        gross_spending[p] = round(paid, 2)

        if paid > 0 or share > 0:
            active_spenders[p] = balances[p]

    # ---------- INDIVIDUAL SETTLEMENTS ----------
    settlements = get_payer_wise_settlements(df, participants)

    # ---------- GROUP LOGIC ----------
    groups = get_groups_config()
    membership = {}

    for members in groups.values():
        for m in members:
            membership[m] = membership.get(m, 0) + 1

    group_stats = {}
    for g, members in groups.items():
        gs = gb = gg = 0
        rows = []

        for m in members:
            if m not in participants:
                continue

            d = membership.get(m, 1)
            es = totals.get(m, 0) / d
            eb = balances.get(m, 0) / d
            eg = gross_spending.get(m, 0) / d

            gs += es
            gb += eb
            gg += eg

            rows.append({
                'name': m,
                'effective_bal': round(eb, 2),
                'effective_gross': round(eg, 2),
                'divisor': d
            })

        group_stats[g] = {
            'total_allocated_share': round(gs, 2),
            'total_gross_paid': round(gg, 2),
            'net_balance': round(gb, 2),
            'members': rows
        }

    # ---------- INTER-GROUP SETTLEMENTS ----------
    group_settlements = get_inter_group_settlements_from_individual(
        settlements,
        groups
    )

    wa = urllib.parse.quote(
        f"*Trip: {session['current_trip'].replace('_',' ')}*\n" +
        "\n".join(settlements)
    )

    return render_template(
        'index.html',
        trip_name=session['current_trip'].replace('_',' '),
        participants=participants,
        expenses=df.to_dict(orient='records'),
        balances=balances,
        gross_spending=gross_spending,
        active_spenders=active_spenders,
        totals=totals,
        settlements=settlements,
        group_stats=group_stats,
        group_settlements=group_settlements,
        grouped_members=[m for g in groups.values() for m in g],
        wa_url=f"https://wa.me/?text={wa}",
        locked=get_config()[session['current_trip']]['locked']
    )


@app.route('/save', methods=['POST'])
def save_expense():
    df = load_data()
    eid = request.form.get('id') or str(uuid.uuid4())[:8]
    amt = float(request.form['amount'])
    split = request.form.getlist('split_between')
    share = round(amt / len(split), 2)

    row = {'id': eid,'Date': request.form['date'],'Description': request.form['description'],'Payer': request.form['payer'],'Total Amount': amt}

    for p in session['participants']:
        row[p] = share if p in split else 0
        row[f"{p}_Eligible"] = "Yes" if p in split else "No"

    df = df[df['id'] != eid]
    df = pd.concat([df, pd.DataFrame([row])])
    df.to_csv(get_trip_file(), index=False)

    return redirect(url_for('index'))

@app.route('/delete_exp/<id>')
def delete_exp(id):
    df = load_data()
    df[df['id'] != id].to_csv(get_trip_file(), index=False)
    return redirect(url_for('index'))

@app.route('/delete_group/<name>')
def delete_group(name):
    g = get_groups_config()
    g.pop(name, None)
    save_groups_config(g)
    return redirect(url_for('index'))

@app.route('/delete_trip/<name>')
def delete_trip(name):
    path = os.path.join(TRIPS_FOLDER, f"expense_{name}.csv")
    if os.path.exists(path):
        os.remove(path)
    return redirect(url_for('index'))

@app.route('/new_trip')
def new_trip():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
