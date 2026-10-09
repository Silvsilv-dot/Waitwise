"""Waitwise - patient flow & scheduling, wired to the existing Neon `waitroom` schema (synthetic data).

Required secret:  DATABASE_URL (Neon connection string). No logins, no passwords.
pip install streamlit sqlalchemy psycopg2-binary pandas tzdata
"""
import calendar
import html
import json
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Waitwise", page_icon=":material/local_hospital:", layout="wide")

FACILITY = "Riverside Community Health Center"
FRONT_DESK = "(201) 555-0142"  # fictional number
SCH = "waitroom"

ET = ZoneInfo("America/New_York")  # all app times shown in US Eastern
OPEN_H, CLOSE_H = 8, 17
LUNCH = (12, 13)


# ====================================================================== scheduling logic
def at(day, h, m=0):
    return datetime.combine(day, time(h, m), tzinfo=ET)


def is_workday(d):
    return d.weekday() < 5


def six_months_out(today):
    month = today.month + 6
    year = today.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def cells(day):
    cur, close = at(day, OPEN_H), at(day, CLOSE_H)
    while cur < close:
        yield cur
        cur += timedelta(minutes=30)


def in_lunch(s, e):
    return s < at(s.date(), LUNCH[1]) and e > at(s.date(), LUNCH[0])


def free_starts(day, busy, duration, now):
    out, close = [], at(day, CLOSE_H)
    for s in cells(day):
        e = s + timedelta(minutes=duration)
        if e > close or in_lunch(s, e) or s <= now:
            continue
        if any(s < be and e > bs for bs, be in busy):
            continue
        out.append(s)
    return out


def time_state(start, end, now):
    return "past" if end <= now else ("now" if start <= now else "upcoming")


def day_rows(day, appts, now):
    by_start = {a["start"]: a for a in appts}
    rows, cur, close = [], at(day, OPEN_H), at(day, CLOSE_H)
    while cur < close:
        a = by_start.get(cur)
        if a:
            rows.append({"kind": "appt", "start": a["start"], "end": a["end"], "appt": a,
                         "tstate": time_state(a["start"], a["end"], now)})
            cur = a["end"]
            continue
        nxt = cur + timedelta(minutes=30)
        kind = "lunch" if LUNCH[0] <= cur.hour < LUNCH[1] else "open"
        rows.append({"kind": kind, "start": cur, "end": nxt, "tstate": time_state(cur, nxt, now)})
        cur = nxt
    return rows


def fmt_time(dt):
    return dt.astimezone(ET).strftime("%I:%M %p").lstrip("0")


def fmt_date(dt):
    return dt.strftime("%a, %b %d").replace(" 0", " ")


def now_et():
    return datetime.now(ET)


def esc(x):
    return html.escape(str(x))


# ====================================================================== database (Neon)
missing = []
try:
    missing = [k for k in ("DATABASE_URL",) if k not in st.secrets]
except Exception:
    missing = ["DATABASE_URL"]
if missing:
    st.error(f"Missing secrets: {', '.join(missing)}. Add them in .streamlit/secrets.toml or Streamlit Cloud > Settings > Secrets.")
    st.stop()


@st.cache_resource
def engine():
    url = st.secrets["DATABASE_URL"]
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    if "sslmode" not in url:  # always encrypt in transit
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return create_engine(url, pool_pre_ping=True, pool_recycle=240, pool_size=5,
                         connect_args={"application_name": "waitwise-app"})


def query(sql, **params):
    with engine().connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def _audit(conn, role, user_id, action, table, record_id=None, old=None, new=None):
    conn.execute(text(
        f"INSERT INTO {SCH}.audit_log(user_role,user_id,action_type,table_affected,record_id,old_values_json,new_values_json) "
        "VALUES(:r,:u,:a,:t,:i,CAST(:o AS jsonb),CAST(:n AS jsonb))"),
        dict(r=role, u=str(user_id), a=action, t=table, i=None if record_id is None else str(record_id),
             o=None if old is None else json.dumps(old, default=str),
             n=None if new is None else json.dumps(new, default=str)))


def audit(role, user_id, action, table="session", record_id=None, old=None, new=None):
    with engine().begin() as c:
        _audit(c, role, user_id, action, table, record_id, old, new)


@st.cache_data(ttl=300, show_spinner=False)
def types():
    return {r["type_id"]: {"name": r["name"], "min": r["duration_minutes"]}
            for r in query(f"SELECT * FROM {SCH}.appointment_types")}


def tlabel(type_id):
    return types()[type_id]["name"]


def tdur(type_id):
    return types()[type_id]["min"]


@st.cache_data(ttl=30, show_spinner=False)
def patient_map():
    return {r["patient_id"]: r for r in query(f"SELECT * FROM {SCH}.patients")}


def pname(pid, full=True):
    p = patient_map().get(pid)
    if not p:
        return "Unknown"
    return f"{p['first_name']} {p['last_name']}" if full else p["first_name"]


def providers():
    return query(f"SELECT p.provider_id, p.name, p.specialty, COALESCE(d.delay_minutes,0) AS delay_min "
                 f"FROM {SCH}.providers p LEFT JOIN {SCH}.provider_delays d ON d.provider_id=p.provider_id "
                 f"WHERE p.is_active ORDER BY p.provider_id")


def _ann(rows):
    for r in rows:
        r["start"], r["end"] = r["start_time_est"].astimezone(ET), r["end_time_est"].astimezone(ET)
    return rows


