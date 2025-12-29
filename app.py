import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import mux_python
import os
import requests
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import hashlib
from dotenv import load_dotenv

# --- CONFIG & DATABASE ---
load_dotenv("mux-FC360 App.env")

MUX_TOKEN_ID = st.secrets.get("MUX_TOKEN_ID") or os.getenv('MUX_TOKEN_ID')
MUX_TOKEN_SECRET = st.secrets.get("MUX_TOKEN_SECRET") or os.getenv('MUX_TOKEN_SECRET')
DB_URL = st.secrets.get("DATABASE_URL") or os.getenv('DATABASE_URL')

configuration = mux_python.Configuration()
configuration.username = MUX_TOKEN_ID
configuration.password = MUX_TOKEN_SECRET

Base = declarative_base()

class Team(Base):
    __tablename__ = 'teams'
    id = Column(Integer, primary_key=True); name = Column(String(100), unique=True)
    matches = relationship("Match", back_populates="team")
    users = relationship("User", back_populates="team")

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True); email = Column(String(100), unique=True)
    password = Column(String(100)); role = Column(String(20), default='user') 
    team_id = Column(Integer, ForeignKey('teams.id'), nullable=True)
    team = relationship("Team", back_populates="users")

class Match(Base):
    __tablename__ = 'matches'
    id = Column(Integer, primary_key=True); opponent = Column(String(200))
    team_id = Column(Integer, ForeignKey('teams.id')); mux_asset_id = Column(String(100))
    mux_playback_id = Column(String(100)); status = Column(String(50), default='uploading')
    team = relationship("Team", back_populates="matches")
    events = relationship("Event", back_populates="match", cascade="all, delete-orphan")

class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True); match_id = Column(Integer, ForeignKey('matches.id'))
    tag = Column(String(100)); player = Column(String(100)); start_ms = Column(Integer); end_ms = Column(Integer)
    match = relationship("Match", back_populates="events")

# Engine with SSL & Pooler support
if DB_URL and DB_URL.startswith("postgres"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DB_URL, pool_pre_ping=True)
else:
    engine = create_engine('sqlite:///soccer_mux.db', connect_args={"check_same_thread": False})

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --- UTILS ---
def hash_password(password): return hashlib.sha256(str.encode(password)).hexdigest()

def create_mux_upload():
    try:
        api_client = mux_python.ApiClient(configuration)
        direct_uploads_api = mux_python.DirectUploadsApi(api_client)
        create_asset_request = mux_python.CreateAssetRequest(playback_policy=[mux_python.PlaybackPolicy.PUBLIC])
        create_upload_request = mux_python.CreateUploadRequest(new_asset_settings=create_asset_request, cors_origin="*", timeout=3600)
        upload = direct_uploads_api.create_direct_upload(create_upload_request)
        return upload.data.url, upload.data.id
    except Exception as e:
        st.error(f"Mux Error: {e}"); return None, None

def update_processing_matches():
    session = Session()
    processing = session.query(Match).filter(Match.status.in_(['processing', 'uploading'])).all()
    if not processing: st.toast("All matches synced!"); session.close(); return
    api_client = mux_python.ApiClient(configuration); uploads_api = mux_python.DirectUploadsApi(api_client); assets_api = mux_python.AssetsApi(api_client)
    for m in processing:
        try:
            up = uploads_api.get_direct_upload(m.mux_asset_id)
            if up.data.asset_id:
                asset = assets_api.get_asset(up.data.asset_id)
                if asset.data.status == 'ready':
                    m.mux_playback_id = asset.data.playback_ids[0].id
                    m.status = 'ready'
        except: continue
    session.commit(); session.close(); st.rerun()

def parse_xml(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes.decode('utf-8', errors='ignore').strip())
        events = []
        for instance in root.findall('.//instance'):
            start = instance.find('start'); end = instance.find('end'); code = instance.find('code'); label = instance.find('.//label/text')
            if start is not None and end is not None:
                events.append({'tag': code.text if code is not None else 'Unknown', 'player': label.text if label is not None else '', 'start_ms': int(float(start.text) * 1000), 'end_ms': int(float(end.text) * 1000)})
        return events
    except Exception as e: st.error(f"XML Error: {e}"); return []

# --- LOGIN & REGISTRATION ---
if "authenticated" not in st.session_state:
    st.session_state.update({"authenticated": False, "user_id": None, "role": None, "active_video": None})

