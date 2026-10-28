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
                  
    # Añadido campo 'jornada'
    c.execute('''CREATE TABLE IF NOT EXISTS matches 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, jornada TEXT, rival TEXT, match_date TEXT, match_time TEXT, status TEXT DEFAULT 'PENDIENTE')''')

    c.execute('''CREATE TABLE IF NOT EXISTS ratings 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, player_id INTEGER, match_id INTEGER, rating INTEGER,
                  FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(player_id) REFERENCES players(id), FOREIGN KEY(match_id) REFERENCES matches(id),
                  UNIQUE(user_id, player_id, match_id))''')
                  
    # Historial de puntos para poder REVERTIR jornadas
    c.execute('''CREATE TABLE IF NOT EXISTS match_history 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, user_id INTEGER, points_awarded REAL)''')
    
    # USUARIO MAESTRO
    c.execute("SELECT * FROM users WHERE username='usuariomaestro'")
    if not c.fetchone():
        hashed_pw = generate_password_hash("1234")
        c.execute("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, ?, ?, ?)", 
                  ('usuariomaestro', hashed_pw, 'admin', 9999, 0))
        
    conn.commit()
    conn.close()

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

# ==========================================
# 2. LÓGICA DEL JUEGO
# ==========================================
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
    next_match = run_query("SELECT match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY match_date ASC, match_time ASC LIMIT 1")
    if next_match:
        try:
            d_str, t_str = next_match[0]
            t_str = str(t_str).split('.')[0]
            dt_str = f"{d_str} {t_str}"
            fmt = "%Y-%m-%d %H:%M:%S" if len(t_str.split(':')) == 3 else "%Y-%m-%d %H:%M"
            match_dt = datetime.strptime(dt_str, fmt)
            if datetime.now() >= match_dt: return True
        except: pass
    return False

# ==========================================
# 3. AUTENTICACIÓN
# ==========================================
def login_user(username, password):
    user = run_query("SELECT id, password, role FROM users WHERE username=?", (username.lower(),))
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
        st.error("Ese nombre está reservado.")
        return
    try:
        run_query("INSERT INTO users (username, password, role, rupias, total_points) VALUES (?, ?, 'user', 20, 0)", 
                  (username.lower(), generate_password_hash(password)))
        st.success("✅ Cuenta creada. ¡Inicia sesión!")
    except sqlite3.IntegrityError:
        st.error("El usuario ya existe.")

