import streamlit as st
import sqlite3
import pandas as pd
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# ==========================================
# 1. CONFIGURACIÓN DE BASE DE DATOS
# ==========================================
def init_db():
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    
    # Tabla Usuarios
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  username TEXT UNIQUE, 
                  password TEXT, 
                  role TEXT, 
                  rupias INTEGER, 
                  total_points REAL DEFAULT 0)''')
    
    # Tabla Futbolistas
    c.execute('''CREATE TABLE IF NOT EXISTS players 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  name TEXT, 
                  position TEXT, 
                  price INTEGER, 
                  points REAL DEFAULT 0)''')
    
    # Tabla Plantillas de usuarios
    c.execute('''CREATE TABLE IF NOT EXISTS user_squads 
                 (user_id INTEGER, 
                  player_id INTEGER, 
                  is_starter BOOLEAN,
                  FOREIGN KEY(user_id) REFERENCES users(id), 
                  FOREIGN KEY(player_id) REFERENCES players(id))''')
                  
    # Tabla Partidos
    c.execute('''CREATE TABLE IF NOT EXISTS matches 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  rival TEXT, 
                  match_date TEXT, 
                  match_time TEXT, 
                  status TEXT DEFAULT 'PENDIENTE')''')

    # Tabla Puntuaciones de Usuarios (1 a 5 estrellas)
    c.execute('''CREATE TABLE IF NOT EXISTS ratings 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  user_id INTEGER, 
                  player_id INTEGER, 
                  match_id INTEGER, 
                  rating INTEGER,
                  FOREIGN KEY(user_id) REFERENCES users(id), 
                  FOREIGN KEY(player_id) REFERENCES players(id), 
                  FOREIGN KEY(match_id) REFERENCES matches(id),
                  UNIQUE(user_id, player_id, match_id))''')
        
    conn.commit()
    conn.close()

# ==========================================
# 2. FUNCIONES DE BASE DE DATOS Y REGLAS LALIGA
# ==========================================
def run_query(query, params=()):
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    data = c.fetchall()
    conn.close()
    return data

def get_dataframe(query, params=()):
    conn = sqlite3.connect('fantasy.db')
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

def calculate_laliga_points(pos, goles, asistencias, porteria_cero, penalti_parado, penalti_fallado):
    pts = 0
    if pos in ['POR', 'DEF']:
        pts += goles * 6
    elif pos == 'MED':
        pts += goles * 5
    elif pos == 'DEL':
        pts += goles * 4
        
    pts += asistencias * 3
    
    if porteria_cero:
        if pos in ['POR', 'DEF']:
            pts += 4
        elif pos == 'MED':
            pts += 1
            
    if pos == 'POR':
        pts += penalti_parado * 5
        
    pts -= penalti_fallado * 2
    return pts

def format_countdown(td):
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0:
        return "00h 00m 00s"
    
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    
    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"

# ==========================================
# 3. BANNER DEL PRÓXIMO PARTIDO
# ==========================================
def show_next_match_banner():
    next_match = run_query("SELECT rival, match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY id DESC LIMIT 1")
    if next_match:
        rival, match_date, match_time = next_match[0]
        st.info(f"⚽ **Próximo Partido:** vs **{rival}** | 📅 **Fecha:** {match_date} | 🕒 **Hora:** {match_time} — *Fecha límite para alinear tus 4 titulares*")
    else:
        st.warning("⚠️ No hay ningún partido programado en este momento.")

# ==========================================
# 4. AUTENTICACIÓN Y REGISTRO INTELIGENTE
# ==========================================
def login_user(username, password):
    user = run_query("SELECT id, password, role, rupias FROM users WHERE username=?", (username,))
    if user and check_password_hash(user[0][1], password):
        st.session_state['user_id'] = user[0][0]
        st.session_state['username'] = username
        st.session_state['role'] = user[0][2]
        st.session_state['logged_in'] = True
        st.rerun()
    else:
        st.error("Usuario o contraseña incorrectos")

def register_user(username, password):
    try:
        hashed_pw = generate_password_hash(password)
        users_count = run_query("SELECT COUNT(*) FROM users")[0][0]
        
        if users_count == 0:
            run_query("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, ?, ?, 0)", 
                      (username, hashed_pw, 'admin', 999))
            st.success("👑 ¡Cuenta creada! Eres el primer usuario y se te ha asignado el rol de ADMINISTRADOR.")
        else:
            run_query("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, ?, ?, 0)", 
                      (username, hashed_pw, 'user', 20))
            st.success("✅ Cuenta de jugador creada exitosamente. ¡Ya puedes iniciar sesión!")
            
    except sqlite3.IntegrityError:
        st.error("El nombre de usuario ya existe. Por favor, elige otro.")

# ==========================================
# 5. PANELES DE LA APP
# ==========================================
def admin_panel():
    show_next_match_banner()
    st.header("👑 Panel de Administrador")
    st.write("Bienvenido. Como administrador, **no participas en la liga** y este es tu panel exclusivo de gestión.")
    
    tab1, tab2, tab3 = st.tabs(["📅 Partidos y Horarios", "⚽ Jugadores del Club", "📊 Carga de Estadísticas y Puntos"])
    
    with tab1:
        st.subheader("Programar Nuevo Partido (Fecha Límite)")
        with st.form("add_match"):
            rival = st.text_input("Nombre del Rival")
            match_date = st.date_input("Fecha del Partido")
            match_time = st.time_input("Hora del Partido")
            submit_match = st.form_submit_button("Guardar Partido")
            
            if submit_match and rival:
                run_query("INSERT INTO matches (rival, match_date, match_time, status) VALUES (?, ?, ?, 'PENDIENTE')",
                          (rival, str(match_date), str(match_time)))
                st.success(f"Partido contra {rival} programado.")
                st.rerun()
                
        st.subheader("Historial de Partidos")
        matches_df = get_dataframe("SELECT id AS 'ID', rival AS 'Rival', match_date AS 'Fecha', match_time AS 'Hora', status AS 'Estado' FROM matches ORDER BY id DESC")
        st.dataframe(matches_df, use_container_width=True)

    with tab2:
        st.subheader("Añadir Nuevo Futbolista")
        with st.form("add_player"):
            name = st.text_input("Nombre del jugador")
            position = st.selectbox("Posición", ["POR", "DEF", "MED", "DEL"])
            price = st.number_input("Precio Inicial (Rupias)", min_value=1, value=5)
            submitted = st.form_submit_button("Añadir Futbolista")
            if submitted and name:
                run_query("INSERT INTO players (name, position, price, points) VALUES (?, ?, ?, 0)", 
                          (name, position, price))
                st.success(f"{name} añadido al juego.")
                st.rerun()
                
        st.subheader("Base de Datos de Futbolistas")
        players_df = get_dataframe("SELECT id AS 'ID', name AS 'Nombre', position AS 'Posición', price AS 'Precio (Rupias)', ROUND(points, 2) AS 'Puntos Totales' FROM players")
        st.dataframe(players_df, use_container_width=True)

    with tab3:
        st.subheader("⚽ Introducción de Estadísticas de la Jornada")
        st.write("Selecciona el partido finalizado para introducir las estadísticas oficiales de los futbolistas (LaLiga Fantasy) y sumar la **nota media asignada por los usuarios**.")
        
        matches_list = get_dataframe("SELECT id, rival, match_date, status FROM matches ORDER BY id DESC")
        if matches_list.empty:
            st.info("No hay partidos programados.")
        else:
            m_options = {f"vs {m['rival']} ({m['match_date']}) - [{m['status']}]": m['id'] for idx, m in matches_list.iterrows()}
            sel_m_label = st.selectbox("Selecciona partido a contabilizar:", list(m_options.keys()))
            selected_match_id = m_options[sel_m_label]
            
            players = get_dataframe("SELECT id, name, position FROM players")
            if players.empty:
                st.info("Añade primero futbolistas en la pestaña 'Jugadores del Club'.")
            else:
                with st.form("match_stats_form"):
                    player_stats_inputs = {}
                    for idx, row in players.iterrows():
                        st.markdown(f"#### 👤 {row['name']} ({row['position']})")
                        c1, c2, c3, c4, c5 = st.columns(5)
                        goles = c1.number_input("Goles", min_value=0, value=0, key=f"gol_{row['id']}_{selected_match_id}")
                        asist = c2.number_input("Asistencias", min_value=0, value=0, key=f"ast_{row['id']}_{selected_match_id}")
                        p_cero = c3.checkbox("Portería a cero", value=False, key=f"pc_{row['id']}_{selected_match_id}")
                        
                        pen_parado = 0
                        if row['position'] == 'POR':
                            pen_parado = c4.number_input("Penaltis Parados", min_value=0, value=0, key=f"pp_{row['id']}_{selected_match_id}")
                        pen_fallado = c5.number_input("Penaltis Fallados", min_value=0, value=0, key=f"pf_{row['id']}_{selected_match_id}")
                        
                        player_stats_inputs[row['id']] = {
                            "pos": row['position'], "goles": goles, "asistencias": asist,
                            "porteria_cero": p_cero, "penalti_parado": pen_parado, "penalti_fallado": pen_fallado
                        }
                        st.divider()

                    submit_stats = st.form_submit_button("🔥 Procesar Jornada y Sumar Puntos")
                    if submit_stats:
                        for p_id, stats in player_stats_inputs.items():
                            stat_pts = calculate_laliga_points(stats['pos'], stats['goles'], stats['asistencias'], stats['porteria_cero'], stats['penalti_parado'], stats['penalti_fallado'])
                            avg_res = run_query("SELECT AVG(rating) FROM ratings WHERE player_id=? AND match_id=?", (p_id, selected_match_id))[0][0]
                            user_avg_pts = round(avg_res, 2) if avg_res is not None else 0.0
                            
                            total_match_pts = stat_pts + user_avg_pts
                            if total_match_pts != 0:
