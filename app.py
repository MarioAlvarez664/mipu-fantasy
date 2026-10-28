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
    
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, role TEXT, rupias INTEGER, total_points REAL DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS players 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, position TEXT, price INTEGER, points REAL DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_squads 
                 (user_id INTEGER, player_id INTEGER, is_starter BOOLEAN,
                  FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(player_id) REFERENCES players(id))''')
                  
    c.execute('''CREATE TABLE IF NOT EXISTS matches 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, rival TEXT, match_date TEXT, match_time TEXT, status TEXT DEFAULT 'PENDIENTE')''')

    c.execute('''CREATE TABLE IF NOT EXISTS ratings 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, player_id INTEGER, match_id INTEGER, rating INTEGER,
                  FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(player_id) REFERENCES players(id), FOREIGN KEY(match_id) REFERENCES matches(id),
                  UNIQUE(user_id, player_id, match_id))''')
    
    # USUARIO MAESTRO FIJO REQUERIDO
    c.execute("SELECT * FROM users WHERE username='usuariomaestro'")
    if not c.fetchone():
        hashed_pw = generate_password_hash("1234")
        c.execute("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, ?, ?, ?)", 
                  ('usuariomaestro', hashed_pw, 'admin', 9999, 0))
        
    conn.commit()
    conn.close()

# ==========================================
# 2. FUNCIONES GENERALES
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
    if pos in ['POR', 'DEF']: pts += goles * 6
    elif pos == 'MED': pts += goles * 5
    elif pos == 'DEL': pts += goles * 4
        
    pts += asistencias * 3
    
    if porteria_cero:
        if pos in ['POR', 'DEF']: pts += 4
        elif pos == 'MED': pts += 1
            
    if pos == 'POR': pts += penalti_parado * 5
    pts -= penalti_fallado * 2
    return pts

def format_countdown(td):
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0: return "00h 00m 00s"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    if days > 0: return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"

def is_market_locked():
    """Bloquea los fichajes y cambios de plantilla si el partido ya ha empezado."""
    next_match = run_query("SELECT match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY id DESC LIMIT 1")
    if next_match:
        try:
            date_str, time_str = next_match[0]
            time_str = str(time_str).split('.')[0]
            dt_str = f"{date_str} {time_str}"
            fmt = "%Y-%m-%d %H:%M:%S" if len(time_str.split(':')) == 3 else "%Y-%m-%d %H:%M"
            match_dt = datetime.strptime(dt_str, fmt)
            if datetime.now() >= match_dt:
                return True
        except:
            pass
    return False

# ==========================================
# 3. AUTENTICACIÓN
# ==========================================
def login_user(username, password):
    user = run_query("SELECT id, password, role, rupias FROM users WHERE username=?", (username.lower(),))
    if user and check_password_hash(user[0][1], password):
        st.session_state['user_id'] = user[0][0]
        st.session_state['username'] = username
        st.session_state['role'] = user[0][2]
        st.session_state['logged_in'] = True
        st.rerun()
    else:
        st.error("Usuario o contraseña incorrectos")

def register_user(username, password):
    if username.lower() == 'usuariomaestro':
        st.error("Ese nombre de usuario está reservado.")
        return
    try:
        hashed_pw = generate_password_hash(password)
        # Todos los nuevos son usuarios normales con 20 rupias
        run_query("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, ?, ?, 0)", 
                  (username.lower(), hashed_pw, 'user', 20))
        st.success("✅ Cuenta creada exitosamente. ¡Inicia sesión para jugar!")
    except sqlite3.IntegrityError:
        st.error("El nombre de usuario ya existe. Por favor, elige otro.")

# ==========================================
# 4. PANELES - MAESTRO Y USUARIO
# ==========================================
def admin_panel():
    # Banner Admin
    next_match = run_query("SELECT rival, match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY id DESC LIMIT 1")
    if next_match:
        st.info(f"⚽ **Partido Activo:** vs **{next_match[0][0]}** ({next_match[0][1]} a las {next_match[0][2]})")
        
    st.header("👑 Panel del Usuario Maestro")
    st.write("Panel exclusivo de gestión. Tú eres quien controla la liga, añade jugadores y contabiliza puntos.")
    
    tab1, tab2, tab3 = st.tabs(["📅 Subir Partidos", "⚽ Añadir Jugadores", "📊 Subir Estadísticas"])
    
    with tab1:
        st.subheader("Programar Nuevo Partido")
        with st.form("add_match"):
            rival = st.text_input("Nombre del Equipo Rival")
            match_date = st.date_input("Fecha del Partido")
            match_time = st.time_input("Hora del Partido")
            if st.form_submit_button("Guardar Partido") and rival:
                run_query("INSERT INTO matches (rival, match_date, match_time, status) VALUES (?, ?, ?, 'PENDIENTE')", (rival, str(match_date), str(match_time)))
                st.success(f"Partido contra {rival} programado.")
                st.rerun()
        
        matches_df = get_dataframe("SELECT id AS 'ID', rival AS 'Rival', match_date AS 'Fecha', match_time AS 'Hora', status AS 'Estado' FROM matches ORDER BY id DESC")
        st.dataframe(matches_df, use_container_width=True)

    with tab2:
        st.subheader("Dar de alta a un Futbolista")
        with st.form("add_player"):
            name = st.text_input("Nombre del jugador")
            position = st.selectbox("Posición", ["POR", "DEF", "MED", "DEL"])
            price = st.number_input("Precio Inicial (Rupias)", min_value=1, value=5)
            if st.form_submit_button("Añadir al Mercado") and name:
                run_query("INSERT INTO players (name, position, price, points) VALUES (?, ?, ?, 0)", (name, position, price))
                st.success(f"{name} añadido al juego.")
                st.rerun()
                
        players_df = get_dataframe("SELECT id AS 'ID', name AS 'Nombre', position AS 'Posición', price AS 'Precio (Rupias)', ROUND(points, 2) AS 'Puntos Totales' FROM players")
        st.dataframe(players_df, use_container_width=True)

    with tab3:
        st.subheader("⚽ Introducción de Estadísticas")
        matches_list = get_dataframe("SELECT id, rival, match_date, status FROM matches ORDER BY id DESC")
        if matches_list.empty:
            st.info("No hay partidos programados.")
        else:
            m_options = {f"vs {m['rival']} ({m['match_date']}) - [{m['status']}]": m['id'] for idx, m in matches_list.iterrows()}
            selected_match_id = m_options[st.selectbox("Selecciona partido a contabilizar:", list(m_options.keys()))]
            
            players = get_dataframe("SELECT id, name, position FROM players")
            if not players.empty:
                with st.form("match_stats_form"):
                    player_stats_inputs = {}
                    for idx, row in players.iterrows():
                        st.markdown(f"#### 👤 {row['name']} ({row['position']})")
                        c1, c2, c3, c4, c5 = st.columns(5)
                        goles = c1.number_input("Goles", min_value=0, value=0, key=f"gol_{row['id']}")
                        asist = c2.number_input("Asist", min_value=0, value=0, key=f"ast_{row['id']}")
                        p_cero = c3.checkbox("Portería 0", key=f"pc_{row['id']}")
                        pen_parado = c4.number_input("Pen Parados", min_value=0, value=0, key=f"pp_{row['id']}") if row['position'] == 'POR' else 0
                        pen_fallado = c5.number_input("Pen Fallados", min_value=0, value=0, key=f"pf_{row['id']}")
                        
                        player_stats_inputs[row['id']] = {"pos": row['position'], "goles": goles, "asistencias": asist, "porteria_cero": p_cero, "penalti_parado": pen_parado, "penalti_fallado": pen_fallado}
                        st.divider()

                    if st.form_submit_button("🔥 Procesar Jornada y Sumar Puntos"):
                        for p_id, stats in player_stats_inputs.items():
                            stat_pts = calculate_laliga_points(stats['pos'], stats['goles'], stats['asistencias'], stats['porteria_cero'], stats['penalti_parado'], stats['penalti_fallado'])
                            avg_res = run_query("SELECT AVG(rating) FROM ratings WHERE player_id=? AND match_id=?", (p_id, selected_match_id))[0][0]
                            user_avg_pts = round(avg_res, 2) if avg_res is not None else 0.0
                            
                            total_match_pts = stat_pts + user_avg_pts
                            if total_match_pts != 0:
                                run_query("UPDATE players SET points = points + ? WHERE id = ?", (total_match_pts, p_id))
                                starters = run_query("SELECT user_id FROM user_squads WHERE player_id = ? AND is_starter = 1", (p_id,))
                                for u in starters:
                                    run_query("UPDATE users SET total_points = total_points + ? WHERE id = ?", (total_match_pts, u[0]))
                                    
                        run_query("UPDATE matches SET status = 'FINALIZADO' WHERE id = ?", (selected_match_id,))
                        st.success("¡Puntos de la jornada sumados con éxito a la clasificación!")
                        st.rerun()

def user_panel():
    user_id = st.session_state['user_id']
    rupias = run_query("SELECT rupias FROM users WHERE id=?", (user_id,))[0][0]
    
    # Banner y bloqueo
    market_locked = is_market_locked()
    next_match = run_query("SELECT rival, match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY id DESC LIMIT 1")
    if next_match:
        if market_locked:
            st.error(f"🔒 **Partido en juego o finalizado** (vs {next_match[0][0]}). Plantillas y Mercado bloqueados hasta el reparto de puntos.")
        else:
            st.info(f"⚽ **Próximo Partido:** vs **{next_match[0][0]}** | 📅 {next_match[0][1]} | 🕒 {next_match[0][2]} — *Tienes hasta esta hora para hacer cambios*")
    
    st.sidebar.title(f"👤 {st.session_state['username'].capitalize()}")
    st.sidebar.subheader(f"💰 Tus Rupias: {rupias}")
    
    menu = st.sidebar.radio("Navegación", ["Mi Plantilla", "Mercado", "⭐ Votar Jugadores", "🏆 Clasificación"])
    
    if menu == "⭐ Votar Jugadores":
        st.header("⭐ Votación de Jugadores")
        st.write("La votación abre **5 horas después** del inicio y cierra a las **48 horas**.")
        matches_df = get_dataframe("SELECT id, rival, match_date, match_time FROM matches ORDER BY id DESC")
        if matches_df.empty:
            st.info("No hay partidos.")
        else:
            now = datetime.now()
            match_options, match_status_dict = {}, {}
            for idx, row in matches_df.iterrows():
                try:
                    time_str = str(row['match_time']).split('.')[0]
                    dt_str = f"{row['match_date']} {time_str}"
                    match_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S") if len(time_str.split(':')) == 3 else datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    
                    t_start, t_end = match_dt + timedelta(hours=5), match_dt + timedelta(hours=48)
                    match_options[f"vs {row['rival']} ({row['match_date']})"] = row['id']
                    
                    if now < t_start: status, time_left = "NOT_OPEN", t_start - now
                    elif t_start <= now <= t_end: status, time_left = "OPEN", t_end - now
                    else: status, time_left = "CLOSED", timedelta(0)
                        
                    match_status_dict[row['id']] = {"status": status, "time_left": time_left, "t_start": t_start}
                except: continue

            if match_options:
                sel_m_id = match_options[st.selectbox("Selecciona partido:", list(match_options.keys()))]
                m_info = match_status_dict[sel_m_id]
                
                if m_info["status"] == "NOT_OPEN":
                    st.warning(f"⏳ Abre en: **{format_countdown(m_info['time_left'])}**")
                elif m_info["status"] == "CLOSED":
                    st.error("🔒 Votación finalizada.")
                elif m_info["status"] == "OPEN":
                    st.info(f"⏱️ **¡Abierta!** Cierra en: **{format_countdown(m_info['time_left'])}**")
                    if run_query("SELECT COUNT(*) FROM ratings WHERE user_id=? AND match_id=?", (user_id, sel_m_id))[0][0] > 0:
                        st.success("✅ Ya has votado.")
                    else:
                        players_df = get_dataframe("SELECT id, name, position FROM players")
                        if not players_df.empty:
                            with st.form("rating_form"):
                                ratings_input = {p['id']: st.slider(f"{p['name']} ({p['position']})", 1, 5, 3) for _, p in players_df.iterrows()}
                                if st.form_submit_button("Guardar Votos"):
                                    for p_id, val in ratings_input.items():
                                        run_query("INSERT OR REPLACE INTO ratings (user_id, player_id, match_id, rating) VALUES (?, ?, ?, ?)", (user_id, p_id, sel_m_id, val))
                                    st.success("¡Guardado!")
                                    st.rerun()

    elif menu == "🏆 Clasificación":
        st.header("🏆 Clasificación General")
        ranking_df = get_dataframe("SELECT username AS 'Usuario', ROUND(total_points, 2) AS 'Puntos' FROM users WHERE role != 'admin' ORDER BY total_points DESC")
        if ranking_df.empty: st.info("Aún no hay jugadores.")
        else:
            ranking_df.index = range(1, len(ranking_df) + 1)
            st.table(ranking_df)

    elif menu == "Mercado":
        st.header("🛒 Mercado de Fichajes")
        if market_locked:
            st.error("🔒 **MERCADO CERRADO.** Hay un partido en juego. Se reabrirá cuando el administrador procese los puntos.")
        else:
            market_df = get_dataframe("SELECT id, name, position, price, points FROM players WHERE id NOT IN (SELECT player_id FROM user_squads WHERE user_id=?)", (user_id,))
            squad_count = run_query("SELECT COUNT(*) FROM user_squads WHERE user_id=?", (user_id,))[0][0]
            
            for _, row in market_df.iterrows():
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.write(f"**{row['name']}** ({row['position']}) - Pts: {round(row['points'], 2)}")
                c2.write(f"🪙 {row['price']}")
                if c3.button("Fichar", key=f"buy_{row['id']}"):
                    if squad_count >= 5: st.error("Plantilla llena (Máx 5).")
                    elif rupias >= row['price']:
                        run_query("UPDATE users SET rupias = rupias - ? WHERE id = ?", (row['price'], user_id))
                        run_query("INSERT INTO user_squads (user_id, player_id, is_starter) VALUES (?, ?, ?)", (user_id, row['id'], False))
                        st.rerun()
                    else: st.error("Sin rupias.")

    elif menu == "Mi Plantilla":
        st.header("📋 Mi Plantilla")
        squad_df = get_dataframe("SELECT p.id, p.name, p.position, p.price, p.points, u.is_starter FROM players p JOIN user_squads u ON p.id = u.player_id WHERE u.user_id=?", (user_id,))
        
        if squad_df.empty:
            st.info("Aún no tienes jugadores.")
        else:
            starters_df, subs_df = squad_df[squad_df['is_starter'] == 1], squad_df[squad_df['is_starter'] == 0]
            
            # CAMPO DE FÚTBOL VISUAL
            pitch_html = """
            <div style="background: linear-gradient(180deg, #2e7d32 0%, #388e3c 100%); border: 4px solid white; border-radius: 10px; padding: 20px; position: relative; margin-bottom: 20px;">
                <div style="position: absolute; top: 50%; left: 0; right: 0; height: 2px; background: rgba(255,255,255,0.4);"></div>
                <div style="position: absolute; top: 50%; left: 50%; width: 60px; height: 60px; border: 2px solid rgba(255,255,255,0.4); border-radius: 50%; transform: translate(-50%, -50%);"></div>
                <h4 style="text-align:center; color: white; margin-top: 0;">TITULARES EN EL CAMPO</h4>
                <div style="display: flex; justify-content: space-evenly; flex-wrap: wrap; position: relative; z-index: 1; min-height: 80px;">
            """
            for _, p in starters_df.iterrows():
                pitch_html += f'<div style="background: white; color: black; padding: 5px; border-radius: 5px; text-align: center; width: 90px; margin: 5px; border-bottom: 3px solid #1b5e20;"><b>{p["position"]}</b><br><small>{p["name"]}</small></div>'
            st.markdown(pitch_html + "</div></div>", unsafe_allow_html=True)
            
            # BANQUILLO VISUAL
            if not subs_df.empty:
                bench_html = '<div style="background-color: #f0f2f6; border: 2px dashed #ccc; border-radius: 10px; padding: 15px; margin-bottom: 30px;"><h5 style="text-align:center;">🪑 BANQUILLO</h5><div style="display: flex; justify-content: space-evenly;">'
                for _, p in subs_df.iterrows(): bench_html += f'<div style="background: #ddd; color: #333; padding: 5px; border-radius: 5px; text-align: center; width: 90px;"><b>{p["position"]}</b><br><small>{p["name"]}</small></div>'
                st.markdown(bench_html + "</div></div>", unsafe_allow_html=True)

            if market_locked:
                st.error("🔒 **ALINEACIÓN CERRADA.** El partido ya ha comenzado, no puedes hacer cambios.")
            else:
                st.markdown("### ⚙️ Gestionar Plantilla")
                st.write(f"{len(starters_df)}/4 titulares seleccionados.")
                for _, row in squad_df.iterrows():
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.write(f"**{row['name']}** ({row['position']})")
                    is_titular = c2.checkbox("Al Campo", value=bool(row['is_starter']), key=f"start_{row['id']}")
                    if is_titular != bool(row['is_starter']):
                        if is_titular and len(starters_df) >= 4:
                            st.error("Ya hay 4 titulares.")
                            st.rerun()
                        else:
                            run_query("UPDATE user_squads SET is_starter=? WHERE user_id=? AND player_id=?", (is_titular, user_id, row['id']))
                            st.rerun()
                    if c3.button("Vender", key=f"sell_{row['id']}"):
                        run_query("UPDATE users SET rupias = rupias + ? WHERE id = ?", (row['price'], user_id))
                        run_query("DELETE FROM user_squads WHERE user_id=? AND player_id=?", (user_id, row['id']))
                        st.rerun()

# ==========================================
# 5. INICIO DE APP
# ==========================================
def main():
    st.set_page_config(page_title="Fantasy Local", page_icon="⚽", layout="centered")
    init_db()
    if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
        
    if not st.session_state['logged_in']:
        st.title("⚽ Fantasy Local")
        t1, t2 = st.tabs(["Iniciar Sesión", "Registrarse"])
        with t1:
            u_log = st.text_input("Usuario", key="l_u")
            p_log = st.text_input("Contraseña", type="password", key="l_p")
            if st.button("Entrar"): login_user(u_log, p_log)
        with t2:
            u_reg = st.text_input("Nuevo Usuario", key="r_u")
            p_reg = st.text_input("Nueva Contraseña", type="password", key="r_p")
            if st.button("Crear Equipo"):
                if u_reg and p_reg: register_user(u_reg, p_reg)
                else: st.error("Rellena todos los campos.")
    else:
        if st.sidebar.button("Cerrar Sesión"):
            st.session_state.clear()
            st.rerun()
        if st.session_state['role'] == 'admin': admin_panel()
        else: user_panel()

if __name__ == '__main__':
    main()
