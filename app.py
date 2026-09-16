import math
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import streamlit as st

try:
    from metar import Metar
    HAS_METAR_PARSER = True
except ImportError:
    HAS_METAR_PARSER = False

PSEUDO_ICAO = "ZZZZ"
REMARK_SAFETY = "RMK SYNTHETIC DATA NON-OFFICIAL NOT FOR OPERATIONAL FLIGHT USE"

# --- FUNCIONES DE CÁLCULO Y METEOROLOGÍA ---

def resolve_location(city_name: str) -> tuple[float, float, float, str]:
    encoded_name = urllib.parse.quote(city_name)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_name}&count=1&language=es&format=json"
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherWeb/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        
    if not data.get("results"):
        raise ValueError(f"No se encontró la localidad: '{city_name}'")
        
    match = data["results"][0]
    label = f"{match.get('name')}, {match.get('admin1', '')} ({match.get('country_code', '')})"
    return match["latitude"], match["longitude"], match.get("elevation", 0.0), label

def fetch_open_meteo_dataset(lat: float, lon: float, elevation: float) -> dict:
    endpoint = "https://api.open-meteo.com/v1/forecast"
    url = (
        f"{endpoint}?latitude={lat}&longitude={lon}&elevation={elevation}"
        f"&current=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility"
        f"&hourly=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"visibility,wind_speed_10m,wind_direction_10m,precipitation_probability"
        f"&wind_speed_unit=kn&forecast_days=2"
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherWeb/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))

def calculate_qnh_doc9837(surface_pressure_hpa: float, elevation_m: float) -> int:
    factor = 1.0 - (0.0065 * elevation_m) / 288.15
    return int(round(surface_pressure_hpa * math.pow(factor, -5.25588)))

def estimate_lcl_cloud_base_ft(temperature_c: float, dew_point_c: float) -> int:
    spread = max(0.0, temperature_c - dew_point_c)
    base_feet = spread * 400.0
    return max(100, int(math.floor(base_feet / 100.0) * 100))

def map_wmo_to_icao(code: int) -> str:
    lookup = {
        45: "FG", 48: "FZFG", 51: "-DZ", 53: "DZ", 55: "+DZ",
        56: "-FZDZ", 57: "+FZDZ", 61: "-RA", 63: "RA", 65: "+RA",
        66: "-FZRA", 67: "+FZRA", 71: "-SN", 73: "SN", 75: "+SN",
        77: "SG", 80: "-SHRA", 81: "SHRA", 82: "+SHRA",
        85: "-SHSN", 86: "+SHSN", 95: "TSRA", 96: "+TSRA", 99: "TSGR"
    }
    return lookup.get(code, "")

def discretize_cloud_layer(coverage_pct: float, base_ft: int) -> str:
    if coverage_pct < 10:
        return "NSC"
    tag = f"{min(base_ft // 100, 999):03d}"
    if coverage_pct <= 25:
        return f"FEW{tag}"
    elif coverage_pct <= 50:
        return f"SCT{tag}"
    elif coverage_pct <= 87:
        return f"BKN{tag}"
    return f"OVC{tag}"

def format_temperatures(t: float, td: float) -> str:
    t_r, td_r = int(round(t)), int(round(td))
    t_str = f"M{abs(t_r):02d}" if t_r < 0 else f"{t_r:02d}"
    td_str = f"M{abs(td_r):02d}" if td_r < 0 else f"{td_r:02d}"
    return f"{t_str}/{td_str}"

