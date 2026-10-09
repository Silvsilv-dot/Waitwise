"""HIPAA-aware waiting-room app (Patient View + Staff View). SYNTHETIC DATA ONLY.
Connects to Neon Postgres. Every access and change is written to waitroom.audit_log.
Run locally:  streamlit run app.py
"""
import calendar
import datetime as dt
import html
import json
import os
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="Waiting room", page_icon="🏥", layout="wide")


def secret(name, default=None):
    """Read a setting from the environment (.env) or Streamlit's Secrets box. Never hard-code credentials."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return st.secrets[name]
    except Exception:
        return default


DATABASE_URL = secret("DATABASE_URL")
FACILITY = secret("FACILITY_NAME", "Shirley Community Health Center")
PHONE = secret("FRONT_DESK_PHONE", "(555) 010-0100")
STAFF_PIN = secret("STAFF_PIN")
TZ = ZoneInfo("America/New_York")
TZ_LABEL = "EST"          # change to "ET" if you prefer; the zone handles daylight saving automatically
if not DATABASE_URL:
    st.error("DATABASE_URL is missing. On Streamlit Cloud add it under Manage app > Settings > Secrets.")
    st.stop()

ICON = {"scheduled": "🗓️", "checked-in": "🧍", "in-room": "🚪", "completed": "✔️", "cancelled": "✖️", "no_show": "🚫"}
esc = html.escape

st.markdown("""<style>
.big{font-size:3rem;font-weight:700;line-height:1.1}.mid{font-size:1.5rem;font-weight:600}
.lab{opacity:.7;font-size:.95rem;margin-bottom:4px}
.card{border:1px solid rgba(127,127,127,.35);border-radius:14px;padding:18px;background:rgba(127,127,127,.07);height:100%}
.chip{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:700;font-size:1.05rem}
.ok{background:#dff3e8;color:#17744a}.late{background:#fdf0d5;color:#8a5200}.bad{background:#fbe4df;color:#b8321f}
.stButton>button{min-height:48px}
</style>""", unsafe_allow_html=True)


# ---------- database helpers ----------
@contextmanager
def cursor():
    c = psycopg2.connect(DATABASE_URL)  # TLS is required by the Neon connection string (sslmode=require)
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        c.close()


def q(sql, params=None):
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def audit(cur, role, uid, action, table, rid, old=None, new=None):
    j = lambda o: None if o is None else psycopg2.extras.Json(o, dumps=lambda x: json.dumps(x, default=str))
    cur.execute("""INSERT INTO waitroom.audit_log (user_role, user_id, action_type, table_affected, record_id,
                   old_values_json, new_values_json) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (role, uid, action, table, str(rid), j(old), j(new)))


def log_access(role, uid, action, table, rid, note):
    """Log a read of data once per browser session per item (not on every refresh)."""
    key = ("acc", role, uid, action, table, str(rid))
    if key not in st.session_state:
        st.session_state[key] = True
        with cursor() as cur:
            audit(cur, role, uid, action, table, rid, None, {"note": note})


class Conflict(Exception):
    pass


def _check_free(cur, provider_id, appt_id, start, end):
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (provider_id,))   # stops two people grabbing one slot
    cur.execute("""SELECT 1 FROM waitroom.appointments WHERE provider_id=%s AND appointment_id<>%s
                   AND status IN ('scheduled','completed','no_show') AND start_time_est < %s AND end_time_est > %s""",
                (provider_id, appt_id, end, start))
    if cur.fetchone():
        raise Conflict()


def move_appt(aid, start, end, role, uid):
    with cursor() as cur:
        cur.execute("SELECT * FROM waitroom.appointments WHERE appointment_id=%s FOR UPDATE", (aid,))
        old = cur.fetchone()
        _check_free(cur, old["provider_id"], aid, start, end)
        cur.execute("""UPDATE waitroom.appointments SET start_time_est=%s, end_time_est=%s, status='scheduled',
                       updated_at=now() WHERE appointment_id=%s RETURNING *""", (start, end, aid))
        audit(cur, role, uid, "RESCHEDULE", "appointments", aid, old, cur.fetchone())


def set_status(aid, status, role, uid):
    with cursor() as cur:
        cur.execute("SELECT * FROM waitroom.appointments WHERE appointment_id=%s FOR UPDATE", (aid,))
        old = cur.fetchone()
        cur.execute("UPDATE waitroom.appointments SET status=%s, updated_at=now() WHERE appointment_id=%s RETURNING *",
                    (status, aid))
        audit(cur, role, uid, {"cancelled": "CANCEL", "no_show": "NO_SHOW"}.get(status, "UPDATE"),
              "appointments", aid, old, cur.fetchone())


