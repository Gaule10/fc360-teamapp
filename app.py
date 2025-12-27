import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import mux_python
import os
from dotenv import load_dotenv
import requests
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import chardet
import hashlib

# --- 1. DATABASE & CONFIG SETUP ---
# Supports both local .env and Streamlit Cloud Secrets
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
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True)
    matches = relationship("Match", back_populates="team")
    users = relationship("User", back_populates="team")

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    email = Column(String(100), unique=True)
    password = Column(String(100)) 
    role = Column(String(20), default='user') 
    team_id = Column(Integer, ForeignKey('teams.id'), nullable=True)
    team = relationship("Team", back_populates="users")

class Match(Base):
    __tablename__ = 'matches'
    id = Column(Integer, primary_key=True)
    opponent = Column(String(200))
    match_date = Column(String(50))
    team_id = Column(Integer, ForeignKey('teams.id'))
    mux_asset_id = Column(String(100))
    mux_playback_id = Column(String(100))
    status = Column(String(50), default='uploading')
    team = relationship("Team", back_populates="matches")
    events = relationship("Event", back_populates="match", cascade="all, delete-orphan")

class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey('matches.id'))
    tag = Column(String(100))
    player = Column(String(100))
    start_ms = Column(Integer)
    end_ms = Column(Integer)
    match = relationship("Match", back_populates="events")

# Database Engine Selection (Postgres for Cloud, SQLite for Local)
if DB_URL and DB_URL.startswith("postgres"):
    # SQLAlchemy requires "postgresql://" not "postgres://"
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DB_URL)
else:
    engine = create_engine('sqlite:///soccer_mux.db', connect_args={"check_same_thread": False})

# Auto-migration to ensure schema is always up to date
def run_migrations():
    with engine.connect() as conn:
        try:
            inspector = conn.execute(text("PRAGMA table_info(users)")).fetchall()
            columns = [row[1] for row in inspector]
            if 'team_id' not in columns and 'team_id' != "":
                 conn.execute(text("ALTER TABLE users ADD COLUMN team_id INTEGER REFERENCES teams(id)"))
                 conn.commit()
        except:
            pass # Skip if on Postgres (different syntax)

Base.metadata.create_all(engine)
run_migrations()
Session = sessionmaker(bind=engine)

