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

# --- FUNCIONES METEOROLÓGICAS Y DE CÁLCULO ---

def resolve_location(city_name: str) -> tuple[float, float, float, str]:
    """Resuelve la localidad a coordenadas y elevación sobre el nivel del mar."""
    encoded_name = urllib.parse.quote(city_name)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_name}&count=1&language=es&format=json"
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherWeb/2.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        
    if not data.get("results"):
        raise ValueError(f"No se encontró la localidad: '{city_name}'")
        
    match = data["results"][0]
    label = f"{match.get('name')}, {match.get('admin1', '')} ({match.get('country_code', '')})"
    return match["latitude"], match["longitude"], match.get("elevation", 0.0), label

def fetch_open_meteo_dataset(lat: float, lon: float, elevation: float) -> dict:
    """Descarga variables superficiales y prospectivas de Open-Meteo."""
    endpoint = "https://api.open-meteo.com/v1/forecast"
    url = (
        f"{endpoint}?latitude={lat}&longitude={lon}&elevation={elevation}"
        f"&current=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility"
        f"&hourly=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"visibility,wind_speed_10m,wind_direction_10m,precipitation_probability"
        f"&wind_speed_unit=kn&forecast_days=2"
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherWeb/2.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode('utf-8'))

def calculate_qnh_doc9837(surface_pressure_hpa: float, elevation_m: float) -> int:
    """Ajuste altimétrico QNH según ISA (ICAO Doc 9837)."""
    factor = 1.0 - (0.0065 * elevation_m) / 288.15
    return int(round(surface_pressure_hpa * math.pow(factor, -5.25588)))

def estimate_lcl_cloud_base_ft(temperature_c: float, dew_point_c: float) -> int:
    """Base de condensación convectiva (LCL) en pies AGL según ley de Espy."""
    spread = max(0.0, temperature_c - dew_point_c)
    base_feet = spread * 400.0
    return max(100, int(math.floor(base_feet / 100.0) * 100))

def map_wmo_to_icao(code: int) -> str:
    """Traduce código WMO 4677 a descriptor de tiempo presente OACI."""
    lookup = {
        45: "FG", 48: "FZFG", 51: "-DZ", 53: "DZ", 55: "+DZ",
        56: "-FZDZ", 57: "+FZDZ", 61: "-RA", 63: "RA", 65: "+RA",
        66: "-FZRA", 67: "+FZRA", 71: "-SN", 73: "SN", 75: "+SN",
        77: "SG", 80: "-SHRA", 81: "SHRA", 82: "+SHRA",
        85: "-SHSN", 86: "+SHSN", 95: "TSRA", 96: "+TSRA", 99: "TSGR"
    }
    return lookup.get(code, "")

def discretize_cloud_layer(coverage_pct: float, base_ft: int) -> str:
    """Discretiza la cobertura de nubes a octas OACI."""
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

def format_temperatures(temp_c: float, dew_c: float) -> str:
    """Formatea TT/Td con prefijo M para negativos."""
    t_r, td_r = int(round(temp_c)), int(round(dew_c))
    t_str = f"M{abs(t_r):02d}" if t_r < 0 else f"{t_r:02d}"
    td_str = f"M{abs(td_r):02d}" if td_r < 0 else f"{td_r:02d}"
    return f"{t_str}/{td_str}"

def format_visibility(meters: float) -> str:
    """Formatea la visibilidad en 4 dígitos según normativa OACI."""
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

def get_cardinal(deg: float) -> str:
    """Calcula el cuadrante cardinal en español."""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
    idx = int(round(deg / 22.5)) % 16
    return dirs[idx]