APPT_SELECT = (f"SELECT a.*, pr.name AS provider_name, t.name AS type_name, t.duration_minutes FROM {SCH}.appointments a "
               f"JOIN {SCH}.providers pr ON pr.provider_id=a.provider_id JOIN {SCH}.appointment_types t ON t.type_id=a.type_id ")


def appts_between(start, end, provider_id=None, include_cancelled=False):
    sql = APPT_SELECT + "WHERE a.start_time_est >= :s AND a.start_time_est < :e"
    params = dict(s=start, e=end)
    if provider_id:
        sql += " AND a.provider_id=:p"
        params["p"] = provider_id
    if not include_cancelled:
        sql += " AND a.status <> 'cancelled'"
    return _ann(query(sql + " ORDER BY a.start_time_est", **params))


def patient_appts(pid):
    return _ann(query(APPT_SELECT + "WHERE a.patient_id=:p ORDER BY a.start_time_est DESC LIMIT 40", p=pid))


def next_appt(pid):
    rows = _ann(query(
        f"SELECT a.*, pr.name AS provider_name, t.name AS type_name, t.duration_minutes, COALESCE(d.delay_minutes,0) AS delay_min "
        f"FROM {SCH}.appointments a JOIN {SCH}.providers pr ON pr.provider_id=a.provider_id "
        f"JOIN {SCH}.appointment_types t ON t.type_id=a.type_id "
        f"LEFT JOIN {SCH}.provider_delays d ON d.provider_id=a.provider_id "
        f"WHERE a.patient_id=:p AND a.status='scheduled' AND a.end_time_est > now() ORDER BY a.start_time_est LIMIT 1", p=pid))
    return rows[0] if rows else None


def patients_ahead(appt, now):
    if appt["start"].date() != now.date():
        return None
    rows = appts_between(at(now.date(), 0), at(now.date() + timedelta(days=1), 0), appt["provider_id"])
    return sum(1 for a in rows if a["status"] == "scheduled" and a["start"] < appt["start"] and a["end"] > now)


@st.cache_data(ttl=5, show_spinner=False)
def open_slots(duration, provider_id=None):
    now = now_et()
    today, last = now.date(), six_months_out(now.date())
    sql = (f"SELECT provider_id,start_time_est,end_time_est FROM {SCH}.appointments WHERE status<>'cancelled' "
           "AND end_time_est > :s AND start_time_est < :e")
    params = dict(s=at(today, 0), e=at(last + timedelta(days=1), 0))
    if provider_id:
        sql += " AND provider_id=:p"
        params["p"] = provider_id
    busy = defaultdict(list)
    for r in query(sql, **params):
        s = r["start_time_est"].astimezone(ET)
        busy[(r["provider_id"], s.date())].append((s, r["end_time_est"].astimezone(ET)))
    pids = [provider_id] if provider_id else [p["provider_id"] for p in providers()]
    out, d = [], today
    while d <= last:
        if is_workday(d):
            for pid in pids:
                out += [{"provider_id": pid, "start": s} for s in free_starts(d, busy[(pid, d)], duration, now)]
        d += timedelta(days=1)
    out.sort(key=lambda x: (x["start"], x["provider_id"]))
    return out


def waitlist_rows():
    rows = query(f"SELECT w.*, pr.name AS provider_name FROM {SCH}.waitlist w JOIN {SCH}.providers pr ON pr.provider_id=w.provider_id "
                 f"WHERE w.status='active' ORDER BY w.created_at")
    for r in rows:
        r["created"] = r["created_at"].astimezone(ET)
    return rows


def audit_rows(limit=200):
    rows = query(f"SELECT * FROM {SCH}.audit_log ORDER BY audit_id DESC LIMIT :n", n=limit)
    for r in rows:
        r["ts_et"] = r["timestamp_est"].astimezone(ET)
    return rows


def _conflict(c, provider_id, start, end, exclude_id=None):
    c.execute(text("SELECT pg_advisory_xact_lock(:p)"), dict(p=provider_id))  # serialize bookings per provider
    sql = (f"SELECT 1 FROM {SCH}.appointments WHERE provider_id=:p AND status<>'cancelled' "
           "AND start_time_est < :e AND end_time_est > :s")
    params = dict(p=provider_id, s=start, e=end)
    if exclude_id:
        sql += " AND appointment_id<>:x"
        params["x"] = exclude_id
    return c.execute(text(sql + " LIMIT 1"), params).first() is not None


def book(patient_id, provider_id, start, type_id, role, user_id, waitlist_id=None):
    end = start + timedelta(minutes=tdur(type_id))
    with engine().begin() as c:
        if _conflict(c, provider_id, start, end):
            return False
        aid = c.execute(text(
            f"INSERT INTO {SCH}.appointments(patient_id,provider_id,type_id,start_time_est,end_time_est,status) "
            "VALUES(:pa,:pr,:t,:s,:e,'scheduled') RETURNING appointment_id"),
            dict(pa=patient_id, pr=provider_id, t=type_id, s=start, e=end)).scalar()
        _audit(c, role, user_id, "BOOK_FROM_WAITLIST" if waitlist_id else "BOOK", "appointments", aid, None,
               dict(patient_id=patient_id, provider_id=provider_id, type_id=type_id, start=start.isoformat(), end=end.isoformat()))
        if waitlist_id:
            c.execute(text(f"UPDATE {SCH}.waitlist SET status='filled', updated_at=now() WHERE waitlist_id=:i"), dict(i=waitlist_id))
            _audit(c, role, user_id, "WAITLIST_FILLED", "waitlist", waitlist_id, {"status": "active"}, {"status": "filled"})
    st.cache_data.clear()
    return True