def set_delay(pid, minutes, role, uid):
    with cursor() as cur:
        cur.execute("SELECT * FROM waitroom.provider_delays WHERE provider_id=%s", (pid,))
        old = cur.fetchone()
        cur.execute("""INSERT INTO waitroom.provider_delays (provider_id, delay_minutes) VALUES (%s,%s)
                       ON CONFLICT (provider_id) DO UPDATE SET delay_minutes=EXCLUDED.delay_minutes, updated_at=now()
                       RETURNING *""", (pid, max(0, minutes)))
        audit(cur, role, uid, "UPDATE", "provider_delays", pid, old, cur.fetchone())


def assign_waitlist(wl, start, type_row, role, uid):
    end = start + dt.timedelta(minutes=type_row["duration_minutes"])
    with cursor() as cur:
        _check_free(cur, wl["provider_id"], 0, start, end)
        cur.execute("""INSERT INTO waitroom.appointments (patient_id, provider_id, type_id, start_time_est, end_time_est, status)
                       VALUES (%s,%s,%s,%s,%s,'scheduled') RETURNING *""",
                    (wl["patient_id"], wl["provider_id"], type_row["type_id"], start, end))
        new = cur.fetchone()
        audit(cur, role, uid, "CREATE", "appointments", new["appointment_id"], None, new)
        cur.execute("UPDATE waitroom.waitlist SET status='filled', updated_at=now() WHERE waitlist_id=%s RETURNING *",
                    (wl["waitlist_id"],))
        audit(cur, role, uid, "UPDATE", "waitlist", wl["waitlist_id"], {"status": wl["status"]}, cur.fetchone())


def set_waitlist(wid, status, role, uid):
    with cursor() as cur:
        cur.execute("SELECT * FROM waitroom.waitlist WHERE waitlist_id=%s FOR UPDATE", (wid,))
        old = cur.fetchone()
        cur.execute("UPDATE waitroom.waitlist SET status=%s, updated_at=now() WHERE waitlist_id=%s RETURNING *", (status, wid))
        audit(cur, role, uid, "UPDATE", "waitlist", wid, old, cur.fetchone())


def add_waitlist(pid, prov, d0, d1, role, uid):
    with cursor() as cur:
        cur.execute("""INSERT INTO waitroom.waitlist (patient_id, provider_id, preferred_start_date, preferred_end_date, status)
                       VALUES (%s,%s,%s,%s,'active') RETURNING *""", (pid, prov, d0, d1))
        new = cur.fetchone()
        audit(cur, role, uid, "CREATE", "waitlist", new["waitlist_id"], None, new)


def open_slots(provider_id, d0, d1, minutes):
    """Open start times for a provider between two dates (weekdays, 8:00-5:00, lunch 12-1). 60-min visits need two free cells."""
    if d1 < d0:
        return []
    rows = q("""WITH cells AS (
                  SELECT ((%(d0)s::date + i) + time '08:00' + k * interval '30 minutes') AT TIME ZONE 'America/New_York' AS s
                  FROM generate_series(0, %(n)s) i CROSS JOIN generate_series(0, 17) k
                  WHERE extract(isodow FROM (%(d0)s::date + i)) <= 5 AND k NOT IN (8, 9)),
                busy AS (SELECT start_time_est a, end_time_est b FROM waitroom.appointments
                         WHERE provider_id=%(p)s AND status IN ('scheduled','completed','no_show') AND end_time_est > now()),
                free AS (SELECT s FROM cells c WHERE s > now() + interval '30 minutes'
                         AND NOT EXISTS (SELECT 1 FROM busy WHERE a < c.s + interval '30 minutes' AND b > c.s))
                SELECT s FROM free f
                WHERE %(m)s = 30 OR EXISTS (SELECT 1 FROM free g WHERE g.s = f.s + interval '30 minutes')
                ORDER BY s""", {"d0": d0, "n": (d1 - d0).days, "p": provider_id, "m": minutes})
    return [r["s"].astimezone(TZ) for r in rows if d0 <= r["s"].astimezone(TZ).date() <= d1]


# ---------- time + display helpers (everything shown in US Eastern) ----------
def now_et():
    return dt.datetime.now(TZ)


