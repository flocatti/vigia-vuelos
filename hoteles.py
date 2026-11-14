#!/usr/bin/env python3
"""Vigía de hoteles: consulta Google Hoteles (vía SerpApi) para cada zona del
viaje, guarda las 3 opciones más baratas en Supabase y manda un resumen por
Telegram. Usa las mismas claves que vigia.py.

Uso:
    python hoteles.py             corre solo los días configurados
    python hoteles.py --forzar    corre aunque hoy no toque
    python hoteles.py --simular   prueba sin internet, con precios inventados
"""
import datetime as dt
import json
import os
import random
import sys
import urllib.parse

from vigia import AQUI, HOY, SIMULAR, _sb_cabeceras, avisar, http_json


def cargar_config():
    with open(os.path.join(AQUI, "hoteles.json"), encoding="utf-8") as f:
        return json.load(f)


def minimos_previos(ensayo):
    """Precio total más bajo ya visto por zona (en el mismo modo)."""
    if SIMULAR:
        return {}
    url = (os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/hoteles_precios"
           f"?select=zona,total&ensayo=eq.{str(ensayo).lower()}&order=total.asc&limit=5000")
    filas = http_json(url, cabeceras=_sb_cabeceras())
    if not isinstance(filas, list):
        raise RuntimeError(f"Supabase respondió algo inesperado: {filas}")
    minimos = {}
    for f in filas:
        minimos.setdefault(f["zona"], f["total"])
    return minimos


def guardar(filas):
    if SIMULAR or not filas:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/hoteles_precios"
    resp = http_json(url, datos=filas, cabeceras=_sb_cabeceras(), metodo="POST")
    if resp:
        raise RuntimeError(f"Supabase no guardó los hoteles: {resp}")


def vuelo_mas_barato():
    """(total para todos, origen, es_ensayo) de la última consulta de vuelos, o None."""
    if SIMULAR:
        return 4300, "ASU", True
    try:
        url = (os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/vuelos_precios"
               "?select=origen,total_ajustado,fecha_consulta,ensayo&order=fecha_consulta.desc&limit=60")
        filas = http_json(url, cabeceras=_sb_cabeceras())
        if not isinstance(filas, list) or not filas:
            return None
        dia, modo = filas[0]["fecha_consulta"][:10], filas[0]["ensayo"]
        del_dia = [f for f in filas if f["fecha_consulta"][:10] == dia and f["ensayo"] == modo]
        mejor = min(del_dia, key=lambda f: f["total_ajustado"])
        return mejor["total_ajustado"], mejor["origen"], modo
    except Exception as e:
        print("  no se pudo leer el precio de vuelos:", e)
        return None


def consultar(cfg, zona, entrada, salida, con_filtros=True):
    params = {"engine": "google_hotels", "q": zona["q"],
              "check_in_date": entrada.isoformat(), "check_out_date": salida.isoformat(),
              "adults": str(cfg["personas"]), "currency": "USD", "hl": "es", "gl": "us",
              "api_key": os.environ["SERPAPI_KEY"]}
    if zona.get("casa"):
        params["vacation_rentals"] = "true"
        params["bedrooms"] = str(zona.get("dormitorios", 2))
    if con_filtros:
        if zona.get("ordenar_precio", True):
            params["sort_by"] = "3"                  # precio más bajo primero
        if not zona.get("casa"):
            params["rating"] = "8"                   # 4 puntos de opinión o más
    return http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))


def esta_en_la_zona(prop, zona):
    """Descarta lo que Google devuelve de más: otro barrio u otra cadena."""
    nombre = (prop.get("name") or "").lower().replace("’", "'")
    if zona.get("nombre_contiene") and not any(t in nombre for t in zona["nombre_contiene"]):
        return False
    if zona.get("nombre_empieza") and not any(nombre.startswith(t) for t in zona["nombre_empieza"]):
        return False
    if zona.get("caja"):
        gps = prop.get("gps_coordinates") or {}
        lat, lon = gps.get("latitude"), gps.get("longitude")
        lat_min, lat_max, lon_min, lon_max = zona["caja"]
        if lat is None or lon is None or not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            return False
    return True