# ==========================================
# 4. PANEL DE USUARIO MAESTRO (ADMIN)
# ==========================================
def admin_panel():
    st.header("👑 Panel de Control - Modo Dios")
    
    t1, t2, t3, t4 = st.tabs(["📅 Partidos", "⚽ Jugadores", "💸 Economía y Mercado", "📊 Puntuaciones"])
    
    # ---------------- PARTIDOS ----------------
    with t1:
        st.subheader("Añadir Partido")
        with st.form("add_match"):
            c1, c2 = st.columns(2)
            jornada = c1.text_input("Jornada (ej. Jornada 1)")
            rival = c2.text_input("Rival")
            m_date = c1.date_input("Fecha")
            m_time = c2.time_input("Hora")
            if st.form_submit_button("Guardar Partido") and rival and jornada:
                run_query("INSERT INTO matches (jornada, rival, match_date, match_time) VALUES (?, ?, ?, ?)", 
                          (jornada, rival, str(m_date), str(m_time)))
                st.success("Partido añadido.")
                st.rerun()
                
        st.subheader("Gestionar Partidos")
        m_df = get_dataframe("SELECT id, jornada, rival, match_date, match_time, status FROM matches")
        st.dataframe(m_df, use_container_width=True)
        with st.expander("Borrar Partido"):
            m_id_del = st.number_input("ID del partido a borrar", min_value=0, step=1)
            if st.button("Eliminar Partido"):
                run_query("DELETE FROM matches WHERE id=?", (m_id_del,))
                st.rerun()

    # ---------------- JUGADORES ----------------
    with t2:
        st.subheader("Añadir Jugador")
        with st.form("add_player"):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Nombre")
            pos = c2.selectbox("Posición", ["POR", "DEF", "MED", "DEL"])
            price = c3.number_input("Precio Inicial", min_value=1, value=5)
            if st.form_submit_button("Añadir") and name:
                run_query("INSERT INTO players (name, position, price) VALUES (?, ?, ?)", (name, pos, price))
                st.success("Jugador añadido.")
                st.rerun()
                
        st.subheader("Base de Datos (Edición Manual)")
        p_df = get_dataframe("SELECT id, name, position, price, points FROM players")
        st.dataframe(p_df, use_container_width=True)
        with st.expander("Modificar / Borrar Jugador"):
            c1, c2, c3 = st.columns(3)
            p_id_edit = c1.number_input("ID Jugador", min_value=0, step=1)
            new_price = c2.number_input("Nuevo Precio", min_value=1, step=1)
            if c3.button("Actualizar Precio"):
                run_query("UPDATE players SET price=? WHERE id=?", (new_price, p_id_edit))
                st.rerun()
            if st.button("🗑️ Eliminar Jugador Definitivamente"):
                run_query("DELETE FROM user_squads WHERE player_id=?", (p_id_edit,))
                run_query("DELETE FROM players WHERE id=?", (p_id_edit,))
                st.warning("Jugador eliminado.")
                st.rerun()

    # ---------------- ECONOMÍA ----------------
    with t3:
        st.subheader("Fluctuación de Mercado")
        st.write("Ajusta los precios de todos los jugadores automáticamente según cuántos usuarios los tengan comprados.")
        if st.button("🔄 Ejecutar Fluctuación de Mercado"):
            players = run_query("SELECT id, price FROM players")
            for p in players:
                p_id, old_price = p
                owners = run_query("SELECT COUNT(*) FROM user_squads WHERE player_id=?", (p_id,))[0][0]
                if owners > 0: new_price = int(old_price + (old_price * 0.10 * owners)) # Sube 10% por cada dueño
                else: new_price = int(max(1, old_price - (old_price * 0.05))) # Baja 5% si nadie lo tiene
                run_query("UPDATE players SET price=? WHERE id=?", (new_price, p_id))
            st.success("Precios actualizados según demanda.")
            st.rerun()
            
        st.subheader("Modificar Dinero de Usuarios")
        u_df = get_dataframe("SELECT id, username, rupias FROM users WHERE role='user'")
        st.dataframe(u_df, use_container_width=True)
        with st.form("edit_money"):
            c1, c2 = st.columns(2)
            u_id_edit = c1.number_input("ID Usuario", min_value=0, step=1)
            new_rup = c2.number_input("Nuevas Rupias", min_value=0, step=1)
            if st.form_submit_button("Fijar Saldo"):
                run_query("UPDATE users SET rupias=? WHERE id=?", (new_rup, u_id_edit))
                st.rerun()

    # ---------------- PUNTUACIONES ----------------
    with t4:
        st.subheader("⚽ Procesar Jornada")
        pendientes = get_dataframe("SELECT id, jornada, rival FROM matches WHERE status='PENDIENTE'")
        if pendientes.empty: st.info("No hay partidos pendientes.")
        else:
            m_opts = {f"{r['jornada']} vs {r['rival']}": r['id'] for _, r in pendientes.iterrows()}
            sel_m_id = m_opts[st.selectbox("Partido:", list(m_opts.keys()))]
            
            with st.form("stats_form"):
                pl = get_dataframe("SELECT id, name, position FROM players")
                inputs = {}
                for _, r in pl.iterrows():
                    st.write(f"**{r['name']}** ({r['position']})")
                    c1,c2,c3,c4,c5 = st.columns(5)
                    inputs[r['id']] = {
                        "g": c1.number_input("Gol", 0, key=f"g{r['id']}"),
                        "a": c2.number_input("Asis", 0, key=f"a{r['id']}"),
                        "pc": c3.checkbox("Port. 0", key=f"pc{r['id']}"),
                        "pp": c4.number_input("Pen. Par", 0, key=f"pp{r['id']}") if r['position']=='POR' else 0,
                        "pf": c5.number_input("Pen. Fal", 0, key=f"pf{r['id']}")
                    }
                if st.form_submit_button("Guardar y Repartir Puntos"):
                    for p_id, s in inputs.items():
                        # Calcular puntos
                        pts = calculate_laliga_points(pl[pl['id']==p_id]['position'].values[0], s['g'], s['a'], s['pc'], s['pp'], s['pf'])
                        avg_v = run_query("SELECT AVG(rating) FROM ratings WHERE player_id=? AND match_id=?", (p_id, sel_m_id))[0][0]
                        total_pts = pts + (round(avg_v, 2) if avg_v else 0)
                        
                        if total_pts != 0:
                            run_query("UPDATE players SET points = points + ? WHERE id=?", (total_pts, p_id))
                            starters = run_query("SELECT user_id FROM user_squads WHERE player_id=? AND is_starter=1", (p_id,))
                            for u in starters:
                                run_query("UPDATE users SET total_points = total_points + ? WHERE id=?", (total_pts, u[0]))
                                run_query("INSERT INTO match_history (match_id, user_id, points_awarded) VALUES (?,?,?)", (sel_m_id, u[0], total_pts))
                                
                    run_query("UPDATE matches SET status='FINALIZADO' WHERE id=?", (sel_m_id,))
                    st.success("Jornada procesada correctamente.")
                    st.rerun()
                    
        st.subheader("⏪ Revertir Jornada (Borrar Puntos)")
        fin_matches = get_dataframe("SELECT id, jornada, rival FROM matches WHERE status='FINALIZADO'")
        if not fin_matches.empty:
            fm_opts = {f"{r['jornada']} vs {r['rival']}": r['id'] for _, r in fin_matches.iterrows()}
            rev_id = fm_opts[st.selectbox("Partido a revertir:", list(fm_opts.keys()))]
            if st.button("⚠️ Anular Puntos de este Partido"):
                history = run_query("SELECT user_id, points_awarded FROM match_history WHERE match_id=?", (rev_id,))
                for h in history: run_query("UPDATE users SET total_points = total_points - ? WHERE id=?", (h[1], h[0]))
                run_query("DELETE FROM match_history WHERE match_id=?", (rev_id,))
                run_query("UPDATE matches SET status='PENDIENTE' WHERE id=?", (rev_id,))
                st.warning("Puntos anulados. Partido devuelto a pendiente.")
                st.rerun()