def render_wind_rose_svg(deg: float) -> str:
    """Genera la rosa de los vientos con brújula y aguja direccional en SVG."""
    cardinal = get_cardinal(deg)
    return f"""
    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; margin-top: 6px;">
        <svg width="105" height="105" viewBox="0 0 120 120" style="filter: drop-shadow(0 2px 5px rgba(0,0,0,0.12));">
            <circle cx="60" cy="60" r="52" fill="#f8fafc" stroke="#cbd5e1" stroke-width="2.5" />
            <circle cx="60" cy="60" r="42" fill="none" stroke="#e2e8f0" stroke-width="1.2" stroke-dasharray="2 3" />
            <text x="60" y="16" text-anchor="middle" font-size="11" font-weight="bold" fill="#ef4444" font-family="sans-serif">N</text>
            <text x="109" y="64" text-anchor="middle" font-size="10" font-weight="bold" fill="#64748b" font-family="sans-serif">E</text>
            <text x="60" y="111" text-anchor="middle" font-size="10" font-weight="bold" fill="#64748b" font-family="sans-serif">S</text>
            <text x="12" y="64" text-anchor="middle" font-size="10" font-weight="bold" fill="#64748b" font-family="sans-serif">O</text>
            <g transform="rotate({deg}, 60, 60)">
                <line x1="60" y1="20" x2="60" y2="92" stroke="#0284c7" stroke-width="3" stroke-linecap="round" />
                <polygon points="60,100 54,86 66,86" fill="#0284c7" />
                <circle cx="60" cy="20" r="4.5" fill="#ef4444" />
            </g>
            <circle cx="60" cy="60" r="3.5" fill="#0f172a" />
        </svg>
        <span style="font-size: 12px; font-weight: 600; color: #475569; margin-top: 4px;">{int(round(deg))}° ({cardinal})</span>
    </div>
    """

def build_metar(dataset: dict, elevation: float) -> str:
    """Construye el informe de observación METAR."""
    current = dataset["current"]
    time_stamp = datetime.now(timezone.utc).strftime("%d%H%MZ")
    
    wind_dir = int(round(current["wind_direction_10m"] / 10.0) * 10) % 360
    wind_spd = int(round(current["wind_speed_10m"]))
    wind_gst = int(round(current.get("wind_gusts_10m", 0)))
    
    if wind_spd < 1:
        wind_block = "00000KT"
    elif wind_gst >= wind_spd + 10:
        wind_block = f"{wind_dir:03d}{wind_spd:02d}G{wind_gst:02d}KT"
    else:
        wind_block = f"{wind_dir:03d}{wind_spd:02d}KT"
        
    temp_c = current["temperature_2m"]
    dew_c = current["dew_point_2m"]
    weather_desc = map_wmo_to_icao(current["weather_code"])
    cloud_base = estimate_lcl_cloud_base_ft(temp_c, dew_c)
    cloud_block = discretize_cloud_layer(current["cloud_cover"], cloud_base)
    vis_meters = current.get("visibility", 10000)
    vis_block = format_visibility(vis_meters)
    
    is_cavok = (
        vis_meters >= 10000 and 
        weather_desc == "" and 
        (cloud_block == "NSC" or cloud_base >= 5000) and 
        current["cloud_cover"] < 50
    )
    qnh_val = calculate_qnh_doc9837(current["surface_pressure"], elevation)
    temp_block = format_temperatures(temp_c, dew_c)
    
    tokens = ["METAR", PSEUDO_ICAO, time_stamp, "AUTO", wind_block]
    if is_cavok:
        tokens.append("CAVOK")
    else:
        tokens.append(vis_block)
        if weather_desc:
            tokens.append(weather_desc)
        tokens.append(cloud_block)
        
    tokens.extend([temp_block, f"Q{qnh_val:04d}", REMARK_SAFETY])
    return " ".join(tokens)

