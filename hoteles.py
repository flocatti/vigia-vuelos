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


def consultar(cfg, zona, entrada, salida, con_filtros=True):
    params = {"engine": "google_hotels", "q": zona["q"],
              "check_in_date": entrada.isoformat(), "check_out_date": salida.isoformat(),
              "adults": str(cfg["personas"]), "currency": "USD", "hl": "es", "gl": "us",
              "api_key": os.environ["SERPAPI_KEY"]}
    if zona.get("casa"):
        params["vacation_rentals"] = "true"
        params["bedrooms"] = str(zona.get("dormitorios", 2))
    if con_filtros:
        params["sort_by"] = "3"                      # precio más bajo primero
        if not zona.get("casa"):
            params["rating"] = "8"                   # 4 estrellas de opinión o más
    return http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))


def buscar(cfg, zona, entrada, salida):
    """Lista de hasta 3 opciones (más barata primero) para la zona."""
    noches = (salida - entrada).days
    if SIMULAR:
        base = zona.get("precio_simulado", 150)
        return [{"nombre": f"Opción simulada {i + 1}", "noche": round(base * f),
                 "total": round(base * f) * noches, "puntaje": 4.2, "link": "https://www.google.com/travel/hotels"}
                for i, f in enumerate(sorted(random.uniform(0.8, 1.4) for _ in range(3)))]
    datos = consultar(cfg, zona, entrada, salida)
    if not datos or datos.get("error"):
        print(f"  reintento sin filtros ({(datos or {}).get('error', 'respuesta vacía')})")
        datos = consultar(cfg, zona, entrada, salida, con_filtros=False)
    if not datos or datos.get("error"):
        print(f"  sin resultados ({(datos or {}).get('error', 'respuesta vacía')})")
        return []
    link_general = datos.get("search_metadata", {}).get("google_hotels_url", "")
    min_puntaje = zona.get("puntaje_minimo", cfg["puntaje_minimo"])
    min_opiniones = zona.get("opiniones_minimas", cfg["opiniones_minimas"])
    opciones = []
    for p in datos.get("properties", []):
        noche = (p.get("rate_per_night") or {}).get("extracted_lowest")
        total = (p.get("total_rate") or {}).get("extracted_lowest")
        if not noche and not total:
            continue
        if (p.get("overall_rating") or 0) < min_puntaje or (p.get("reviews") or 0) < min_opiniones:
            continue
        total = total or noche * noches
        opciones.append({"nombre": p.get("name", "?"), "noche": round(noche or total / noches),
                         "total": round(total), "puntaje": p.get("overall_rating"),
                         "link": p.get("link") or link_general})
    return sorted(opciones, key=lambda o: o["total"])[:3]


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
            bloques.append(f"{zona['nombre']}: sin resultados")
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
        previo = minimos.get(zona["codigo"])
        if previo and mejor["total"] < previo:
            lineas.append(f" ⬇ Nuevo mínimo (antes USD {previo})")
        lineas.append(f" {mejor['link']}")
        bloques.append("\n".join(lineas))
        g = zona["grupo"]
        if g not in mejor_por_grupo or mejor["total"] < mejor_por_grupo[g][1]:
            mejor_por_grupo[g] = (zona["nombre"], mejor["total"])
    guardar(nuevas)
    if not nuevas:
        print("No se obtuvo ningún precio.")
        return
    titulo = ("🏨 Hoteles — precios de REFERENCIA (un año antes del viaje)" if ensayo
              else "🏨 Hoteles — precios del viaje")
    resumen = "\n".join(f" {g}: {n} — USD {t}" for g, (n, t) in mejor_por_grupo.items())
    suma = sum(t for _, t in mejor_por_grupo.values())
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
    avisar(f"Combinación más barata:\n{resumen}\n Total alojamiento: USD {suma}")
    print(f"\nListo: {len(nuevas)} precios de hoteles guardados.")


if __name__ == "__main__":
    main()