def format_visibility(meters: float) -> str:
    v = int(round(meters))
    if v >= 10000:
        return "9999"
    elif v <= 50:
        return "0000"
    elif v < 800:
        v = (v // 50) * 50
    elif v < 5000:
        v = (v // 100) * 100
    else:
        v = (v // 1000) * 1000
    return f"{v:04d}"

def build_metar(dataset: dict, elevation: float) -> str:
    current = dataset["current"]
    time_stamp = datetime.now(timezone.utc).strftime("%d%H%MZ")
    
    w_dir = int(round(current["wind_direction_10m"] / 10.0) * 10) % 360
    w_spd = int(round(current["wind_speed_10m"]))
    w_gst = int(round(current.get("wind_gusts_10m", 0)))
    
    if w_spd < 1:
        wind = "00000KT"
    elif w_gst >= w_spd + 10:
        wind = f"{w_dir:03d}{w_spd:02d}G{w_gst:02d}KT"
    else:
        wind = f"{w_dir:03d}{w_spd:02d}KT"
        
    t, td = current["temperature_2m"], current["dew_point_2m"]
    wx = map_wmo_to_icao(current["weather_code"])
    base = estimate_lcl_cloud_base_ft(t, td)
    cloud = discretize_cloud_layer(current["cloud_cover"], base)
    vis = format_visibility(current.get("visibility", 10000))
    
    is_cavok = (current.get("visibility", 10000) >= 10000 and wx == "" and (cloud == "NSC" or base >= 5000) and current["cloud_cover"] < 50)
    qnh = calculate_qnh_doc9837(current["surface_pressure"], elevation)
    temp = format_temperatures(t, td)
    
    tokens = ["METAR", PSEUDO_ICAO, time_stamp, "AUTO", wind]
    if is_cavok:
        tokens.append("CAVOK")
    else:
        tokens.extend([vis, wx, cloud] if wx else [vis, cloud])
    tokens.extend([temp, f"Q{qnh:04d}", REMARK_SAFETY])
    return " ".join(tokens)

def build_taf(dataset: dict) -> str:
    hourly = dataset["hourly"]
    time_now = datetime.now(timezone.utc)
    issue_header = time_now.strftime("%d%H%MZ")
    val_start = time_now.strftime("%d%H")
    end_dt = datetime.fromtimestamp(time_now.timestamp() + 86400, timezone.utc)
    val_end = end_dt.strftime("%d%H")
    
    cur_iso = time_now.strftime("%Y-%m-%dT%H:00")
    try:
        start_idx = hourly["time"].index(cur_iso)
    except (ValueError, KeyError):
        start_idx = min(time_now.hour, max(0, len(hourly["time"]) - 25))
        
    b_dir = int(round(hourly["wind_direction_10m"][start_idx] / 10.0) * 10) % 360
    b_spd = int(round(hourly["wind_speed_10m"][start_idx]))
    b_lcl = estimate_lcl_cloud_base_ft(hourly["temperature_2m"][start_idx], hourly["dew_point_2m"][start_idx])
    b_cloud = discretize_cloud_layer(hourly["cloud_cover"][start_idx], b_lcl)
    b_wx = map_wmo_to_icao(hourly["weather_code"][start_idx])
    b_vis = format_visibility(hourly.get("visibility", [10000] * len(hourly["time"]))[start_idx])
    
    lines = [f"TAF {PSEUDO_ICAO} {issue_header} {val_start}/{val_end} {b_dir:03d}{b_spd:02d}KT {b_vis}"]
    if b_wx:
        lines[0] += f" {b_wx}"
    lines[0] += f" {b_cloud}"
    
    for offset in range(3, 24, 3):
        step = start_idx + offset
        if step >= len(hourly["time"]):
            break
        s_dt = datetime.fromisoformat(hourly["time"][step]).replace(tzinfo=timezone.utc)
        e_ts = min(s_dt.timestamp() + 10800, end_dt.timestamp())
        span = f"{s_dt.strftime('%d%H')}/{datetime.fromtimestamp(e_ts, timezone.utc).strftime('%d%H')}"
        
        prob = hourly.get("precipitation_probability", [0] * len(hourly["time"]))[step]
        wx_tag = map_wmo_to_icao(hourly["weather_code"][step])
        
        if wx_tag != "" and prob >= 30:
            prefix = "PROB30 " if prob < 50 else ""
            h_vis = format_visibility(hourly.get("visibility", [10000] * len(hourly["time"]))[step])
            h_lcl = estimate_lcl_cloud_base_ft(hourly["temperature_2m"][step], hourly["dew_point_2m"][step])
            h_cld = discretize_cloud_layer(hourly["cloud_cover"][step], h_lcl)
            lines.append(f"  {prefix}TEMPO {span} {h_vis} {wx_tag} {h_cld}")
            
    lines.append(f"  {REMARK_SAFETY}")
    return "\n".join(lines)

# --- INTERFAZ WEB STREAMLIT ---

st.set_page_config(page_title="AeroWeather METAR/TAF", page_icon="✈️", layout="centered")

st.title("✈️ METAR & TAF Sintético")
st.caption("Generador meteorológico aeronáutico para localidades sin aeropuerto.")

col1, col2 = st.columns([3, 1])
with col1:
    ciudad = st.text_input("Municipio o pueblo:", value="Vera", placeholder="Ej: Vera, Cazorla, Ronda...")
with col2:
    buscar = st.button("Consultar", use_container_width=True, type="primary")

if ciudad:
    try:
        with st.spinner("Obteniendo modelos meteorológicos..."):
            lat, lon, elev, label = resolve_location(ciudad)
            data = fetch_open_meteo_dataset(lat, lon, elev)
            metar_txt = build_metar(data, elev)
            taf_txt = build_taf(data)

        st.success(f"📍 **{label}** | Lat: `{lat:.3f}` | Lon: `{lon:.3f}` | Elev: `{elev:.0f} m MSL`")

        # Tarjetas de resumen rápido
        cur = data["current"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Viento", f"{int(cur['wind_direction_10m'])}° a {int(round(cur['wind_speed_10m']))} kt")
        m2.metric("Temperatura", f"{round(cur['temperature_2m'])}°C", f"Pto. Rocío {round(cur['dew_point_2m'])}°C", delta_color="off")
        m3.metric("QNH", f"{calculate_qnh_doc9837(cur['surface_pressure'], elev)} hPa")
        m4.metric("Nubes", f"{cur['cloud_cover']}%")

        # Visualización de códigos aeronáuticos
        st.subheader("Informe METAR")
        st.code(metar_txt, language="plaintext")

        st.subheader("Pronóstico TAF (24 Horas)")
        st.code(taf_txt, language="plaintext")

    except Exception as e:
        st.error(f"Error al procesar la solicitud: {e}")