def buscar(cfg, zona, entrada, salida):
    """Lista de hasta 3 opciones (más barata primero) para la zona."""
    noches = (salida - entrada).days
    if SIMULAR:
        base = zona.get("precio_simulado", 150)
        return [{"nombre": f"Opción simulada {i + 1}", "noche": round(base * f),
                 "total": round(base * f) * noches, "puntaje": 4.2, "link": "https://www.google.com/travel/hotels"}
                for i, f in enumerate(sorted(random.uniform(0.8, 1.4) for _ in range(cfg.get("opciones_por_zona", 3))))]
    consultas = zona["q"] if isinstance(zona["q"], list) else [zona["q"]]
    min_puntaje = zona.get("puntaje_minimo", cfg["puntaje_minimo"])
    min_opiniones = zona.get("opiniones_minimas", cfg["opiniones_minimas"])
    propiedades, link_general = [], ""
    for q in consultas:
        z = dict(zona, q=q)
        datos = consultar(cfg, z, entrada, salida)
        if not datos or datos.get("error"):
            print(f"  reintento sin filtros ({(datos or {}).get('error', 'respuesta vacía')})")
            datos = consultar(cfg, z, entrada, salida, con_filtros=False)
        if not datos or datos.get("error"):
            print(f"  sin resultados para '{q}' ({(datos or {}).get('error', 'respuesta vacía')})")
            continue
        link_general = link_general or datos.get("search_metadata", {}).get("google_hotels_url", "")
        encontradas = datos.get("properties") or []
        if not encontradas and datos.get("name") and datos.get("rate_per_night"):
            encontradas = [datos]      # Google devolvió la ficha de un solo hotel
        print(f"  '{q}': " + "; ".join(p.get("name", "?") for p in encontradas[:8]))
        propiedades += encontradas
    opciones = []
    for p in propiedades:
        noche = (p.get("rate_per_night") or {}).get("extracted_lowest")
        total = (p.get("total_rate") or {}).get("extracted_lowest")
        if not noche and not total:
            continue
        if (p.get("overall_rating") or 0) < min_puntaje or (p.get("reviews") or 0) < min_opiniones:
            continue
        if not esta_en_la_zona(p, zona):
            continue
        total = total or noche * noches
        opciones.append({"nombre": p.get("name", "?"), "noche": round(noche or total / noches),
                         "total": round(total), "puntaje": round(p.get("overall_rating") or 0, 1),
                         "link": p.get("link") or link_general})
    print(f"  {len(propiedades)} resultados de Google, {len(opciones)} dentro de la zona")
    return sorted(opciones, key=lambda o: o["total"])[:cfg.get("opciones_por_zona", 3)]


def costo_extra(cfg, zona):
    """Comida, auto y estacionamiento estimados para toda la estadía en la zona."""
    e = zona.get("extras")
    if not e:
        return 0, ""
    n = zona["noches"]
    comida = e.get("comida_por_persona_dia", 0) * cfg["personas"] * n
    auto = (e.get("auto_por_dia", 0) + e.get("nafta_por_dia", 0)) * n
    estac = e.get("estacionamiento_por_dia", 0) * e.get("dias_de_parque", 0)
    partes = [f"comida {comida}"]
    if auto:
        partes.append(f"auto y nafta {auto}")
    if estac:
        partes.append(f"estacionamiento {estac}")
    return comida + auto + estac, ", ".join(partes)


