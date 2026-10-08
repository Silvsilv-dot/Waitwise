"""Waitwise: live clinic wait times, delays, cancellations and rescheduling.
One Python file. Reads and writes the Postgres (Neon) database you loaded.
Run with:  streamlit run app.py
"""
import calendar
import datetime as dt
import json
import os
import re

import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="Waitwise", page_icon="🩺", layout="wide")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    st.error("DATABASE_URL is missing. Copy .env.example to .env and paste your Neon connection string in it.")
    st.stop()

NOW = dt.datetime(2026, 10, 5, 13, 20)   # demo clock (the data is a snapshot of this moment)
PATIENT = "Sam Rivera"
ACTIVE = ("scheduled", "checked_in", "in_progress")


# ---------- database helpers ----------
def q(sql, params=None):
    """Run a SELECT and return a list of dicts."""
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def do(sql, params=None, ok="Done."):
    """Run a change (insert/update/function). Shows the result at the top of the page."""
    try:
        with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.rowcount == 0:
                st.session_state.msg = ("error", "Nothing changed. That slot may have just been taken.")
                return False
        st.session_state.msg = ("success", ok)
        return True
    except Exception as e:  # database errors come back as readable text
        st.session_state.msg = ("error", str(e).split("\n")[0])
        return False


def fmt(ts):
    return f"{ts:%a %b} {ts.day}, {ts.hour % 12 or 12}:{ts.minute:02d} {'AM' if ts.hour < 12 else 'PM'}"


def tm(ts):
    return f"{ts.hour % 12 or 12}:{ts.minute:02d} {'AM' if ts.hour < 12 else 'PM'}"


PROV = {r["provider_id"]: r["name"] for r in q("SELECT provider_id, name FROM clinic.providers ORDER BY 1")}

# ---------- header ----------
st.title("🩺 Waitwise")
st.caption("Riverside Family Clinic (synthetic data) · live from your Neon database · demo clock: "
           f"{NOW:%b %d, %Y} {tm(NOW)}")
if "msg" in st.session_state:
    kind, text = st.session_state.pop("msg")
    (st.success if kind == "success" else st.error)(text)

sched = {r["appt_id"]: r for r in q("SELECT * FROM clinic.projected_schedule(%s)", (NOW,))}
board = q("SELECT * FROM clinic.provider_status(%s)", (NOW,))

tab1, tab2, tab3 = st.tabs(["My visit", "Front desk", "Book ahead (6 months)"])

