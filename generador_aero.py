#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Generador Autónomo Universal de Informes METAR y Pronósticos TAF Sintéticos
Válido para cualquier municipio o coordenada mundial.
Fuente: Open-Meteo Geocoding & Weather Forecast API (Acceso libre)
"""

import math
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Verificación de librerías opcionales
try:
    from metar import Metar
    HAS_METAR_PARSER = True
except ImportError:
    HAS_METAR_PARSER = False

PSEUDO_ICAO = "ZZZZ"
REMARK_SAFETY = "RMK SYNTHETIC DATA NON-OFFICIAL NOT FOR OPERATIONAL FLIGHT USE"


def resolve_location(city_name: str) -> tuple[float, float, float]:
    """
    Resuelve el nombre de cualquier municipio a latitud, longitud y elevación
    utilizando el servicio de geocodificación abierto de Open-Meteo.
    """
    encoded_name = urllib.parse.quote(city_name)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_name}&count=1&language=es&format=json"
    
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroWeatherProcessor/2.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        
    if not data.get("results"):
        raise ValueError(f"No se encontraron coordenadas para la localidad: '{city_name}'")
        
    match = data["results"][0]
    lat = match["latitude"]
    lon = match["longitude"]
    elevation = match.get("elevation", 0.0)
    return lat, lon, elevation


def fetch_open_meteo_dataset(lat: float, lon: float, elevation: float) -> dict:
    """
    Descarga variables atmosféricas en superficie y series prospectivas horarias.
    """
    endpoint = "https://api.open-meteo.com/v1/forecast"
    url = (
        f"{endpoint}?latitude={lat}&longitude={lon}&elevation={elevation}"
        f"&current=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,"
        f"wind_gusts_10m,visibility"
        f"&hourly=temperature_2m,dew_point_2m,weather_code,cloud_cover,"
        f"cloud_cover_low,surface_pressure,pressure_msl,visibility,"
        f"wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability"
        f"&wind_speed_unit=kn&forecast_days=2"
    )
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'AeroWeatherProcessor/2.0'}
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode('utf-8'))


def calculate_qnh_doc9837(surface_pressure_hpa: float, elevation_m: float) -> int:
    """
    Calcula el ajuste altimétrico QNH mediante la ley hipsométrica ISA (ICAO Doc 9837).
    """
    reduction_factor = 1.0 - (0.0065 * elevation_m) / 288.15
    qnh = surface_pressure_hpa * math.pow(reduction_factor, -5.25588)
    return int(round(qnh))


def estimate_lcl_cloud_base_ft(temperature_c: float, dew_point_c: float) -> int:
    """
    Estima la base de condensación convectiva (LCL) en pies AGL con la relación de Espy:
    h = 400 * (T - Td).
    """
    spread = max(0.0, temperature_c - dew_point_c)
    base_feet = spread * 400.0
    rounded_base = int(math.floor(base_feet / 100.0) * 100)
    return max(100, rounded_base)


def map_wmo_to_icao(code: int) -> str:
    """
    Traduce el código numérico de tiempo presente WMO 4677 a descriptores OACI.
    """
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
    """
    Transforma el porcentaje de nubosidad a octas según el Anexo 3 de la OACI.
    """
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
    """
    Formatea TT/Td anteponiendo 'M' a magnitudes bajo cero.
    """
    t_round = int(round(temperature))
    d_round = int(round(dew_point))
    t_part = f"M{abs(t_round):02d}" if t_round < 0 else f"{t_round:02d}"
    d_part = f"M{abs(d_round):02d}" if d_round < 0 else f"{d_round:02d}"
    return f"{t_part}/{d_part}"


def format_visibility(meters: float) -> str:
    """
    Discretiza la visibilidad horizontal en metros según las resoluciones OACI.
    """
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


def build_metar(dataset: dict, elevation_m: float) -> str:
    """
    Construye el informe de observación METAR con reducción barométrica al terreno local.
    """
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
    
    vis_meters = current.get("visibility", 10000)
    vis_block = format_visibility(vis_meters)
    
    # Criterio estricto CAVOK
    is_cavok = (
        vis_meters >= 10000 and 
        weather_desc == "" and 
        (cloud_block == "NSC" or cloud_base >= 5000) and 
        current["cloud_cover"] < 50
    )
    
    qnh_val = calculate_qnh_doc9837(current["surface_pressure"], elevation_m)
    temp_block = format_temperatures(t_val, td_val)
    
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
    """
    Genera el pronóstico terminal TAF para 24 horas.
    """
    hourly = dataset["hourly"]
    time_now = datetime.now(timezone.utc)
    
    issue_header = time_now.strftime("%d%H%MZ")
    val_start = time_now.strftime("%d%H")
    end_time_obj = datetime.fromtimestamp(time_now.timestamp() + 86400, timezone.utc)
    val_end = end_time_obj.strftime("%d%H")
    validity_header = f"{val_start}/{val_end}"
    
    # Sincronización horaria con respaldo
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


def verify_metar(metar_str: str) -> None:
    """Valida la sintaxis del METAR."""
    if not HAS_METAR_PARSER:
        return
    try:
        content = " ".join(metar_str.split()[1:])
        report = Metar.Metar(content)
        print("\n[VALIDACIÓN OACI]: Formato conforme.")
        print(f"- Estación: {report.station_id}")
        if report.wind_speed:
            print(f"- Viento: {report.wind_speed.value()} kt")
        if report.press:
            print(f"- Presión QNH: {report.press.value()} hPa")
        if report.temp:
            print(f"- Temperatura: {report.temp.value()} °C")
    except Exception as e:
        print(f"\n[VALIDACIÓN OACI - AVISO]: {e}")


if __name__ == "__main__":
    # Permite pasar el nombre como argumento: python aero_weather.py "Pueblo"
    if len(sys.argv) > 1:
        location_query = " ".join(sys.argv[1:])
    else:
        location_query = input("Introduce el nombre del pueblo o municipio: ").strip()

    try:
        print(f"Buscando coordenadas para '{location_query}'...")
        lat, lon, elevation = resolve_location(location_query)
        print(f"-> Localizado: Lat={lat:.4f}, Lon={lon:.4f}, Elevación={elevation:.1f} m MSL")
        
        print("Descargando datos meteorológicos...")
        dataset = fetch_open_meteo_dataset(lat, lon, elevation)
        
        metar_result = build_metar(dataset, elevation)
        taf_result = build_taf(dataset)
        
        print("\n" + "=" * 65)
        print("INFORME METAR OBTENIDO:")
        print(metar_result)
        print("=" * 65)
        print("PRONÓSTICO TAF GENERADO:")
        print(taf_result)
        print("=" * 65)
        
        verify_metar(metar_result)
        
    except Exception as err:
        print(f"\nError: {err}")