def t12(x):
    x = x.astimezone(TZ)
    return f"{x.hour % 12 or 12}:{x.minute:02d} {'AM' if x.hour < 12 else 'PM'}"


def d_short(x):
    x = x.astimezone(TZ)
    return f"{x:%a, %b} {x.day}"


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def display_status(a, now):
    if a["status"] != "scheduled":
        return a["status"]
    if now >= a["end_time_est"]:
        return "completed"
    if now >= a["start_time_est"]:
        return "in-room"
    if now >= a["start_time_est"] - dt.timedelta(minutes=15):
        return "checked-in"
    return "scheduled"


def delay_badge(delay):
    if delay < 5:
        return "ok", "✅ On time"
    return ("late" if delay < 20 else "bad"), f"⚠️ Running late · about {delay} min"


@st.fragment(run_every="15s")
def header(role_label):
    c1, c2 = st.columns([3, 2])
    c1.markdown(f"<div class='mid'>🏥 {esc(FACILITY)}</div><div class='lab'>{role_label}</div>", unsafe_allow_html=True)
    n = now_et()
    c2.markdown(f"<div class='mid' style='text-align:right'>🕒 {n:%A, %b} {n.day}, {n.year} - {t12(n)} {TZ_LABEL}</div>",
                unsafe_allow_html=True)


def flash():
    if "msg" in st.session_state:
        kind, text = st.session_state.pop("msg")
        (st.success if kind == "success" else st.error)(text)


# ---------- Patient View ----------
@st.cache_data(ttl=120)
def patient_list():
    return q("SELECT patient_id, first_name, last_name FROM waitroom.patients ORDER BY first_name, last_name")


@st.cache_data(ttl=60)
def default_patient():
    r = q("""SELECT patient_id FROM waitroom.appointments WHERE status='scheduled' AND start_time_est > now()
             ORDER BY start_time_est LIMIT 1""")
    return r[0]["patient_id"] if r else None