# --- 2. HELPERS ---
def hash_password(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def parse_xml(xml_bytes):
    try:
        detected = chardet.detect(xml_bytes)
        encoding = detected['encoding'] or 'utf-8'
        root = ET.fromstring(xml_bytes.decode(encoding).strip())
        events = []
        for instance in root.findall('.//instance'):
            start = instance.find('start'); end = instance.find('end')
            code = instance.find('code'); label = instance.find('.//label/text')
            if start is not None and end is not None:
                events.append({
                    'tag': code.text if code is not None else 'Unknown',
                    'player': label.text if label is not None else '',
                    'start_ms': int(float(start.text) * 1000),
                    'end_ms': int(float(end.text) * 1000),
                })
        return events
    except Exception as e:
        st.error(f"XML Error: {e}"); return []

def create_mux_upload():
    try:
        api_client = mux_python.ApiClient(configuration)
        direct_uploads_api = mux_python.DirectUploadsApi(api_client)
        create_asset_request = mux_python.CreateAssetRequest(playback_policy=[mux_python.PlaybackPolicy.PUBLIC])
        create_upload_request = mux_python.CreateUploadRequest(
            new_asset_settings=create_asset_request, cors_origin="*", timeout=3600
        )
        upload = direct_uploads_api.create_direct_upload(create_upload_request)
        return upload.data.url, upload.data.id
    except Exception as e:
        st.error(f"Mux API Error: {e}"); return None, None

def update_processing_matches():
    session = Session()
    processing = session.query(Match).filter(Match.status.in_(['processing', 'uploading'])).all()
    if not processing: 
        st.toast("Everything is synced!"); session.close(); return
        
    api_client = mux_python.ApiClient(configuration)
    uploads_api = mux_python.DirectUploadsApi(api_client)
    assets_api = mux_python.AssetsApi(api_client)
    
    for m in processing:
        try:
            up = uploads_api.get_direct_upload(m.mux_asset_id)
            if up.data.asset_id:
                asset = assets_api.get_asset(up.data.asset_id)
                if asset.data.status == 'ready':
                    m.mux_asset_id = up.data.asset_id
                    m.mux_playback_id = asset.data.playback_ids[0].id
                    m.status = 'ready'
                    st.success(f"Linked: {m.opponent} is READY")
        except: continue
    session.commit(); session.close(); st.rerun()

# --- 3. LOGIN & REGISTRATION ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
    st.session_state.user_id = None
    st.session_state.role = None
    st.session_state.active_video = None

if not st.session_state.authenticated:
    st.title("⚽ FC360 Analysis Login")
    t1, t2 = st.tabs(["Login", "Register"])
    db = Session()
    with t1:
        e = st.text_input("Email"); p = st.text_input("Password", type="password")
        if st.button("Login", use_container_width=True):
            user = db.query(User).filter(User.email == e, User.password == hash_password(p)).first()
            if user:
                st.session_state.authenticated = True
                st.session_state.user_id = user.id
                st.session_state.role = user.role
                st.rerun()
            else: st.error("Invalid credentials")
    with t2:
        ne = st.text_input("New Email"); np = st.text_input("New Password", type="password")
        nr = st.selectbox("Role", ["user", "admin"])
        if st.button("Create Account", use_container_width=True):
            try:
                db.add(User(email=ne, password=hash_password(np), role=nr))
                db.commit(); st.success("Success! Please login.")
            except: st.error("Account creation failed.")
    db.close(); st.stop()

# --- 4. AUTHENTICATED UI ---
st.set_page_config(page_title="FC360 Pro Analysis", layout="wide")

with st.sidebar:
    st.title("FC360 Pro")
    db = Session()
    u = db.query(User).get(st.session_state.user_id)
    st.write(f"**{u.email}** ({u.role})")
    st.write(f"Team: {u.team.name if u.team else 'Unassigned'}")
    
    if st.button("Logout", use_container_width=True):
        st.session_state.authenticated = False; st.rerun()
    
    if st.session_state.role == "admin":
        st.divider()
        adm_tabs = st.tabs(["📤 Match", "👥 Users"])
        
        with adm_tabs[0]:
            team_in = st.text_input("Team Name (e.g. NYCFC)")
            opp = st.text_input("Opponent")
            v_file = st.file_uploader("Video", type=['mp4','mov'])
            x_file = st.file_uploader("XML", type=['xml'])
            
            if st.button("🚀 Start Admin Upload", use_container_width=True):
                if team_in and v_file and x_file:
                    bar = st.progress(0, text="Requesting Mux URL...")
                    url, up_id = create_mux_upload()
                    if url:
                        bar.progress(30, text="Uploading Video... (Keep window open)")
                        resp = requests.put(url, data=v_file)
                        if resp.status_code == 200:
                            bar.progress(80, text="Finalizing database...")
                            team = db.query(Team).filter(Team.name == team_in).first()
                            if not team:
                                team = Team(name=team_in); db.add(team); db.flush()
                            
                            new_match = Match(opponent=opp, team_id=team.id, mux_asset_id=up_id, status='processing')
                            db.add(new_match); db.flush()
                            for ev in parse_xml(x_file.read()):
                                db.add(Event(match_id=new_match.id, **ev))
                            db.commit(); bar.progress(100, text="Upload Finished!")
                            st.success("Success! Please wait 1-2 mins, then Sync.")
                else: st.warning("All fields required.")
            
            if st.button("🔄 Sync Mux Status", use_container_width=True):
                update_processing_matches()

        with adm_tabs[1]:
            st.subheader("Team Assignments")
            users = db.query(User).filter(User.role == 'user').all()
            teams = db.query(Team).all()
            t_names = [t.name for t in teams]
            for user in users:
                with st.container(border=True):
                    st.write(user.email)
                    idx = t_names.index(user.team.name) if user.team else 0
                    sel = st.selectbox("Assign To", t_names, index=idx, key=f"s{user.id}")
                    if st.button("Apply", key=f"a{user.id}"):
                        user.team_id = db.query(Team).filter(Team.name == sel).first().id
                        db.commit(); st.rerun()
    db.close()

# --- 5. MAIN CONTENT (Side-by-Side View) ---
st.title("🎬 Performance Review")
db = Session()
u = db.query(User).get(st.session_state.user_id)

if u.role == "admin":
    tags_query = db.query(Event.tag).distinct()
else:
    tags_query = db.query(Event.tag).join(Match).filter(Match.team_id == u.team_id).distinct()

tag_list = sorted([t[0] for t in tags_query.all()])
sel_tag = st.selectbox("Select Action to Analyze:", [""] + tag_list)

if sel_tag:
    v_col, t_col = st.columns([3, 2])
    with t_col:
        st.subheader("Event Timeline")
        query = db.query(Event, Match).join(Match).filter(Event.tag == sel_tag)
        if u.role != "admin": query = query.filter(Match.team_id == u.team_id)
        
        with st.container(height=600, border=True):
            for ev, mt in query.all():
                if mt.status == 'ready':
                    with st.container(border=True):
                        c1, c2 = st.columns([4, 1])
                        c1.write(f"**{ev.tag}**")
                        c1.caption(f"Player: {ev.player} | vs {mt.opponent}")
                        
                        seek = ev.start_ms // 1000
                        # We use the Playback ID from Mux to generate the stream URL
                        url = f"https://stream.mux.com/{mt.mux_playback_id}/low.mp4#t={seek}"
                        if c2.button("▶️", key=f"v{ev.id}"):
                            st.session_state.active_video = url; st.rerun()
                else:
                    st.caption(f"Match vs {mt.opponent} is still processing...")

    with v_col:
        st.subheader("Video Player")
        if st.session_state.active_video:
            st.video(st.session_state.active_video)
        else:
            st.info("Select an action on the right to start your review.")
db.close()