def reschedule(appt_id, provider_id, start, role, user_id):
    with engine().begin() as c:
        old = c.execute(text(f"SELECT provider_id,start_time_est,end_time_est,type_id FROM {SCH}.appointments "
                             "WHERE appointment_id=:i AND status='scheduled' FOR UPDATE"), dict(i=appt_id)).mappings().first()
        if not old:
            return False
        end = start + timedelta(minutes=tdur(old["type_id"]))
        if _conflict(c, provider_id, start, end, exclude_id=appt_id):
            return False
        c.execute(text(f"UPDATE {SCH}.appointments SET provider_id=:p,start_time_est=:s,end_time_est=:e,updated_at=now() "
                       "WHERE appointment_id=:i"), dict(p=provider_id, s=start, e=end, i=appt_id))
        _audit(c, role, user_id, "RESCHEDULE", "appointments", appt_id,
               dict(provider_id=old["provider_id"], start=old["start_time_est"].isoformat(), end=old["end_time_est"].isoformat()),
               dict(provider_id=provider_id, start=start.isoformat(), end=end.isoformat()))
    st.cache_data.clear()
    return True


def set_status(appt_id, status, role, user_id):
    with engine().begin() as c:
        old = c.execute(text(f"SELECT status FROM {SCH}.appointments WHERE appointment_id=:i"), dict(i=appt_id)).scalar()
        c.execute(text(f"UPDATE {SCH}.appointments SET status=:s, updated_at=now() WHERE appointment_id=:i"), dict(s=status, i=appt_id))
        _audit(c, role, user_id, status.upper(), "appointments", appt_id, {"status": old}, {"status": status})
    st.cache_data.clear()


def set_delay(provider_id, minutes, role, user_id):
    with engine().begin() as c:
        old = c.execute(text(f"SELECT delay_minutes FROM {SCH}.provider_delays WHERE provider_id=:i"), dict(i=provider_id)).scalar()
        c.execute(text(f"INSERT INTO {SCH}.provider_delays(provider_id,delay_minutes,updated_at) VALUES(:i,:m,now()) "
                       "ON CONFLICT (provider_id) DO UPDATE SET delay_minutes=EXCLUDED.delay_minutes, updated_at=now()"),
                  dict(i=provider_id, m=minutes))
        _audit(c, role, user_id, "SET_DELAY", "provider_delays", provider_id, {"delay_minutes": old}, {"delay_minutes": minutes})


def waitlist_expire(wl_id, role, user_id):
    with engine().begin() as c:
        c.execute(text(f"UPDATE {SCH}.waitlist SET status='expired', updated_at=now() WHERE waitlist_id=:i"), dict(i=wl_id))
        _audit(c, role, user_id, "WAITLIST_REMOVED", "waitlist", wl_id, {"status": "active"}, {"status": "expired"})


