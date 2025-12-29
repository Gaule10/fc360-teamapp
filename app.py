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

def apply_custom_css():
    st.markdown("""
        <style>
        [data-testid="stMetricValue"] { font-size: 24px; color: #00FFCC; }
        .stButton>button { width: 100%; border-radius: 5px; background-color: #1E1E1E; border: 1px solid #333; transition: 0.3s; }
        .stButton>button:hover { border-color: #00FFCC; color: #00FFCC; }
        .clip-card { padding: 10px; background: #111; border-left: 3px solid #00FFCC; margin-bottom: 5px; border-radius: 4px; }
        </style>
    """, unsafe_allow_html=True)

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
        if st.button("Sign In"):
            user = db.query(User).filter(User.email == e, User.password == hash_password(p)).first()
            if user:
                st.session_state.update({"authenticated": True, "user_id": user.id, "role": user.role})
                st.rerun()
            else: st.error("Access Denied")
    with t2:
        ne = st.text_input("New Email"); np = st.text_input("New Password", type="password")
        if st.button("Register as User"):
            try:
                # Forced role to 'user' - No selection option for security
                db.add(User(email=ne, password=hash_password(np), role='user'))
                db.commit(); st.success("Account created! Go to Login.")
            except: st.error("Email already in use.")
    db.close(); st.stop()

# --- MAIN APP LAYOUT ---
st.set_page_config(page_title="FC360 Pro", layout="wide")
apply_custom_css()

with st.sidebar:
    st.image("https://via.placeholder.com/150x50.png?text=FC360+PRO", use_container_width=True)
    db = Session()
    u = db.query(User).get(st.session_state.user_id)
    st.caption(f"Logged in as: {u.email}")
    st.write(f"Team: **{u.team.name if u.team else 'Free Agent'}**")
    
    if st.button("Logout"):
        st.session_state.authenticated = False; st.rerun()

    # HIDDEN ADMIN SECTION
    if st.session_state.role == "admin":
        st.divider()
        if st.toggle("🛠️ Enable Admin Management"):
            st.subheader("Admin Suite")
            # Place Admin Match Upload/User Logic here...
            st.info("Admin mode enabled. Return to User mode for analysis.")
            # Match upload logic would go here
            db.close(); st.stop() 

# --- SIDE-BY-SIDE ANALYSIS VIEW ---
st.title("Performance Analysis")
col_vid, col_events = st.columns([3, 2]) # 60/40 Split

with col_events:
    st.subheader("Match Events")
    # Filters
    tags = db.query(Event.tag).distinct().all()
    sel_tag = st.selectbox("Filter Action:", ["All Actions"] + sorted([t[0] for t in tags]))
    
    # Scrollable Timeline Container
    with st.container(height=650, border=True):
        query = db.query(Event, Match).join(Match)
        if u.role != "admin": query = query.filter(Match.team_id == u.team_id)
        if sel_tag != "All Actions": query = query.filter(Event.tag == sel_tag)
        
        for ev, mt in query.all():
            if mt.status == 'ready':
                st.markdown(f'''<div class="clip-card"><b>{ev.tag}</b><br><small>{ev.player} vs {mt.opponent}</small></div>''', unsafe_allow_html=True)
                # Video Trigger Button
                seek = ev.start_ms // 1000
                v_url = f"https://stream.mux.com/{mt.mux_playback_id}/low.mp4#t={seek}"
                if st.button(f"Play Clip", key=f"btn_{ev.id}"):
                    st.session_state.active_video = v_url; st.rerun()

with col_vid:
    st.subheader("Review Room")
    if st.session_state.active_video:
        st.video(st.session_state.active_video)
        st.caption("Press 'L' to skip forward 10s or 'J' to skip back.")
    else:
        st.image("https://via.placeholder.com/800x450.png?text=Select+a+clip+to+start+analysis")

db.close()