def build_taf(dataset: dict) -> str:
    """Construye el pronóstico terminal TAF para 24 horas."""
    hourly = dataset["hourly"]
    time_now = datetime.now(timezone.utc)
    issue_header = time_now.strftime("%d%H%MZ")
    val_start = time_now.strftime("%d%H")
    end_time_obj = datetime.fromtimestamp(time_now.timestamp() + 86400, timezone.utc)
    val_end = end_time_obj.strftime("%d%H")
    validity_header = f"{val_start}/{val_end}"
    
    current_iso_hour = time_now.strftime("%Y-%m-%dT%H:00")
    try:
        start_idx = hourly["time"].index(current_iso_hour)
    except (ValueError, KeyError):
        start_idx = min(time_now.hour, max(0, len(hourly["time"]) - 25))
        
    base_dir = int(round(hourly["wind_direction_10m"][start_idx] / 10.0) * 10) % 360
    base_spd = int(round(hourly["wind_speed_10m"][start_idx]))
    base_t = hourly["temperature_2m"][start_idx]
    base_td = hourly["dew_point_2m"][start_idx]
    base_lcl = estimate_lcl_cloud_base_ft(base_t, base_td)
    base_cloud = discretize_cloud_layer(hourly["cloud_cover"][start_idx], base_lcl)
    base_wx = map_wmo_to_icao(hourly["weather_code"][start_idx])
    base_vis = format_visibility(hourly.get("visibility", [10000] * len(hourly["time"]))[start_idx])
    
    taf_lines = [f"TAF {PSEUDO_ICAO} {issue_header} {validity_header} {base_dir:03d}{base_spd:02d}KT {base_vis}"]
    if base_wx:
        taf_lines[0] += f" {base_wx}"
    taf_lines[0] += f" {base_cloud}"
    
    for offset in range(3, 24, 3):
        step = start_idx + offset
        if step >= len(hourly["time"]):
            break
        step_dt = datetime.fromisoformat(hourly["time"][step]).replace(tzinfo=timezone.utc)
        step_end_ts = min(step_dt.timestamp() + 10800, end_time_obj.timestamp())
        step_end_dt = datetime.fromtimestamp(step_end_ts, timezone.utc)
        time_span = f"{step_dt.strftime('%d%H')}/{step_end_dt.strftime('%d%H')}"
        
        prob_val = hourly.get("precipitation_probability", [0] * len(hourly["time"]))[step]
        wx_tag = map_wmo_to_icao(hourly["weather_code"][step])
        
        if wx_tag != "" and prob_val >= 30:
            prob_prefix = "PROB30 " if prob_val < 50 else ""
            h_vis = hourly.get("visibility", [10000] * len(hourly["time"]))[step]
            vis_repr = format_visibility(h_vis)
            c_t = hourly["temperature_2m"][step]
            c_td = hourly["dew_point_2m"][step]
            c_lcl = estimate_lcl_cloud_base_ft(c_t, c_td)
            c_cloud = discretize_cloud_layer(hourly["cloud_cover"][step], c_lcl)
            taf_lines.append(f"  {prob_prefix}TEMPO {time_span} {vis_repr} {wx_tag} {c_cloud}")
            
    taf_lines.append(f"  {REMARK_SAFETY}")
    return "\n".join(taf_lines)

# --- INTERFAZ STREAMLIT ---

st.set_page_config(page_title="AeroWeather METAR/TAF", page_icon="✈️", layout="centered")

st.title("✈️ METAR & TAF Sintético")
st.caption("Generador meteorológico aeronáutico para localidades sin aeropuerto.")

col1, col2 = st.columns([3, 1])
with col1:
    ciudad = st.text_input("Municipio o pueblo:", value="Pulpí", placeholder="Ej: Pulpí, Vera, Cazorla...")
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

        cur = data["current"]
        wind_speed_kt = int(round(cur["wind_speed_10m"]))
        wind_dir_deg = float(cur["wind_direction_10m"])
        temp_rounded = round(cur["temperature_2m"])
        dew_rounded = round(cur["dew_point_2m"])

        m1, m2, m3, m4 = st.columns(4)
        
        with m1:
            st.metric("Viento", f"{wind_speed_kt} kt")
            st.markdown(render_wind_rose_svg(wind_dir_deg), unsafe_allow_html=True)

        with m2:
            st.metric("Temp / Rocío", f"{temp_rounded}°C / {dew_rounded}°C")

        with m3:
            st.metric("QNH", f"{calculate_qnh_doc9837(cur['surface_pressure'], elev)} hPa")

        with m4:
            st.metric("Nubes", f"{cur['cloud_cover']}%")

        st.subheader("Informe METAR")
        st.code(metar_txt, language="plaintext")

        st.subheader("Pronóstico TAF (24 Horas)")
        st.code(taf_txt, language="plaintext")

    except Exception as e:
        st.error(f"Error al procesar la solicitud: {e}")