# ==========================================
# 5. PANEL DE USUARIO JUGADOR
# ==========================================
def user_panel():
    u_id = st.session_state['user_id']
    rupias = run_query("SELECT rupias FROM users WHERE id=?", (u_id,))[0][0]
    locked = is_market_locked()
    
    nm = run_query("SELECT jornada, rival, match_date, match_time FROM matches WHERE status='PENDIENTE' ORDER BY match_date ASC LIMIT 1")
    if nm:
        if locked: st.error(f"🔒 **PARTIDO EN JUEGO** ({nm[0][0]} vs {nm[0][1]}). Plantilla y Mercado cerrados.")
        else: st.info(f"⚽ **Próximo:** {nm[0][0]} vs **{nm[0][1]}** | {nm[0][2]} a las {nm[0][3]}")
    
    st.sidebar.title(f"👤 {st.session_state['username'].capitalize()}")
    st.sidebar.subheader(f"💰 Rupias: {rupias}")
    
    menu = st.sidebar.radio("Menú", ["📋 Mi Plantilla", "🛒 Mercado", "⭐ Votar", "🏆 Clasificación"])
    
    if menu == "📋 Mi Plantilla":
        st.header("Mi Plantilla")
        sq = get_dataframe("SELECT p.id, p.name, p.position, p.price, p.points, u.is_starter FROM players p JOIN user_squads u ON p.id = u.player_id WHERE u.user_id=?", (u_id,))
        if sq.empty: st.info("Ve al mercado a fichar jugadores.")
        else:
            st_df = sq[sq['is_starter']==1]
            su_df = sq[sq['is_starter']==0]
            
            # --- MEDIO CAMPO VISUAL NAVY BLUE ---
            shirt_svg = '''<svg width="24" height="24" viewBox="0 0 24 24"><path fill="#000080" d="M12 2C10.3 2 9 3.3 9 5v1H5v7h2v9h10v-9h2V6h-4V5c0-1.7-1.3-3-3-3z"/></svg>'''
            
            st.markdown("### 🏟️ Titulares")
            pitch = f"""
            <div style="position: relative; width: 100%; height: 350px; background-color: #2e8b57; border: 4px solid white; border-bottom: none; border-top-left-radius: 15px; border-top-right-radius: 15px; overflow: hidden; margin-bottom: 20px;">
                <div style="position: absolute; bottom: 0; left: 0; right: 0; height: 3px; background-color: white;"></div>
                <div style="position: absolute; bottom: -40px; left: 50%; transform: translateX(-50%); width: 80px; height: 80px; border: 3px solid white; border-radius: 50%;"></div>
                <div style="position: absolute; top: 0; left: 20%; right: 20%; height: 30%; border: 3px solid white; border-top: none;"></div>
                <div style="position: absolute; top: 0; left: 38%; right: 38%; height: 12%; border: 3px solid white; border-top: none;"></div>
            """
            
            positions_css = [
                "top: 20px; left: 20px;", "top: 20px; right: 20px;", 
                "bottom: 30px; left: 20px;", "bottom: 30px; right: 20px;"
            ]
            
            for i, (_, p) in enumerate(st_df.iterrows()):
                css = positions_css[i] if i < 4 else "top: 45%; left: 45%;"
                pitch += f"""
                <div style="position: absolute; {css} width: 80px; text-align: center; color: white; text-shadow: 1px 1px 3px black; display: flex; flex-direction: column; align-items: center;">
                    {shirt_svg}
                    <div style="font-size: 10px; background: rgba(0,0,0,0.5); padding: 2px 4px; border-radius: 4px; margin-top:2px;">{p['position']}</div>
                    <div style="font-size: 12px; font-weight: bold; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;">{p['name']}</div>
                </div>"""
            st.markdown(pitch + "</div>", unsafe_allow_html=True)
            
            # --- BANQUILLO ---
            if not su_df.empty:
                b_html = '<div style="background: #e0e0e0; padding: 10px; border-radius: 10px; text-align: center;"><h5>🪑 BANQUILLO</h5><div style="display:flex; justify-content:center; gap: 15px;">'
                for _, p in su_df.iterrows():
                    b_html += f'<div style="text-align:center;">{shirt_svg}<br><span style="font-size:10px; color:black; font-weight:bold;">{p["position"]}</span><br><span style="color:black; font-size:12px;">{p["name"]}</span></div>'
                st.markdown(b_html + '</div></div><br>', unsafe_allow_html=True)

            # --- CONTROLES ---
            if locked: st.error("No se pueden hacer cambios durante el partido.")
            else:
                for _, r in sq.iterrows():
                    c1, c2, c3 = st.columns([3, 2, 2])
                    c1.write(f"{r['name']} ({r['position']})")
                    is_t = c2.checkbox("Titular", value=bool(r['is_starter']), key=f"t_{r['id']}")
                    if is_t != bool(r['is_starter']):
                        if is_t and len(st_df) >= 4:
                            st.error("Máximo 4 titulares.")
                            st.rerun()
                        else:
                            run_query("UPDATE user_squads SET is_starter=? WHERE user_id=? AND player_id=?", (is_t, u_id, r['id']))
                            st.rerun()
                    if c3.button("Vender (🪙 "+str(r['price'])+")", key=f"s_{r['id']}"):
                        run_query("UPDATE users SET rupias=rupias+? WHERE id=?", (r['price'], u_id))
                        run_query("DELETE FROM user_squads WHERE user_id=? AND player_id=?", (u_id, r['id']))
                        st.rerun()

    elif menu == "🛒 Mercado":
        st.header("Mercado de Fichajes")
        if locked: st.error("🔒 Mercado cerrado por partido en juego.")
        else:
            m_df = get_dataframe("SELECT id, name, position, price, points FROM players WHERE id NOT IN (SELECT player_id FROM user_squads WHERE user_id=?)", (u_id,))
            sq_c = run_query("SELECT COUNT(*) FROM user_squads WHERE user_id=?", (u_id,))[0][0]
            for _, r in m_df.iterrows():
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.write(f"**{r['name']}** ({r['position']}) - Pts: {round(r['points'], 2)}")
                c2.write(f"🪙 {r['price']}")
                if c3.button("Fichar", key=f"b_{r['id']}"):
                    if sq_c >= 5: st.error("Plantilla llena (Máximo 5).")
                    elif rupias >= r['price']:
                        run_query("UPDATE users SET rupias=rupias-? WHERE id=?", (r['price'], u_id))
                        run_query("INSERT INTO user_squads (user_id, player_id, is_starter) VALUES (?,?,0)", (u_id, r['id']))
                        st.rerun()
                    else: st.error("Saldo insuficiente.")

    elif menu == "⭐ Votar":
        st.header("⭐ Votación")
        m_df = get_dataframe("SELECT id, jornada, rival, match_date, match_time FROM matches ORDER BY id DESC")
        if m_df.empty: st.info("Sin partidos.")
        else:
            opts, sts = {}, {}
            now = datetime.now()
            for _, r in m_df.iterrows():
                try:
                    t_str = str(r['match_time']).split('.')[0]
                    mdt = datetime.strptime(f"{r['match_date']} {t_str}", "%Y-%m-%d %H:%M:%S" if len(t_str.split(':'))==3 else "%Y-%m-%d %H:%M")
                    t1, t2 = mdt + timedelta(hours=5), mdt + timedelta(hours=48)
                    opts[f"{r['jornada']} vs {r['rival']}"] = r['id']
                    if now < t1: sts[r['id']] = ("ESPERA", t1 - now)
                    elif t1 <= now <= t2: sts[r['id']] = ("ABIERTO", t2 - now)
                    else: sts[r['id']] = ("CERRADO", timedelta(0))
                except: pass
            
            if opts:
                s_id = opts[st.selectbox("Partido:", list(opts.keys()))]
                stt, t_left = sts[s_id]
                
                if stt == "ESPERA": st.warning(f"⏳ Abre en: {format_countdown(t_left)}")
                elif stt == "CERRADO": st.error("🔒 Finalizada.")
                else:
                    st.info(f"⏱️ Cierra en: {format_countdown(t_left)}")
                    if run_query("SELECT COUNT(*) FROM ratings WHERE user_id=? AND match_id=?", (u_id, s_id))[0][0] > 0:
                        st.success("Ya votaste.")
                    else:
                        with st.form("vote"):
                            pl = get_dataframe("SELECT id, name, position FROM players")
                            v_in = {p['id']: st.slider(f"{p['name']} ({p['position']})", 1, 5, 3) for _, p in pl.iterrows()}
                            if st.form_submit_button("Enviar"):
                                for p, v in v_in.items():
                                    run_query("INSERT INTO ratings (user_id, player_id, match_id, rating) VALUES (?,?,?,?)", (u_id, p, s_id, v))
                                st.rerun()

    elif menu == "🏆 Clasificación":
        st.header("🏆 Clasificación General")
        r_df = get_dataframe("SELECT username AS 'Manager', ROUND(total_points, 2) AS 'Puntos Totales' FROM users WHERE role='user' ORDER BY total_points DESC")
        if r_df.empty: st.info("No hay managers.")
        else:
            r_df.index = range(1, len(r_df) + 1)
            st.table(r_df)

# ==========================================
# 6. MAIN
# ==========================================
def main():
    st.set_page_config("Fantasy Local", "⚽")
    init_db()
    if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
        
    if not st.session_state['logged_in']:
        st.title("⚽ Fantasy Local")
        t1, t2 = st.tabs(["Entrar", "Registrarse"])
        with t1:
            u_log = st.text_input("Usuario")
            p_log = st.text_input("Clave", type="password")
            if st.button("Acceder"): login_user(u_log, p_log)
        with t2:
            u_reg = st.text_input("Nuevo Usuario")
            p_reg = st.text_input("Nueva Clave", type="password")
            if st.button("Crear Equipo") and u_reg: register_user(u_reg, p_reg)
    else:
        if st.sidebar.button("Cerrar Sesión"):
            st.session_state.clear()
            st.rerun()
        if st.session_state['role'] == 'admin': admin_panel()
        else: user_panel()

if __name__ == '__main__':
    main()
