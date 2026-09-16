import math
import json
import urllib.request
from datetime import datetime, timezone
from metar import Metar

# Constantes fijas de la ubicación
LATITUDE = 37.39808
LONGITUDE = -1.75372
ELEVATION = 215.0
PSEUDO_ICAO = "ZZZZ"
REMARK_SAFETY = "RMK SYNTHETIC DATA NON-OFFICIAL NOT FOR OPERATIONAL FLIGHT USE"

def fetch_open_meteo_dataset(lat: float, lon: float, elevation: float) -> dict:
    endpoint = "https://api.open-meteo.com/v1/forecast"
    url = (
        f"{endpoint}?latitude={lat}&longitude={lon}&elevation={elevation}"
        f"&current=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        f"&hourly=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"cloud_cover_low,surface_pressure,pressure_msl,visibility,"
        f"wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability"
        f"&wind_speed_unit=kn&forecast_days=2"
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherProcessor/1.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode('utf-8'))

def calculate_qnh_doc9837(surface_pressure_hpa: float, elevation_m: float) -> int:
    reduction_factor = 1.0 - (0.0065 * elevation_m) / 288.15
    qnh = surface_pressure_hpa * math.pow(reduction_factor, -5.25588)
    return int(round(qnh))

def estimate_lcl_cloud_base_ft(temperature_c: float, dew_point_c: float) -> int:
    spread = max(0.0, temperature_c - dew_point_c)
    base_feet = spread * 400.0
    rounded_base = int(math.floor(base_feet / 100.0) * 100)
    return max(100, rounded_base)

def map_wmo_to_icao(code: int) -> str:
    lookup = {
        45: "FG", 48: "FZFG",
        51: "-DZ", 53: "DZ", 55: "+DZ",
        56: "-FZDZ", 57: "+FZDZ",
        61: "-RA", 63: "RA", 65: "+RA",
        66: "-FZRA", 67: "+FZRA",
        71: "-SN", 73: "SN", 75: "+SN",
        77: "SG",
        80: "-SHRA", 81: "SHRA", 82: "+SHRA",
        85: "-SHSN", 86: "+SHSN",
        95: "TSRA", 96: "+TSRA", 99: "TSGR"
    }
    return lookup.get(code, "")

def discretize_cloud_layer(coverage_pct: float, base_ft: int) -> str:
    if coverage_pct < 10:
        return "NSC"
    altitude_tag = f"{min(base_ft // 100, 999):03d}"
    if coverage_pct <= 25:
        return f"FEW{altitude_tag}"
    elif coverage_pct <= 50:
        return f"SCT{altitude_tag}"
    elif coverage_pct <= 87:
        return f"BKN{altitude_tag}"
    else:
        return f"OVC{altitude_tag}"

def format_temperatures(temperature: float, dew_point: float) -> str:
    t_round = int(round(temperature))
    d_round = int(round(dew_point))
    t_part = f"M{abs(t_round):02d}" if t_round < 0 else f"{t_round:02d}"
    d_part = f"M{abs(d_round):02d}" if d_round < 0 else f"{d_round:02d}"
    return f"{t_part}/{d_part}"

def build_metar(dataset: dict) -> str:
    current = dataset["current"]
    time_now = datetime.now(timezone.utc)
    time_stamp = time_now.strftime("%d%H%MZ")
    
    wind_dir = int(round(current["wind_direction_10m"] / 10.0) * 10) % 360
    wind_spd = int(round(current["wind_speed_10m"]))
    wind_gst = int(round(current.get("wind_gusts_10m", 0)))
    
    if wind_spd < 1:
        wind_block = "00000KT"
    elif wind_gst >= wind_spd + 10:
        wind_block = f"{wind_dir:03d}{wind_spd:02d}G{wind_gst:02d}KT"
    else:
        wind_block = f"{wind_dir:03d}{wind_spd:02d}KT"
        
    t_val = current["temperature_2m"]
    td_val = current["dew_point_2m"]
    weather_desc = map_wmo_to_icao(current["weather_code"])
    cloud_base = estimate_lcl_cloud_base_ft(t_val, td_val)
    cloud_block = discretize_cloud_layer(current["cloud_cover"], cloud_base)
    
    is_cavok = (weather_desc == "" and 
                (cloud_block == "NSC" or cloud_base >= 5000) and 
                current["cloud_cover"] < 50)
    
    qnh_val = calculate_qnh_doc9837(current["surface_pressure"], ELEVATION)
    temp_block = format_temperatures(t_val, td_val)
    
    tokens = ["METAR", PSEUDO_ICAO, time_stamp, "AUTO", wind_block]
    if is_cavok:
        tokens.append("CAVOK")
    else:
        tokens.append("9999")
        if weather_desc:
            tokens.append(weather_desc)
        tokens.append(cloud_block)
        
    tokens.extend([temp_block, f"Q{qnh_val:04d}", REMARK_SAFETY])
    return " ".join(tokens)