if not st.session_state.authenticated:
    st.set_page_config(page_title="FC360 Login", page_icon="⚽")
    st.title("⚽ FC360 Team Performance")
    t1, t2 = st.tabs(["Login", "Create Account"])
    db = Session()
    with t1:
        e = st.text_input("Email"); p = st.text_input("Password", type="password")
        if st.button("Sign In", use_container_width=True):
            user = db.query(User).filter(User.email == e, User.password == hash_password(p)).first()
            if user:
                st.session_state.update({"authenticated": True, "user_id": user.id, "role": user.role})
                st.rerun()
            else: st.error("Access Denied")
    with t2:
        ne = st.text_input("New Email"); np = st.text_input("New Password", type="password")
        if st.button("Register as User", use_container_width=True):
            try:
                db.add(User(email=ne, password=hash_password(np), role='user')) # Hardcoded user role
                db.commit(); st.success("Account created! Go to Login.")
            except: st.error("Email already in use.")
    db.close(); st.stop()

# --- MAIN APP ---
st.set_page_config(page_title="FC360 Pro", layout="wide")

with st.sidebar:
    st.title("FC360 PRO")
    db = Session()
    u = db.query(User).get(st.session_state.user_id)
    st.caption(f"Member: {u.email}")
    st.write(f"Team: **{u.team.name if u.team else 'Free Agent'}**")
    
    if st.button("Logout", use_container_width=True):
        st.session_state.authenticated = False; st.rerun()

    # --- ADMIN SUITE LOGIC ---
    if st.session_state.role == "admin":
        st.divider()
        admin_mode = st.toggle("🛠️ Enable Admin Management")
        if admin_mode:
            st.title("Admin Console")
            
            # Match Upload
            with st.expander("📤 Upload New Match", expanded=True):
                t_in = st.text_input("Team Name"); opp = st.text_input("Opponent")
                v_f = st.file_uploader("Video", type=['mp4','mov']); x_f = st.file_uploader("XML", type=['xml'])
                if st.button("🚀 Start Upload"):
                    if t_in and v_f and x_f:
                        u_url, u_id = create_mux_upload()
                        if u_url:
                            if requests.put(u_url, data=v_f).status_code == 200:
                                team = db.query(Team).filter(Team.name == t_in).first() or Team(name=t_in)
                                if not team.id: db.add(team); db.flush()
                                match = Match(opponent=opp, team_id=team.id, mux_asset_id=u_id, status='processing')
                                db.add(match); db.flush()
                                for ev in parse_xml(x_f.read()): db.add(Event(match_id=match.id, **ev))
                                db.commit(); st.success("Uploaded! Use Sync Status in 1 min.")
            
            if st.button("🔄 Sync Mux Status", use_container_width=True): update_processing_matches()
            
            # User Management
            with st.expander("👥 Assign Teams"):
                users = db.query(User).filter(User.role == 'user').all()
                teams = db.query(Team).all()
                t_list = [t.name for t in teams]
                for user in users:
                    col1, col2 = st.columns([2,1])
                    col1.write(user.email)
                    target = col2.selectbox("Team", t_list, key=f"u{user.id}")
                    if col2.button("Apply", key=f"b{user.id}"):
                        user.team_id = db.query(Team).filter(Team.name == target).first().id
                        db.commit(); st.rerun()
            
            db.close(); st.stop() # Prevents Analysis UI from loading while in Admin Mode

# --- SIDE-BY-SIDE ANALYSIS VIEW ---
st.title("Performance Review Room")
col_vid, col_events = st.columns([3, 2]) # 60/40 Split

with col_events:
    st.subheader("Match Timeline")
    tags = db.query(Event.tag).distinct().all()
    sel_tag = st.selectbox("Action Type:", ["All"] + sorted([t[0] for t in tags]))
    
    with st.container(height=600, border=True):
        query = db.query(Event, Match).join(Match)
        if u.role != "admin": query = query.filter(Match.team_id == u.team_id)
        if sel_tag != "All": query = query.filter(Event.tag == sel_tag)
        
        for ev, mt in query.all():
            if mt.status == 'ready':
                with st.container(border=True):
                    c1, c2 = st.columns([4, 1])
                    c1.write(f"**{ev.tag}** ({ev.player})")
                    c1.caption(f"Match vs {mt.opponent}")
                    if c2.button("▶️", key=f"play_{ev.id}"):
                        st.session_state.active_video = f"https://stream.mux.com/{mt.mux_playback_id}/low.mp4#t={ev.start_ms//1000}"
                        st.rerun()

with col_vid:
    st.subheader("Video Analysis")
    if st.session_state.active_video:
        st.video(st.session_state.active_video)
    else:
        st.info("Select a clip on the right to begin analysis.")

db.close()