@st.fragment(run_every="10s")
def patient_view():
    flash()
    pts = {p["patient_id"]: f"{p['first_name']} {p['last_name'][0]}. · ID {p['patient_id']}" for p in patient_list()}
    ids = list(pts)
    d = default_patient()
    pid = st.selectbox("👤 Demo patient (synthetic)", ids, index=ids.index(d) if d in ids else 0, format_func=pts.get)
    me = next(p for p in patient_list() if p["patient_id"] == pid)
    uid = f"patient:{pid}"
    log_access("patient", uid, "VIEW", "appointments", pid, "viewed own appointment")

    a = q("""SELECT a.*, p.name AS provider, p.specialty, t.name AS type_name, t.duration_minutes
             FROM waitroom.appointments a JOIN waitroom.providers p ON p.provider_id = a.provider_id
             JOIN waitroom.appointment_types t ON t.type_id = a.type_id
             WHERE a.patient_id=%s AND a.status='scheduled' AND a.end_time_est > now()
             ORDER BY a.start_time_est LIMIT 1""", (pid,))
    st.markdown(f"## Hello, {esc(me['first_name'])} {esc(me['last_name'][0])}.")
    if not a:
        st.info("You have no upcoming appointments. Please call the front desk to book one: "
                f"📞 {esc(PHONE)}")
        return
    a = a[0]
    now = now_et()
    start = a["start_time_est"].astimezone(TZ)
    delay = (q("SELECT delay_minutes FROM waitroom.provider_delays WHERE provider_id=%s", (a["provider_id"],)) or [{"delay_minutes": 0}])[0]["delay_minutes"]
    today = start.date() == now.date()
    ahead = q("""SELECT count(*) c FROM waitroom.appointments WHERE provider_id=%s AND status='scheduled'
                 AND start_time_est < %s AND end_time_est > now()
                 AND (start_time_est AT TIME ZONE 'America/New_York')::date = (now() AT TIME ZONE 'America/New_York')::date""",
              (a["provider_id"], a["start_time_est"]))[0]["c"] if today else None
    cls, label = delay_badge(delay if today else 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"<div class='card'><div class='lab'>🕒 Your appointment</div><div class='big'>{t12(start)}</div>"
                f"<div>{d_short(start)} · {TZ_LABEL}</div></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='card'><div class='lab'>🩺 Provider</div><div class='mid'>{esc(a['provider'])}</div>"
                f"<div>{esc(a['specialty'])} · {esc(a['type_name'])}</div></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card'><div class='lab'>🧍 Patients ahead of you</div>"
                f"<div class='big'>{ahead if ahead is not None else '—'}</div></div>", unsafe_allow_html=True)
    exp = f"<div>Expected start about {t12(start + dt.timedelta(minutes=delay))}</div>" if today and delay >= 5 else ""
    c4.markdown(f"<div class='card'><div class='lab'>Provider status</div><span class='chip {cls}'>{label}</span>{exp}</div>",
                unsafe_allow_html=True)

    st.markdown("### 📅 Reschedule")
    st.markdown(f"<div class='mid'>📞 Front desk: {esc(PHONE)}</div>", unsafe_allow_html=True)
    today_d = now.date()
    limit = add_months(today_d, 6)
    months = [add_months(today_d.replace(day=1), i) for i in range(12)]
    ms = st.selectbox("Month", months, format_func=lambda m: f"{calendar.month_name[m.month]} {m.year}", key="rs_month")
    if ms > limit:
        st.warning(f"To reschedule more than 6 months in advance, please call the front desk at {PHONE}.")
        return
    mend = ms.replace(day=calendar.monthrange(ms.year, ms.month)[1])
    slots = open_slots(a["provider_id"], max(ms, today_d), min(mend, limit), a["duration_minutes"])
    by_day = {}
    for s in slots:
        by_day.setdefault(s.date(), []).append(s)
    for col, nme in zip(st.columns(7), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        col.caption(nme)
    for week in calendar.Calendar(0).monthdatescalendar(ms.year, ms.month):
        for col, day in zip(st.columns(7), week):
            if day.month != ms.month:
                continue
            n = len(by_day.get(day, []))
            if col.button(f"{day.day}\n{n} open" if n else f"{day.day}\n–", key=f"rd{day}", disabled=not n,
                          width="stretch"):
                st.session_state.rs_day = day
                st.session_state.pop("rs_pick", None)
    sel = st.session_state.get("rs_day")
    if sel in by_day:
        st.markdown(f"**Open times on {d_short(dt.datetime.combine(sel, dt.time(), TZ))}** (available)")
        for row in range(0, len(by_day[sel]), 6):
            for col, s in zip(st.columns(6), by_day[sel][row:row + 6]):
                if col.button(f"🟢 {t12(s)}", key=f"rs{s.isoformat()}"):
                    st.session_state.rs_pick = s.isoformat()
    pick = st.session_state.get("rs_pick")
    if pick:
        s = dt.datetime.fromisoformat(pick)
        st.markdown(f"New time: **{d_short(s)} at {t12(s)} {TZ_LABEL}** with {esc(a['provider'])}")
        if st.button("✅ Confirm reschedule", type="primary"):
            try:
                move_appt(a["appointment_id"], s, s + dt.timedelta(minutes=a["duration_minutes"]), "patient", uid)
                st.session_state.msg = ("success", f"Done! Your appointment is now {d_short(s)} at {t12(s)} {TZ_LABEL}.")
            except Conflict:
                st.session_state.msg = ("error", "Sorry, that time was just taken. Please pick another.")
            st.session_state.pop("rs_pick", None)
            st.rerun()


# ---------- Staff View ----------
@st.fragment(run_every="10s")
def staff_view():
    role, uid = "staff", "staff:demo"
    flash()
    now = now_et()
    provs = q("SELECT p.*, coalesce(d.delay_minutes,0) AS delay FROM waitroom.providers p "
              "LEFT JOIN waitroom.provider_delays d USING (provider_id) WHERE p.is_active ORDER BY p.provider_id")
    day_choice = st.radio("Day", ["Today", "Tomorrow"], horizontal=True)
    day = now.date() + dt.timedelta(days=0 if day_choice == "Today" else 1)
    d0 = dt.datetime.combine(day, dt.time(), TZ)
    log_access(role, uid, "VIEW", "appointments", str(day), "viewed schedule for the day")
    rows = q("""SELECT a.*, p.first_name, p.last_name, t.name AS type_name FROM waitroom.appointments a
                JOIN waitroom.patients p ON p.patient_id = a.patient_id
                JOIN waitroom.appointment_types t ON t.type_id = a.type_id
                WHERE a.start_time_est >= %s AND a.start_time_est < %s AND a.status <> 'cancelled'
                ORDER BY a.start_time_est""", (d0, d0 + dt.timedelta(days=1)))

    st.markdown("### 🩺 Providers")
    for col, p in zip(st.columns(len(provs)), provs):
        mine = [r for r in rows if r["provider_id"] == p["provider_id"] and r["status"] == "scheduled"]
        busy = [r for r in mine if r["start_time_est"] - dt.timedelta(minutes=60) <= now < r["end_time_est"]]
        if p["delay"] >= 5:
            cls, label = ("late" if p["delay"] < 20 else "bad"), f"⏰ Delay · {p['delay']} min"
        elif not busy:
            cls, label = "ok", "🟢 Available"
        else:
            cls, label = "ok", "✅ On time"
        col.markdown(f"<div class='card'><div class='mid'>{esc(p['name'])}</div><div class='lab'>{esc(p['specialty'])}</div>"
                     f"<span class='chip {cls}'>{label}</span></div>", unsafe_allow_html=True)
        b1, b2, b3 = col.columns(3)
        for b, (txt, new) in zip((b1, b2, b3), (("+5", p["delay"] + 5), ("+15", p["delay"] + 15), ("Clear", 0))):
            if b.button(txt, key=f"dl{p['provider_id']}{txt}", help="Change this provider's delay"):
                set_delay(p["provider_id"], new, role, uid)
                st.session_state.msg = ("success", f"{p['name']}: delay now {max(0, new)} min.")
                st.rerun()

    st.markdown(f"### 🗓️ Time slots · {d_short(d0)}")
    cells = [d0 + dt.timedelta(hours=8, minutes=30 * k) for k in range(18)]
    grid = []
    for k, c in enumerate(cells):
        row = {"Time": t12(c)}
        for p in provs:
            if k in (8, 9):
                row[p["name"]] = "🍽️ Lunch"
                continue
            m = [r for r in rows if r["provider_id"] == p["provider_id"] and r["start_time_est"] <= c < r["end_time_est"]]
            if not m:
                row[p["name"]] = "Available"
            else:
                r = m[0]
                if r["start_time_est"] == c:
                    ds = display_status(r, now)
                    row[p["name"]] = f"{ICON[ds]} {r['first_name']} {r['last_name']} · {r['type_name']} · {ds}"
                else:
                    row[p["name"]] = "↳ (continues)"
        grid.append(row)
    st.dataframe(pd.DataFrame(grid), hide_index=True, width="stretch", height=660)

    st.markdown("### ✏️ Appointment actions")
    who = st.radio("Provider", [p["provider_id"] for p in provs], format_func={p["provider_id"]: p["name"] for p in provs}.get,
                   horizontal=True)
    for r in [r for r in rows if r["provider_id"] == who]:
        ds = display_status(r, now)
        c = st.columns([4, 1, 1])
        c[0].write(f"**{t12(r['start_time_est'])}** · {r['first_name']} {r['last_name']} · {r['type_name']} · {ICON[ds]} {ds}")
        if r["status"] == "scheduled":
            if c[1].button("Cancel", key=f"x{r['appointment_id']}", disabled=now >= r["end_time_est"]):
                set_status(r["appointment_id"], "cancelled", role, uid)
                st.session_state.msg = ("success", f"Cancelled {r['first_name']} {r['last_name']}. The slot is open again.")
                st.rerun()
            if c[2].button("No show", key=f"n{r['appointment_id']}"):
                set_status(r["appointment_id"], "no_show", role, uid)
                st.session_state.msg = ("success", f"Marked {r['first_name']} {r['last_name']} as a no-show.")
                st.rerun()

    st.markdown("### 🔎 Find a patient")
    term = st.text_input("First or last name", placeholder="e.g. Alvarez").strip()
    if len(term) >= 2:
        log_access(role, uid, "SEARCH", "patients", term.lower(), "name search")
        found = q("""SELECT a.start_time_est, a.status, pr.name AS provider, p.first_name, p.last_name
                     FROM waitroom.appointments a JOIN waitroom.patients p ON p.patient_id=a.patient_id
                     JOIN waitroom.providers pr ON pr.provider_id=a.provider_id
                     WHERE (p.first_name ILIKE %s OR p.last_name ILIKE %s) AND a.status='scheduled' AND a.end_time_est > now()
                     ORDER BY a.start_time_est LIMIT 20""", (f"%{term}%", f"%{term}%"))
        if found:
            st.dataframe(pd.DataFrame([{"Patient": f"{f['first_name']} {f['last_name']}", "Provider": f["provider"],
                                        "When": f"{d_short(f['start_time_est'])} {t12(f['start_time_est'])}", "Status": f["status"]}
                                       for f in found]), hide_index=True, width="stretch")
        else:
            st.caption("No upcoming appointments found.")

    st.markdown("### ⏳ Waitlist")
    types = q("SELECT * FROM waitroom.appointment_types ORDER BY duration_minutes")
    vt = st.radio("Offer slots for", [t["type_id"] for t in types], horizontal=True,
                  format_func={t["type_id"]: f"{t['name']} ({t['duration_minutes']} min)" for t in types}.get)
    trow = next(t for t in types if t["type_id"] == vt)
    wls = q("""SELECT w.*, p.first_name, p.last_name, pr.name AS provider FROM waitroom.waitlist w
               JOIN waitroom.patients p ON p.patient_id=w.patient_id JOIN waitroom.providers pr ON pr.provider_id=w.provider_id
               WHERE w.status='active' ORDER BY w.created_at""")
    if not wls:
        st.caption("No one is on the waitlist.")
    for w in wls:
        st.markdown(f"**{w['first_name']} {w['last_name']}** · {w['provider']} · wants {w['preferred_start_date']:%b} "
                    f"{w['preferred_start_date'].day} to {w['preferred_end_date']:%b} {w['preferred_end_date'].day}")
        offers = open_slots(w["provider_id"], max(now.date(), w["preferred_start_date"]), w["preferred_end_date"],
                            trow["duration_minutes"])[:4]
        cols = st.columns(6)
        for col, s in zip(cols, offers):
            if col.button(f"Assign {d_short(s)} {t12(s)}", key=f"as{w['waitlist_id']}{s.isoformat()}"):
                try:
                    assign_waitlist(w, s, trow, role, uid)
                    st.session_state.msg = ("success", f"Booked {w['first_name']} {w['last_name']} on {d_short(s)} at {t12(s)}.")
                except Conflict:
                    st.session_state.msg = ("error", "That slot was just taken.")
                st.rerun()
        if not offers:
            st.caption("No open slots in this patient's date range.")
        if cols[4].button("Mark filled", key=f"fl{w['waitlist_id']}"):
            set_waitlist(w["waitlist_id"], "filled", role, uid)
            st.rerun()
        if cols[5].button("Expire", key=f"ex{w['waitlist_id']}"):
            set_waitlist(w["waitlist_id"], "expired", role, uid)
            st.rerun()
    with st.expander("➕ Add someone to the waitlist"):
        allp = {p["patient_id"]: f"{p['first_name']} {p['last_name']}" for p in patient_list()}
        wp = st.selectbox("Patient", list(allp), format_func=allp.get, key="wp")
        wv = st.selectbox("Provider", [p["provider_id"] for p in provs], format_func={p["provider_id"]: p["name"] for p in provs}.get, key="wv")
        r1, r2 = st.columns(2)
        ws = r1.date_input("From", now.date(), key="ws")
        we = r2.date_input("To", now.date() + dt.timedelta(days=14), key="we")
        if st.button("Add to waitlist"):
            add_waitlist(wp, wv, ws, we, role, uid)
            st.session_state.msg = ("success", f"{allp[wp]} added to the waitlist.")
            st.rerun()

    with st.expander("🧾 Recent audit log"):
        log = q("""SELECT timestamp_est, user_role, user_id, action_type, table_affected, record_id
                   FROM waitroom.audit_log ORDER BY audit_id DESC LIMIT 15""")
        st.dataframe(pd.DataFrame([{**l, "timestamp_est": f"{d_short(l['timestamp_est'])} {t12(l['timestamp_est'])} {TZ_LABEL}"}
                                   for l in log]), hide_index=True, width="stretch")


# ---------- page ----------
st.sidebar.markdown("### 🏥 Waiting room")
view = st.sidebar.radio("View", ["Patient View", "Staff View"])
st.sidebar.caption("Demo uses synthetic data only. Never enter real patient information.")

if view == "Patient View":
    header("📋 Patient View")
    patient_view()
else:
    header("🔒 Staff View · internal use")
    if STAFF_PIN and not st.session_state.get("staff_ok"):
        pin = st.text_input("Staff PIN", type="password")
        if st.button("Unlock"):
            st.session_state.staff_ok = pin == STAFF_PIN
            st.rerun()
        if "staff_ok" in st.session_state and not st.session_state.staff_ok:
            st.error("Incorrect PIN.")
    else:
        if not STAFF_PIN:
            st.warning("Demo mode: no STAFF_PIN is set, so anyone can open this view. Add STAFF_PIN in Secrets to require a PIN.")
        staff_view()