# ---------- tab 1: patient view ----------
with tab1:
    me = q("""SELECT a.appt_id, a.provider_id, a.scheduled_at, a.status, a.visit_type
              FROM clinic.appointments a JOIN clinic.patients p USING (patient_id)
              WHERE p.display_name = %s AND a.status = ANY(%s) AND a.scheduled_at::date = %s
              ORDER BY a.scheduled_at LIMIT 1""", (PATIENT, list(ACTIVE), NOW.date()))
    left, right = st.columns([3, 2])
    with left:
        st.subheader(f"{PATIENT}'s visit today")
        if not me:
            st.info("No visit scheduled for today. Use the **Book ahead** tab to pick a time.")
        else:
            me = me[0]
            doc = PROV[me["provider_id"]]
            if me["status"] == "in_progress":
                st.success(f"You're with {doc} right now.")
            else:
                s = sched[me["appt_id"]]
                wait = max(0, round((s["projected_start"] - NOW).total_seconds() / 60))
                ahead = q("""SELECT count(*) AS c FROM clinic.appointments WHERE provider_id = %s
                             AND scheduled_at < %s AND status = ANY(%s)""",
                          (me["provider_id"], me["scheduled_at"], list(ACTIVE)))[0]["c"]
                c1, c2, c3 = st.columns(3)
                c1.metric("Minutes until you're seen", wait)
                c2.metric("Expected start", tm(s["projected_start"]))
                c3.metric("Patients ahead of you", ahead)
                d = s["delay_min"]
                st.caption(f"Booked for {tm(me['scheduled_at'])} with {doc}")
                if d <= 5:
                    st.success(f"On schedule. Arrive by {tm(s['projected_start'] - dt.timedelta(minutes=10))} to check in.")
                elif d < 15:
                    st.warning(f"{doc} is running about {d} min late.")
                else:
                    st.error(f"{doc} is about {d} min behind. You can arrive by "
                             f"{tm(s['projected_start'] - dt.timedelta(minutes=10))} instead of waiting here.")
                st.toggle("Text me when I'm 10 minutes away", key="notify")

                st.markdown("**Move to an earlier time today**")
                opens = q("""SELECT appt_id, provider_id, scheduled_at FROM clinic.appointments
                             WHERE status IN ('cancelled','no_show') AND scheduled_at::date = %s
                             AND scheduled_at >= %s AND scheduled_at < %s ORDER BY scheduled_at""",
                          (NOW.date(), NOW + dt.timedelta(minutes=15), me["scheduled_at"]))
                if not opens:
                    st.caption("No earlier openings right now. Check back, or tap Reschedule on the Book ahead tab.")
                for o in opens[:6]:
                    a, b = st.columns([4, 1])
                    a.write(f"**{tm(o['scheduled_at'])}** · {PROV[o['provider_id']]}")
                    if b.button("Move me", key="mv" + o["appt_id"]):
                        if do("SELECT clinic.reschedule_appointment(%s, %s)", (me["appt_id"], o["appt_id"]),
                              f"Moved you to {tm(o['scheduled_at'])}. Your old time went to the waitlist."):
                            st.rerun()
                if st.button("Cancel my visit"):
                    if do("SELECT clinic.cancel_appointment(%s)", (me["appt_id"],), "Visit cancelled. Slot released."):
                        st.rerun()
        nxt = q("""SELECT s.provider_id, s.slot_at FROM clinic.future_slots s JOIN clinic.patients p USING (patient_id)
                   WHERE p.display_name = %s AND s.status = 'booked' AND s.slot_at > %s ORDER BY s.slot_at LIMIT 1""",
                (PATIENT, NOW))
        if nxt:
            st.info(f"Your next booked visit: **{fmt(nxt[0]['slot_at'])}** with {PROV[nxt[0]['provider_id']]}")
    with right:
        st.subheader("Who's running late")
        for b in board:
            icon = "🟢" if b["delay_min"] <= 5 else "🟡" if b["delay_min"] < 15 else "🔴"
            st.metric(f"{icon} {b['name']}", b["status"], f"{b['delay_min']} min behind" if b["delay_min"] else "on schedule",
                      delta_color="off")

# ---------- tab 2: front desk ----------
with tab2:
    st.subheader("Report delays")
    cols = st.columns(len(PROV))
    for col, (pid, name) in zip(cols, PROV.items()):
        col.markdown(f"**{name}**")
        for mins in (10, 20):
            if col.button(f"+{mins} min delay", key=f"d{pid}{mins}"):
                if do("SELECT clinic.report_delay(%s, %s, %s)", (pid, mins, NOW), f"{name}: +{mins} min reported."):
                    st.rerun()
    st.divider()
    st.subheader("Upcoming visits")
    who = st.radio("Provider", list(PROV), format_func=PROV.get, horizontal=True)
    rows = [r for r in sched.values() if r["provider_id"] == who][:10]
    for r in rows:
        a, b, c, d_ = st.columns([3, 2, 1, 1])
        a.write(f"{tm(r['scheduled_at'])} · {r['patient']}")
        b.caption(f"est. start {tm(r['projected_start'])}" + (f" ({r['delay_min']} min late)" if r["delay_min"] >= 10 else ""))
        if c.button("Cancel", key="c" + r["appt_id"]):
            if do("SELECT clinic.cancel_appointment(%s)", (r["appt_id"],), f"{r['patient']} cancelled. Opening released."):
                st.rerun()
        if d_.button("No-show", key="n" + r["appt_id"]):
            if do("SELECT clinic.cancel_appointment(%s, 'no_show')", (r["appt_id"],), f"{r['patient']} marked no-show."):
                st.rerun()
    st.divider()
    st.subheader("Openings to fill from the waitlist")
    opens = q("""SELECT appt_id, provider_id, scheduled_at FROM clinic.appointments
                 WHERE status IN ('cancelled','no_show') AND scheduled_at >= %s AND scheduled_at::date = %s
                 ORDER BY scheduled_at LIMIT 8""", (NOW + dt.timedelta(minutes=15), NOW.date()))
    wl = q("SELECT count(*) AS c FROM clinic.waitlist")[0]["c"]
    st.caption(f"{wl} patient(s) on the waitlist")
    for o in opens:
        a, b = st.columns([4, 1])
        a.write(f"**{tm(o['scheduled_at'])}** · {PROV[o['provider_id']]}")
        if b.button("Fill", key="f" + o["appt_id"]):
            if do("""WITH w AS (DELETE FROM clinic.waitlist WHERE waitlist_id =
                       (SELECT waitlist_id FROM clinic.waitlist ORDER BY joined_at, waitlist_id LIMIT 1) RETURNING patient_id)
                     UPDATE clinic.appointments SET status = 'scheduled', patient_id = w.patient_id, delay_adj_min = 0
                     FROM w WHERE appt_id = %s AND status IN ('cancelled','no_show')""", (o["appt_id"],),
                  f"Filled {tm(o['scheduled_at'])} from the waitlist."):
                st.rerun()
    if not opens:
        st.caption("No openings yet. Cancel a visit above to create one.")