def main():
    cfg = cargar_config()
    if HOY.weekday() not in cfg["dias_semana"] and not SIMULAR and "--forzar" not in sys.argv:
        print("Hoy no toca consultar hoteles.")
        return
    real = dt.date.fromisoformat(cfg["llegada_real"])
    referencia = dt.date.fromisoformat(cfg["llegada_referencia"])
    fin = max(z["dia_inicio"] + z["noches"] for z in cfg["zonas"])
    if real + dt.timedelta(days=fin) <= HOY + dt.timedelta(days=cfg["ventana_reserva_dias"]):
        llegada, ensayo = real, False
    elif referencia > HOY + dt.timedelta(days=2):
        llegada, ensayo = referencia, True
    else:
        print("La fecha de referencia ya pasó y las del viaje aún no están a la venta.")
        return
    minimos = minimos_previos(ensayo)
    nuevas, bloques, mejor_por_grupo = [], [], {}
    for zona in cfg["zonas"]:
        entrada = llegada + dt.timedelta(days=zona["dia_inicio"])
        salida = entrada + dt.timedelta(days=zona["noches"])
        print(f"{zona['nombre']}: {entrada} a {salida}")
        try:
            opciones = buscar(cfg, zona, entrada, salida)
        except Exception as e:
            print("  error:", e)
            continue
        if not opciones:
            bloques.append(f"{zona['nombre']}: sin resultados dentro de la zona")
            continue
        lineas = [f"{zona['nombre']} ({zona['noches']} noches desde {entrada:%d/%m/%Y})"]
        for i, o in enumerate(opciones):
            lineas.append(f" {i + 1}. {o['nombre']}: USD {o['noche']}/noche, total USD {o['total']} (★{o['puntaje']})")
            nuevas.append({"zona": zona["codigo"], "entrada": entrada.isoformat(),
                           "salida": salida.isoformat(), "personas": cfg["personas"],
                           "puesto": i + 1, "nombre": o["nombre"], "precio_noche": o["noche"],
                           "total": o["total"], "puntaje": o["puntaje"], "link": o["link"],
                           "ensayo": ensayo})
        mejor = opciones[0]
        extra, detalle = costo_extra(cfg, zona)
        if extra:
            lineas.append(f" + estimado {detalle} = USD {extra}")
            lineas.append(f" COSTO REAL de la opción 1: USD {mejor['total'] + extra}")
        previo = minimos.get(zona["codigo"])
        if previo and mejor["total"] < previo:
            lineas.append(f" ⬇ Nuevo mínimo de alojamiento (antes USD {previo})")
        lineas.append(f" {mejor['link']}")
        bloques.append("\n".join(lineas))
        g = zona["grupo"]
        costo = mejor["total"] + extra
        if g not in mejor_por_grupo or costo < mejor_por_grupo[g][1]:
            mejor_por_grupo[g] = (zona["nombre"], costo)
    guardar(nuevas)
    if not nuevas:
        print("No se obtuvo ningún precio.")
        return
    titulo = ("🏨 Hoteles — precios de REFERENCIA (un año antes del viaje)" if ensayo
              else "🏨 Hoteles — precios del viaje")
    # Telegram corta los mensajes de más de 4096 caracteres: se manda en partes.
    partes, actual = [], f"{titulo}\nHabitación o casa para {cfg['personas']}."
    for b in bloques:
        if len(actual) + len(b) > 3500:
            partes.append(actual)
            actual = b
        else:
            actual += "\n\n" + b
    partes.append(actual)
    for p in partes:
        avisar(p)
    resumen = "\n".join(f" {g}: {n} — USD {t}" for g, (n, t) in mejor_por_grupo.items())
    total = sum(t for _, t in mejor_por_grupo.values())
    fijos = cfg.get("costos_fijos", [])
    lineas_fijas = "".join(f"\n {c['nombre']}: USD {c['monto']}" for c in fijos)
    total += sum(c["monto"] for c in fijos)
    vuelo = vuelo_mas_barato()
    if vuelo:
        monto, origen, de_ensayo = vuelo
        nota = " (fecha de prueba en temporada baja: en fin de año va a ser más caro)" if de_ensayo else ""
        linea_vuelo = f"\n Vuelos desde {origen}, con el costo de llegar: USD {monto}{nota}"
        total += monto
    else:
        linea_vuelo = "\n Vuelos: todavía sin precio"
    tope = cfg.get("tope_usd")
    if tope:
        dif = tope - total
        veredicto = (f"\n ✅ Dentro del tope de USD {tope}: sobran USD {dif}" if dif >= 0
                     else f"\n ⚠️ Supera el tope de USD {tope} por USD {-dif}")
    else:
        veredicto = ""
    avisar("💰 Presupuesto total del viaje\n"
           "Alojamiento más barato de cada ciudad + comida estimada\n"
           "(en Orlando también auto, nafta y estacionamiento si es casa):\n"
           f"{resumen}{lineas_fijas}{linea_vuelo}\n\n TOTAL: USD {total}{veredicto}\n\n"
           "Comida, auto, entradas y traslados son estimaciones fijas de hoteles.json.")
    print(f"\nListo: {len(nuevas)} precios de hoteles guardados.")


if __name__ == "__main__":
    main()

 