def build_taf(dataset: dict) -> str:
    hourly = dataset["hourly"]
    time_now = datetime.now(timezone.utc)
    
    issue_header = time_now.strftime("%d%H%MZ")
    val_start = time_now.strftime("%d%H")
    val_end = datetime.fromtimestamp(time_now.timestamp() + 86400, timezone.utc).strftime("%d%H")
    validity_header = f"{val_start}/{val_end}"
    
    base_dir = int(round(hourly["wind_direction_10m"][0] / 10.0) * 10) % 360
    base_spd = int(round(hourly["wind_speed_10m"][0]))
    base_t = hourly["temperature_2m"][0]
    base_td = hourly["dew_point_2m"][0]
    base_lcl = estimate_lcl_cloud_base_ft(base_t, base_td)
    base_cloud = discretize_cloud_layer(hourly["cloud_cover"][0], base_lcl)
    base_wx = map_wmo_to_icao(hourly["weather_code"][0])
    
    taf_lines = [f"TAF {PSEUDO_ICAO} {issue_header} {validity_header} {base_dir:03d}{base_spd:02d}KT 9999"]
    if base_wx:
        taf_lines[0] += f" {base_wx}"
    taf_lines[0] += f" {base_cloud}"
    
    for step in range(2, 24, 4):
        step_dt = datetime.fromisoformat(hourly["time"][step]).replace(tzinfo=timezone.utc)
        step_end = datetime.fromtimestamp(step_dt.timestamp() + 14400, timezone.utc)
        time_span = f"{step_dt.strftime('%d%H')}/{step_end.strftime('%d%H')}"
        
        prob_val = hourly.get("precipitation_probability", [0] * 48)[step]
        wx_tag = map_wmo_to_icao(hourly["weather_code"][step])
        
        if wx_tag != "" and prob_val >= 30:
            prob_prefix = "PROB30 " if prob_val < 50 else ""
            h_vis = hourly.get("visibility", [9999] * 48)[step]
            vis_val = min(9999, int(h_vis))
            vis_repr = f"{vis_val:04d}" if vis_val < 9999 else "9999"
            
            c_t = hourly["temperature_2m"][step]
            c_td = hourly["dew_point_2m"][step]
            c_lcl = estimate_lcl_cloud_base_ft(c_t, c_td)
            c_cloud = discretize_cloud_layer(hourly["cloud_cover"][step], c_lcl)
            
            taf_lines.append(f"  {prob_prefix}TEMPO {time_span} {vis_repr} {wx_tag} {c_cloud}")
            
    taf_lines.append(f"  {REMARK_SAFETY}")
    return "\n".join(taf_lines)

def verify_metar(metar_str: str):
    try:
        # Excluir la palabra inicial 'METAR'
        content = " ".join(metar_str.split()[1:])
        # Usar Metar.Metar si se importó 'from metar import Metar'
        report = Metar.Metar(content)
        print("\n[VALIDACIÓN OACI]: Formato METAR sintácticamente correcto.")
        print(f"- Estación: {report.station_id}")
        if report.wind_speed:
            print(f"- Viento: {report.wind_speed.value()} nudos")
        if report.press:
            print(f"- Presión QNH: {report.press.value()} hPa")
        if report.temp:
            print(f"- Temperatura: {report.temp.value()} °C")
    except Exception as e:
        print(f"\n[VALIDACIÓN OACI - ADVERTENCIA]: {e}")

if __name__ == "__main__":
    print(f"Descargando datos atmosféricos para lat={LATITUDE}, lon={LONGITUDE}...")
    dataset = fetch_open_meteo_dataset(LATITUDE, LONGITUDE, ELEVATION)
    
    metar = build_metar(dataset)
    taf = build_taf(dataset)
    
    print("\n" + "=" * 60)
    print("INFORME METAR:")
    print(metar)
    print("=" * 60)
    print("PRONÓSTICO TAF:")
    print(taf)
    print("=" * 60)
    
    verify_metar(metar)