# ---------- tab 3: book ahead ----------
with tab3:
    PROMPT = ("Convert a patient scheduling request into a JSON search filter. Today is 2026-10-05. "
              "Providers: okafor, chen, ruiz. Openings exist 2026-10-06 to 2027-04-06. Return ONLY JSON: "
              '{"provider":null|"okafor"|"chen"|"ruiz","weekdays":[0-6, Sunday=0, or empty],'
              '"after":minutes or 0,"before":minutes or 1440,"from":"YYYY-MM-DD","to":"YYYY-MM-DD",'
              '"limit":number up to 8,"note":"one short sentence"}. Request: ')

    def keyword_filter(text):
        t = text.lower()
        f = {"provider": None, "weekdays": [], "after": 0, "before": 1440, "from": "2026-10-06", "to": "2027-04-06",
             "limit": 5, "note": text}
        for p in PROV:
            if p in t:
                f["provider"] = p
        days = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
        f["weekdays"] = [i for i, d in enumerate(days) if d in t or re.search(rf"\b{d[:3]}\b", t)]
        if "morning" in t:
            f["before"] = 720
        if "afternoon" in t:
            f["after"] = 720
        for i, m in enumerate(calendar.month_name[1:], start=1):
            if m.lower() in t:
                y = 2026 if m in ("October", "November", "December") else 2027
                f["from"], f["to"] = f"{y}-{i:02d}-01", f"{y}-{i:02d}-28"
        return f

    def ask_claude(text):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            import anthropic
            msg = anthropic.Anthropic().messages.create(
                model="claude-sonnet-5-5", max_tokens=300,
                messages=[{"role": "user", "content": PROMPT + text[:300]}])
            out = "".join(b.text for b in msg.content if b.type == "text")
            return json.loads(out[out.index("{"): out.rindex("}") + 1])
        except Exception:
            return None

    def search(f):
        """Run the filter against the real future_slots table."""
        return q("""SELECT slot_id, provider_id, slot_at FROM clinic.future_slots
                    WHERE status = 'open' AND slot_at::date BETWEEN %s AND %s
                      AND (%s::text IS NULL OR provider_id = %s)
                      AND (cardinality(%s::int[]) = 0 OR extract(dow FROM slot_at)::int = ANY(%s::int[]))
                      AND extract(hour FROM slot_at) * 60 + extract(minute FROM slot_at) >= %s
                      AND extract(hour FROM slot_at) * 60 + extract(minute FROM slot_at) < %s
                    ORDER BY slot_at LIMIT %s""",
                 (f["from"], f["to"], f["provider"], f["provider"], list(f["weekdays"]), list(f["weekdays"]),
                  int(f["after"]), int(f["before"]), min(8, int(f["limit"]))))

    def book(slot_id, label):
        """Book a future slot for the patient; optionally release today's visit."""
        cur = q("""SELECT a.appt_id FROM clinic.appointments a JOIN clinic.patients p USING (patient_id)
                   WHERE p.display_name = %s AND a.status = ANY(%s) AND a.scheduled_at::date = %s""",
                (PATIENT, list(ACTIVE), NOW.date()))
        if do("SELECT clinic.book_slot(%s, %s)", (slot_id, PATIENT), f"Booked {label}."):
            if cur and st.session_state.get("release", True) and cur[0]:
                do("SELECT clinic.cancel_appointment(%s)", (cur[0]["appt_id"],), f"Booked {label}. Your old time went to the waitlist.")
            st.rerun()

    st.subheader("Ask in plain English")
    ask = st.text_input("What are you looking for?", placeholder="e.g. Dr. Chen on a Tuesday morning in January")
    if st.button("Search") and ask.strip():
        f = ask_claude(ask)
        how = "Claude read your request as" if f else "Matched keywords (set ANTHROPIC_API_KEY for AI)"
        f = f or keyword_filter(ask)
        try:
            st.session_state.ai = (how, f.get("note", ask), search(f))
        except Exception as e:
            st.session_state.ai = (how, "Couldn't run that search: " + str(e).split("\n")[0], [])
    if "ai" in st.session_state:
        how, note, found = st.session_state.ai
        st.caption(f"{how}: {note}")
        if not found:
            st.write("No matching openings. Try a wider window.")
        for r in found:
            a, b = st.columns([4, 1])
            a.write(f"**{fmt(r['slot_at'])}** · {PROV[r['provider_id']]}")
            if b.button("Book", key="ai" + r["slot_id"]):
                book(r["slot_id"], fmt(r["slot_at"]))
    st.checkbox("Release my visit today when I book a later time", value=True, key="release")
    st.divider()

    st.subheader("Next 6 months")
    soon = st.columns(len(PROV))
    for col, pid in zip(soon, PROV):
        n = q("SELECT * FROM clinic.next_available(%s, %s, 1)", (pid, NOW))
        col.metric(f"Soonest: {PROV[pid]}", fmt(n[0]["slot_at"]) if n else "None")

    months = [dt.date(2026, 10, 1)] + [dt.date(2026 + (9 + i) // 12, (9 + i) % 12 + 1, 1) for i in range(1, 7)]
    c1, c2 = st.columns(2)
    month = c1.selectbox("Month", months, format_func=lambda d: d.strftime("%B %Y"))
    prov = c2.selectbox("Provider", ["all"] + list(PROV), format_func=lambda p: "All providers" if p == "all" else PROV[p])
    last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    counts = {}
    for r in q("SELECT * FROM clinic.availability_by_day(%s, %s)", (month, last)):
        if prov == "all" or r["provider_id"] == prov:
            counts[r["day"]] = counts.get(r["day"], 0) + r["open_slots"]

    head = st.columns(7)
    for h, name in zip(head, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        h.caption(name)
    for week in calendar.Calendar(0).monthdatescalendar(month.year, month.month):
        cells = st.columns(7)
        for cell, day in zip(cells, week):
            if day.month != month.month:
                continue
            n = counts.get(day, 0)
            if cell.button(f"{day.day}\n{n} open" if n else f"{day.day}\n–", key=f"day{day}", disabled=not n,
                           use_container_width=True):
                st.session_state.day = day

    day = st.session_state.get("day")
    if day and month.month == day.month:
        st.markdown(f"#### {day:%A, %B} {day.day}")
        slots = q("""SELECT slot_id, provider_id, slot_at FROM clinic.future_slots
                     WHERE status = 'open' AND slot_at::date = %s ORDER BY provider_id, slot_at""", (day,))
        for pid in PROV:
            mine = [s for s in slots if s["provider_id"] == pid and prov in ("all", pid)]
            if not mine:
                continue
            st.write(f"**{PROV[pid]}**")
            row = st.columns(6)
            for i, s in enumerate(mine):
                if row[i % 6].button(tm(s["slot_at"]), key="s" + s["slot_id"]):
                    book(s["slot_id"], fmt(s["slot_at"]))