# ====================================================================== styling
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded');
.stApp{background:#f4f8fa}
.block-container{padding-top:1.2rem;max-width:1250px}
.msr{font-family:'Material Symbols Rounded';font-weight:normal;font-style:normal;font-size:1.4em;vertical-align:middle;
     line-height:1;letter-spacing:normal;text-transform:none;display:inline-block;font-feature-settings:'liga'}
.hdr{display:flex;justify-content:space-between;align-items:center;color:#fff;border-radius:18px;
     padding:1rem 1.6rem;background:linear-gradient(95deg,#0b6e8a,#14a0a6);margin-bottom:1rem}
.hdr .fac{font-size:1.5rem;font-weight:700}.hdr .sub{opacity:.85;font-size:.9rem}
.hdr .msr{font-size:2.4rem;margin-right:.8rem}.hdr-l{display:flex;align-items:center}
.hdr-r{text-align:right}.hdr .time{font-size:1.9rem;font-weight:700;font-variant-numeric:tabular-nums}
.hdr .date{opacity:.9}
.card{background:#fff;border-radius:16px;padding:1.1rem 1.3rem;box-shadow:0 1px 5px rgba(20,60,80,.09);height:100%}
.label{color:#5b6b75;font-size:.92rem;display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem}
.label .msr{color:#0b6e8a}
.big{font-size:2.7rem;font-weight:750;color:#0b6e8a;line-height:1.1;font-variant-numeric:tabular-nums}
.mid{font-size:1.5rem;font-weight:650;color:#17323d}
.pill{display:inline-block;padding:.2rem .8rem;border-radius:999px;font-weight:650;font-size:.9rem}
.ontime{background:#dff5e8;color:#157a45}.delay{background:#fdecc8;color:#9a5b00}
.avail{background:#dcecfb;color:#1a62a8}.muted{color:#6b7c86;font-size:.92rem}
.pname{font-size:1.15rem;font-weight:700;color:#17323d}
.slot-open{color:#1a62a8;font-weight:600}.slot-booked{color:#17323d;font-weight:600}
div.stButton>button{border-radius:10px}
.st-key-cal [data-testid="stColumn"]{min-width:0 !important}
.st-key-cal [data-testid="stHorizontalBlock"]{gap:.35rem}
.st-key-cal button{min-height:64px;padding:.2rem .1rem;line-height:1.2;white-space:normal;
     border-radius:12px;border:1px solid rgba(128,128,128,.25);overflow:visible}
.st-key-cal button p{margin:0;font-size:.82rem;text-align:center;white-space:normal;overflow:visible;text-overflow:clip}
.st-key-cal button p strong{font-size:1.1rem;display:block}
.st-key-cal button:disabled{opacity:.35;background:transparent}
.st-key-times button{min-height:42px;border-radius:10px}
.legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.85rem;color:#6b7c86;margin-top:.4rem}
.legend i{display:inline-block;width:.8rem;height:.8rem;border-radius:4px;margin-right:.35rem;vertical-align:-1px}
.dayhdr{text-align:center;font-weight:700;color:#5b6b75;font-size:.85rem}
.monthttl{text-align:center;font-size:1.4rem;font-weight:700;color:#17323d;padding-top:.3rem}
.rowtxt{line-height:1.3}.rowtxt .sm{color:#6b7c86;font-size:.85rem}
</style>
""", unsafe_allow_html=True)

ss = st.session_state

try:
    types()
except Exception as e:
    st.error("Could not read the `waitroom` schema in Neon. Check DATABASE_URL and that the setup SQL has been run.")
    st.caption(type(e).__name__)
    st.stop()



@st.fragment(run_every=1)
def header():
    n = now_et()
    clock = n.strftime("%I:%M:%S %p").lstrip("0") + " " + n.strftime("%Z")
    st.markdown(
        f'<div class="hdr"><div class="hdr-l"><span class="msr">local_hospital</span><div>'
        f'<div class="fac">{esc(FACILITY)}</div><div class="sub">Waitwise · live patient flow</div></div></div>'
        f'<div class="hdr-r"><div class="time">{clock}</div><div class="date">{n.strftime("%A, %B %d, %Y")} · Eastern Time</div></div></div>',
        unsafe_allow_html=True)


def stat(icon, label, value, big=True):
    return (f'<div class="card"><div class="label"><span class="msr">{icon}</span>{esc(label)}</div>'
            f'<div class="{"big" if big else "mid"}">{value}</div></div>')


def status_of(prov, appts_today):
    if not appts_today:
        return "avail", "Available"
    if prov["delay_min"] > 0:
        return "delay", f"Delay · {prov['delay_min']} min"
    return "ontime", "On time"


# ====================================================================== PATIENT VIEW
def patient_picker():
    st.subheader(":material/person: Who is checking in?")
    pats = sorted(patient_map().items(), key=lambda kv: (kv[1]["last_name"], kv[1]["first_name"]))
    pid = st.selectbox("Select a patient", [k for k, _ in pats], index=None, format_func=lambda i: pname(i),
                       placeholder="Choose a patient (synthetic data)", key="patient_pick")
    return pid


@st.fragment(run_every=10)
def patient_status(pid):
    appt, now = next_appt(pid), now_et()
    first = pname(pid, full=False)
    if not appt:
        st.info(f"Hello {first}, you have no upcoming appointment. Please call the front desk at {FRONT_DESK} to book.")
        return
    ahead, delay = patients_ahead(appt, now), appt["delay_min"]
    status = ('<span class="pill ontime">On time</span>' if delay == 0
              else f'<span class="pill delay">Running {delay} min late</span>')
    st.markdown(f"## :material/waving_hand: Hello, {esc(first)}")
    c = st.columns(4)
    c[0].markdown(stat("event", "Appointment", f"{fmt_time(appt['start'])}<div class='muted'>{fmt_date(appt['start'])} · {esc(appt['type_name'])}</div>"), unsafe_allow_html=True)
    c[1].markdown(stat("stethoscope", "Provider", esc(appt["provider_name"]), big=False), unsafe_allow_html=True)
    c[2].markdown(stat("groups", "Patients ahead of you", f"{'—' if ahead is None else ahead}<div class='muted'>{'Not today' if ahead is None else 'still waiting or in visit'}</div>"), unsafe_allow_html=True)
    c[3].markdown(stat("schedule", "Provider status", status, big=False), unsafe_allow_html=True)


def select_day(d):
    ss["sel_day"], ss["pending"] = d, None


def set_pending(s):
    ss["pending"] = s


def go_month(delta):
    y, m = ss["cal_month"]
    m += delta
    if m == 13:
        y, m = y + 1, 1
    if m == 0:
        y, m = y - 1, 12
    ss["cal_month"] = (y, m)


def jump_to(d):
    ss["cal_month"] = (d.year, d.month)
    select_day(d)


def patient_calendar(pid):
    appt = next_appt(pid)
    if not appt:
        return
    now = now_et()
    today, last = now.date(), six_months_out(now.date())
    st.divider()
    st.subheader(":material/calendar_month: Reschedule your visit")
    other = st.toggle("Include other providers", value=False)
    prov = None if other else appt["provider_id"]
    slots = open_slots(appt["duration_minutes"], prov)
    names = {p["provider_id"]: p["name"] for p in providers()}
    names.setdefault(appt["provider_id"], appt["provider_name"])
    by_day = {}
    for s in slots:
        by_day.setdefault(s["start"].date(), []).append(s)
    ss.setdefault("cal_month", (today.year, today.month))
    y, m = ss["cal_month"]

    if slots:
        f = slots[0]
        st.button(f"Next available: {fmt_date(f['start'])} · {fmt_time(f['start'])}", icon=":material/bolt:",
                  on_click=jump_to, args=(f["start"].date(),))

    cal, times = st.columns([3, 2], gap="large")
    with cal:
        nav = st.columns([1, 4, 1])
        nav[0].button("◀", key="cal_prev", on_click=go_month, args=(-1,), disabled=(y, m) <= (today.year, today.month), use_container_width=True)
        nav[1].markdown(f"<div class='monthttl'>{calendar.month_name[m]} {y}</div>", unsafe_allow_html=True)
        nav[2].button("▶", key="cal_next", on_click=go_month, args=(1,), disabled=(y, m) >= (last.year, last.month), use_container_width=True)
        # tint each available day by how many openings it has (green 6+, amber 1-5)
        tint = []
        for d, lst in by_day.items():
            if d.month == m and d.year == y and ss.get("sel_day") != d:
                col = "rgba(33,168,100,.20)" if len(lst) >= 6 else "rgba(240,170,30,.22)"
                tint.append(f".st-key-cd{d} button{{background:{col}}}")
        cur = appt["start"].date()
        tint.append(f".st-key-cd{cur} button{{outline:2px solid #0b6e8a;outline-offset:-2px}}")
        st.markdown(f"<style>{''.join(tint)}</style>", unsafe_allow_html=True)
        with st.container(key="cal"):
            hdr = st.columns(7)
            for i, d in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
                hdr[i].markdown(f"<div class='dayhdr'>{d}</div>", unsafe_allow_html=True)
            for wk in calendar.Calendar(6).monthdatescalendar(y, m):
                cols = st.columns(7)
                for i, d in enumerate(wk):
                    if d.month != m:
                        continue
                    n = len(by_day.get(d, []))
                    label = f"**{d.day}**  \n{n} open" if n else f"**{d.day}**"
                    cols[i].button(label, key=f"cd{d}", disabled=n == 0, use_container_width=True,
                                   type="primary" if ss.get("sel_day") == d else "secondary",
                                   on_click=select_day, args=(d,))
        st.markdown(
            "<div class='legend'><span><i style='background:rgba(33,168,100,.5)'></i>6+ open</span>"
            "<span><i style='background:rgba(240,170,30,.55)'></i>1–5 open</span>"
            "<span><i style='border:2px solid #0b6e8a'></i>Your current visit</span>"
            f"<span>Openings shown through {last.strftime('%b %d, %Y')}</span></div>", unsafe_allow_html=True)

    with times:
        day = ss.get("sel_day")
        if not day or day not in by_day:
            st.markdown("#### Open times")
            st.caption("Pick a highlighted date on the calendar.")
        else:
            st.markdown(f"#### {day.strftime('%A, %B %d')}")
            with st.container(key="times"):
                ncol = 3 if prov else 2
                for part, rng in (("Morning", lambda s: s["start"].hour < 12), ("Afternoon", lambda s: s["start"].hour >= 12)):
                    grp = [s for s in by_day[day] if rng(s)]
                    if not grp:
                        continue
                    st.caption(part.upper())
                    for k in range(0, len(grp), ncol):
                        row = st.columns(ncol)
                        for j, s in enumerate(grp[k:k + ncol]):
                            label = fmt_time(s["start"]) + ("" if prov else f"  \n{names[s['provider_id']]}")
                            row[j].button(label, key=f"slot{s['provider_id']}{s['start'].isoformat()}",
                                          use_container_width=True, on_click=set_pending, args=(s,),
                                          type="primary" if (ss.get("pending") or {}).get("start") == s["start"]
                                          and (ss.get("pending") or {}).get("provider_id") == s["provider_id"] else "secondary")
        pend = ss.get("pending")
        if pend:
            st.success(f"Move to **{pend['start'].strftime('%a, %b %d')} at {fmt_time(pend['start'])} ET** with **{names[pend['provider_id']]}**?")
            if st.button("Confirm reschedule", type="primary", icon=":material/check_circle:", use_container_width=True):
                ok = reschedule(appt["appointment_id"], pend["provider_id"], pend["start"], "patient", f"patient:{pid}")
                ss["pending"] = None
                st.toast("Appointment rescheduled." if ok else "That time was just taken. Please pick another.", icon="✅" if ok else "⚠️")
                st.rerun()
            st.button("Keep my current time", icon=":material/close:", use_container_width=True, on_click=set_pending, args=(None,))
    st.info(f":material/call: Need a date after {last.strftime('%B %d, %Y')}? Please call the front desk at **{FRONT_DESK}**.")


def patient_view():
    pid = patient_picker()
    if not pid:
        return
    if ss.get("audited_patient") != pid:
        audit("patient", f"patient:{pid}", "VIEW", "patients", pid)
        ss["audited_patient"] = pid
    patient_status(pid)
    patient_calendar(pid)


# ====================================================================== STAFF VIEW
def who():
    return "staff", "front-desk"


def next_workday(d, step=1):
    while not is_workday(d):
        d += timedelta(days=step)
    return d


def go_day(delta):
    ss["sch_day"] = next_workday(ss["sch_day"] + timedelta(days=delta), 1 if delta > 0 else -1)


def go_today():
    ss["sch_day"] = next_workday(now_et().date())


def set_focus(pid):
    ss["focus_pid"] = pid


def clear_focus():
    ss["focus_pid"] = None
    ss["last_pick"] = None


def do_status(appt_id, status):
    set_status(appt_id, status, *who())


@st.fragment(run_every=10)
def provider_cards():
    now = now_et()
    provs = providers()
    appts = appts_between(at(now.date(), 0), at(now.date() + timedelta(days=1), 0))
    cols = st.columns(max(len(provs), 1))
    for i, p in enumerate(provs):
        mine = [a for a in appts if a["provider_id"] == p["provider_id"]]
        cls, label = status_of(p, mine)
        cur = next((a for a in mine if a["start"] <= now < a["end"] and a["status"] == "scheduled"), None)
        nxt = next((a for a in mine if a["start"] > now and a["status"] == "scheduled"), None)
        if cur:
            line = f'<span class="msr">person</span> In visit · <b>{esc(cur["type_name"])}</b>'
        elif nxt:
            line = f'<span class="msr">schedule</span> Next {fmt_time(nxt["start"])} · <b>{esc(nxt["type_name"])}</b>'
        else:
            line = '<span class="msr">check_circle</span> Nothing more today'
        booked = sum(1 for a in mine if a["status"] in ("scheduled", "completed"))
        cols[i].markdown(
            f'<div class="card"><div class="pname"><span class="msr">stethoscope</span> {esc(p["name"])}</div>'
            f'<div class="muted">{esc(p["specialty"])}</div><div style="margin:.5rem 0"><span class="pill {cls}">{label}</span></div>'
            f'<div style="display:flex;gap:1.6rem"><div><div class="big">{booked}</div><div class="muted">booked today</div></div>'
            f'<div><div class="big">{p["delay_min"]}</div><div class="muted">min late</div></div></div>'
            f'<div class="muted" style="margin-top:.6rem">{line}</div></div>', unsafe_allow_html=True)


def delay_controls():
    with st.expander("Update a provider's delay", icon=":material/timer:"):
        provs = providers()
        c = st.columns([2, 1, 1, 2])
        pv = c[0].selectbox("Provider", provs, format_func=lambda p: p["name"], key="dl_p")
        mins = c[1].number_input("Minutes late", 0, 180, int(pv["delay_min"]), 5, key=f"dl_m{pv['provider_id']}")
        c[2].write("")
        if c[2].button("Save", icon=":material/save:"):
            set_delay(pv["provider_id"], int(mins), *who())
            st.rerun()
        c[3].write("")
        if c[3].button("Simulate live activity (demo)", icon=":material/bolt:"):
            for p in provs:
                set_delay(p["provider_id"], random.choice([0, 0, 10, 15, 20, 30]), "system", "simulator")
            st.rerun()


@st.fragment(run_every=10)
def schedule_panel():
    now = now_et()
    provs = providers()
    names = {p["provider_id"]: p["name"] for p in provs}
    ids = list(names)
    sel = st.segmented_control("Provider", ids, format_func=lambda i: names[i], default=ids[0],
                               key="sch_prov", label_visibility="collapsed")
    pid = sel if sel in names else ids[0]
    day = ss["sch_day"]
    n = st.columns([1, 1, 3, 1])
    n[0].button("◀", key="d_prev", on_click=go_day, args=(-1,), use_container_width=True)
    n[1].button("Today", key="d_today", on_click=go_today, use_container_width=True)
    n[2].markdown(f"<div class='monthttl' style='font-size:1.15rem'>{day.strftime('%A, %B %d, %Y')}</div>", unsafe_allow_html=True)
    n[3].button("▶", key="d_next", on_click=go_day, args=(1,), use_container_width=True)

    appts = appts_between(at(day, 0), at(day + timedelta(days=1), 0), pid)
    lunch_shown = False
    for r in day_rows(day, appts, now):
        if r["kind"] == "lunch":
            if not lunch_shown:
                st.caption("12:00 – 1:00 PM · Lunch")
                lunch_shown = True
            continue
        c = st.columns([2.8, 1.1, 1.1, 1.3])
        span = f"{fmt_time(r['start'])} – {fmt_time(r['end'])}"
        if r["kind"] == "open":
            sub = '<span class="slot-open">Open</span>' + (" · past" if r["tstate"] == "past" else "")
            c[0].markdown(f"<div class='rowtxt'><b>{span}</b><br><span class='sm'>{sub}</span></div>", unsafe_allow_html=True)
            continue
        a = r["appt"]
        tag = {"scheduled": "Booked", "completed": "Completed", "no_show": "No-show"}[a["status"]]
        if a["status"] == "scheduled" and r["tstate"] == "past":
            tag = "Past · not marked"
        elif a["status"] == "scheduled" and r["tstate"] == "now":
            tag = "In progress"
        c[0].markdown(f"<div class='rowtxt'><b>{span}</b><br><span class='sm'>{tag} · {esc(a['type_name'])}</span></div>", unsafe_allow_html=True)
        c[1].button("View", key=f"v{a['appointment_id']}", icon=":material/person:", use_container_width=True,
                    on_click=set_focus, args=(a["patient_id"],))
        if a["status"] == "scheduled":
            c[2].button("Cancel", key=f"c{a['appointment_id']}", use_container_width=True,
                        on_click=do_status, args=(a["appointment_id"], "cancelled"))
            c[3].button("No-show", key=f"n{a['appointment_id']}", use_container_width=True,
                        disabled=a["start"] > now, on_click=do_status, args=(a["appointment_id"], "no_show"))
    if not appts:
        st.caption("No appointments booked this day." if is_workday(day) else "Clinic closed.")


def patient_panel():
    st.markdown("#### :material/person: Patient")
    q = st.text_input("Search by name", placeholder="First or last name", icon=":material/search:", key="pq")
    if q.strip():
        hits = [pid for pid, p in patient_map().items() if q.strip().lower() in f"{p['first_name']} {p['last_name']}".lower()][:10]
        if not hits:
            st.caption("No matching patients.")
        else:
            pick = st.selectbox("Matches", hits, index=None, format_func=pname, key="ppick",
                                placeholder=f"{len(hits)} match(es) — choose one")
            if pick and pick != ss.get("last_pick"):
                ss["last_pick"], ss["focus_pid"] = pick, pick
            elif not pick:
                ss["last_pick"] = None
    pid = ss.get("focus_pid")
    if not pid:
        st.info("Search for a patient, or press View on a booked slot.")
        return
    if ss.get("audited_focus") != pid:
        audit(*who(), "VIEW_PATIENT", "patients", pid)
        ss["audited_focus"] = pid
    p = patient_map()[pid]
    st.markdown(
        f'<div class="card"><div class="mid">{esc(p["first_name"])} {esc(p["last_name"])}</div>'
        f'<div class="muted">DOB {p["date_of_birth"]:%m/%d/%Y} · {esc(p["sex"])} · {esc(p["phone_masked"])}</div></div>',
        unsafe_allow_html=True)
    st.write("")
    now = now_et()
    appts = patient_appts(pid)
    upcoming = sorted([a for a in appts if a["end"] > now and a["status"] != "cancelled"], key=lambda a: a["start"])[:4]
    recent = [a for a in appts if a["end"] <= now][:3]
    for title, group in (("Upcoming", upcoming), ("Recent", recent)):
        if not group:
            continue
        st.caption(title.upper())
        for a in group:
            c = st.columns([3, 1.2, 1.3])
            c[0].markdown(f"<div class='rowtxt'><b>{fmt_date(a['start'])} · {fmt_time(a['start'])}</b><br>"
                          f"<span class='sm'>{esc(a['provider_name'])} · {esc(a['type_name'])} · {a['status'].replace('_', ' ')}</span></div>",
                          unsafe_allow_html=True)
            if a["status"] == "scheduled":
                c[1].button("Cancel", key=f"pc{a['appointment_id']}", use_container_width=True,
                            on_click=do_status, args=(a["appointment_id"], "cancelled"))
                c[2].button("No-show", key=f"pn{a['appointment_id']}", use_container_width=True,
                            disabled=a["start"] > now, on_click=do_status, args=(a["appointment_id"], "no_show"))
    st.button("Close patient", icon=":material/close:", on_click=clear_focus)


def schedule_tab():
    provider_cards()
    delay_controls()
    st.write("")
    left, right = st.columns([5, 3], gap="large")
    with left:
        schedule_panel()
    with right:
        patient_panel()


def waitlist_tab():
    wl = waitlist_rows()
    soon = [s for s in open_slots(30) if s["start"] < now_et() + timedelta(days=3)]
    c = st.columns(2)
    c[0].markdown(stat("hourglass_top", "Patients waiting", len(wl)), unsafe_allow_html=True)
    c[1].markdown(stat("event_available", "Open slots · next 3 days", len(soon)), unsafe_allow_html=True)
    st.write("")
    if not wl:
        st.success("Waitlist is empty.")
        return
    w = st.selectbox("Waitlisted patient", wl, index=None, placeholder="Choose a patient to fill an opening",
                     format_func=lambda w: f"{pname(w['patient_id'])} · {w['provider_name']}")
    if not w:
        return
    st.markdown(
        f'<div class="card"><div class="mid">{esc(pname(w["patient_id"]))}</div><div class="muted">Wants {esc(w["provider_name"])} · '
        f'{w["preferred_start_date"]:%b %d} to {w["preferred_end_date"]:%b %d, %Y} · waiting since {w["created"]:%b %d}</div></div>',
        unsafe_allow_html=True)
    st.write("")
    tid = st.radio("Visit type", list(types()), format_func=lambda t: f"{tlabel(t)} ({tdur(t)} min)", horizontal=True,
                   index=list(types()).index(2) if 2 in types() else 0)
    anydate = st.checkbox("Also show openings outside the preferred dates")
    opts = [s for s in open_slots(tdur(tid), w["provider_id"])
            if anydate or w["preferred_start_date"] <= s["start"].date() <= w["preferred_end_date"]][:30]
    if not opts:
        st.info("No matching openings for this provider and date range.")
    else:
        s = st.selectbox("Matching openings", opts, format_func=lambda s: f"{fmt_date(s['start'])} · {fmt_time(s['start'])}")
        if st.button("Book this opening", type="primary", icon=":material/event_available:"):
            ok = book(w["patient_id"], s["provider_id"], s["start"], tid, *who(), waitlist_id=w["waitlist_id"])
            st.toast("Booked from waitlist." if ok else "Slot was just taken — pick another.", icon="✅" if ok else "⚠️")
            st.rerun()
    if st.button("Remove from waitlist", icon=":material/delete:"):
        waitlist_expire(w["waitlist_id"], *who())
        st.rerun()


def json_short(d):
    return ", ".join(f"{k}={v}" for k, v in (d or {}).items())[:120]


ACTION_LABELS = {
    "BOOK": "🟢 Booked", "BOOK_FROM_WAITLIST": "🟢 Booked from waitlist", "RESCHEDULE": "🔵 Rescheduled",
    "CANCELLED": "🔴 Cancelled", "NO_SHOW": "🟠 No-show", "COMPLETED": "✅ Completed",
    "SET_DELAY": "⏱️ Delay updated", "WAITLIST_FILLED": "🟣 Waitlist filled", "WAITLIST_REMOVED": "⚪ Removed from waitlist",
    "VIEW": "👁️ Viewed", "VIEW_PATIENT": "👁️ Viewed", "DB_INSERT": "🗄️ Added in database",
    "DB_UPDATE": "🗄️ Edited in database", "DB_DELETE": "🗄️ Deleted in database",
}


def _obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}


def _t(v):
    try:
        d = datetime.fromisoformat(str(v)).astimezone(ET)
        return f"{fmt_date(d)} {fmt_time(d)}"
    except Exception:
        return str(v)


def describe(r):
    a, o, n = r["action_type"], _obj(r["old_values_json"]), _obj(r["new_values_json"])
    if a in ("VIEW", "VIEW_PATIENT"):
        return ""
    if a in ("BOOK", "BOOK_FROM_WAITLIST"):
        return f"Patient #{n.get('patient_id')} · {_t(n.get('start'))}"
    if a == "RESCHEDULE":
        return f"{_t(o.get('start'))}  →  {_t(n.get('start'))}"
    if a == "SET_DELAY":
        return f"{o.get('delay_minutes') or 0} min  →  {n.get('delay_minutes')} min late"
    if "status" in o or "status" in n:
        return f"{o.get('status', '—')}  →  {n.get('status', '—')}"
    return json_short(n or o)


@st.fragment(run_every=10)
def audit_tab():
    st.caption("Every change is recorded here, refreshed every 10 seconds. Patient names are never stored in the log, only IDs.")
    rows = audit_rows(300)
    c = st.columns([2, 2, 2, 1.4])
    changes_only = c[3].toggle("Changes only", value=True, help="Hide 'Viewed' entries")
    roles = sorted({r["user_role"] for r in rows})
    role_f = c[0].multiselect("Role", roles, placeholder="All roles")
    acts = sorted({ACTION_LABELS.get(r["action_type"], r["action_type"]) for r in rows})
    act_f = c[1].multiselect("Event", acts, placeholder="All events")
    q = c[2].text_input("Search", placeholder="User, record ID, detail", icon=":material/search:")

    def keep(r):
        lab = ACTION_LABELS.get(r["action_type"], r["action_type"])
        if changes_only and r["action_type"] in ("VIEW", "VIEW_PATIENT"):
            return False
        if r["action_type"] == "LOGIN_FAILED" and changes_only:
            return False
        if role_f and r["user_role"] not in role_f:
            return False
        if act_f and lab not in act_f:
            return False
        if q.strip() and q.strip().lower() not in f"{r['user_id']} {r['record_id']} {describe(r)} {lab}".lower():
            return False
        return True

    shown = [r for r in rows if keep(r)]
    today = now_et().date()
    todays = [r for r in rows if r["ts_et"].date() == today and r["action_type"] not in ("VIEW", "VIEW_PATIENT", "LOGIN_FAILED")]
    s = st.columns(4)
    s[0].markdown(stat("history", "Changes today", len(todays)), unsafe_allow_html=True)
    s[1].markdown(stat("event_available", "Booked / rescheduled", sum(1 for r in todays if r["action_type"] in ("BOOK", "BOOK_FROM_WAITLIST", "RESCHEDULE"))), unsafe_allow_html=True)
    s[2].markdown(stat("event_busy", "Cancelled / no-show", sum(1 for r in todays if r["action_type"] in ("CANCELLED", "NO_SHOW"))), unsafe_allow_html=True)
    s[3].markdown(stat("timer", "Delay updates", sum(1 for r in todays if r["action_type"] == "SET_DELAY")), unsafe_allow_html=True)
    st.write("")
    if not shown:
        st.info("No activity matches these filters.")
        return
    df = pd.DataFrame([{
        "When": r["ts_et"].strftime("%b %d, %I:%M:%S %p").replace(" 0", " "),
        "Event": ACTION_LABELS.get(r["action_type"], r["action_type"]),
        "Who": f"{r['user_role']} · {r['user_id']}",
        "Record": f"{r['table_affected']} #{r['record_id']}" if r["record_id"] else r["table_affected"],
        "Details": describe(r)} for r in shown])
    st.dataframe(df, use_container_width=True, hide_index=True, height=480, column_config={
        "When": st.column_config.TextColumn(width="small"),
        "Event": st.column_config.TextColumn(width="medium"),
        "Who": st.column_config.TextColumn(width="medium"),
        "Record": st.column_config.TextColumn(width="small"),
        "Details": st.column_config.TextColumn(width="large")})
    st.caption(f"Showing {len(shown)} of the latest {len(rows)} entries.")


def staff_view():
    ss.setdefault("sch_day", next_workday(now_et().date()))
    t = st.tabs([":material/calendar_view_day: Schedule", ":material/hourglass_top: Waitlist", ":material/history: Activity log"])
    with t[0]:
        schedule_tab()
    with t[1]:
        waitlist_tab()
    with t[2]:
        audit_tab()


# ====================================================================== MAIN
header()
view = st.sidebar.radio("Waitwise", ["Patient view", "Staff view"], label_visibility="collapsed")
st.sidebar.caption(":material/history: All changes are recorded in the activity log")
patient_view() if view == "Patient view" else staff_view()
