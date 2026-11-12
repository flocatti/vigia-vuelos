#!/usr/bin/env python3
"""Vigía de vuelos: consulta Google Flights (vía SerpApi), guarda el historial
en Supabase y avisa por Telegram cuando aparece un precio muy bueno.

Uso:
    python vigia.py             corrida real (necesita las claves en variables de entorno)
    python vigia.py --simular   prueba sin internet, con precios inventados

Solo usa la librería estándar de Python: no hay nada que instalar.
"""
import datetime as dt
import json
import os
import random
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
SIMULAR = "--simular" in sys.argv
HOY = dt.date.today()


def cargar_config():
    with open(os.path.join(AQUI, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def http_json(url, datos=None, cabeceras=None, metodo=None):
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, headers=cabeceras or {}, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            texto = r.read().decode()
    except urllib.error.HTTPError as e:
        texto = e.read().decode()
        try:
            return json.loads(texto)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code}: {texto[:300]}")
    return json.loads(texto) if texto.strip() else None


# ---------------------------------------------------------------- Supabase
def _sb_cabeceras():
    clave = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": clave, "Authorization": f"Bearer {clave}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}


def leer_historial():
    if SIMULAR:
        return []
    url = (os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/vuelos_precios"
           "?select=origen,salida,total_ajustado,fecha_consulta,ensayo"
           "&order=fecha_consulta.desc&limit=5000")
    filas = http_json(url, cabeceras=_sb_cabeceras())
    if not isinstance(filas, list):
        raise RuntimeError(f"Supabase respondió algo inesperado: {filas}")
    return filas


def guardar(filas):
    if SIMULAR or not filas:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/vuelos_precios"
    resp = http_json(url, datos=filas, cabeceras=_sb_cabeceras(), metodo="POST")
    if resp:  # con return=minimal, una respuesta con contenido es un error
        raise RuntimeError(f"Supabase no guardó los precios: {resp}")


# ---------------------------------------------------------------- Telegram
def avisar(texto):
    print("\n--- MENSAJE ---\n" + texto + "\n---------------")
    if SIMULAR:
        return
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    resp = http_json(url, datos={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": texto,
                                 "disable_web_page_preview": True},
                     cabeceras={"Content-Type": "application/json"})
    if not (resp or {}).get("ok"):
        print("AVISO: Telegram no aceptó el mensaje:", resp)


# ---------------------------------------------------------------- Búsqueda
def buscar(cfg, origen, salida, regreso):
    """Devuelve dict con el precio más barato encontrado, o None si no hay."""
    if SIMULAR:
        base = {"BUE": 5200, "ASU": 4300, "MVD": 5000, "SCL": 3900, "GRU": 4100}
        return {"precio": int(base.get(origen["codigo"], 4800) * random.uniform(0.7, 1.15)),
                "aerolinea": "Simulada", "escalas": 1, "link": "https://www.google.com/travel/flights"}
    tramos = [
        {"departure_id": origen["aeropuertos"], "arrival_id": cfg["llegada"], "date": salida.isoformat()},
        {"departure_id": cfg["regreso_desde"], "arrival_id": origen["aeropuertos"], "date": regreso.isoformat()},
    ]
    params = {"engine": "google_flights", "type": "3", "multi_city_json": json.dumps(tramos),
              "adults": str(cfg["pasajeros"]), "currency": "USD", "hl": "es", "gl": "ar",
              "api_key": os.environ["SERPAPI_KEY"]}
    datos = http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
    if not datos or datos.get("error"):
        print(f"  sin resultados ({(datos or {}).get('error', 'respuesta vacía')})")
        return None
    opciones = [o for o in datos.get("best_flights", []) + datos.get("other_flights", [])
                if isinstance(o.get("price"), (int, float))]
    if not opciones:
        print("  sin precios en la respuesta")
        return None
    mejor = min(opciones, key=lambda o: o["price"])
    vuelos = mejor.get("flights", [])
    return {"precio": mejor["price"],
            "aerolinea": ", ".join(sorted({v.get("airline", "?") for v in vuelos})) or "?",
            "escalas": max(len(vuelos) - 1, 0),
            "link": datos.get("search_metadata", {}).get("google_flights_url", "")}


# ---------------------------------------------------------------- Lógica
def elegir_consultas(cfg, historial):
    """Combinaciones (origen, salida) a consultar hoy, y si es modo ensayo."""
    limite = HOY + dt.timedelta(days=cfg["ventana_reserva_dias"])
    dias = cfg["duracion_dias"]
    reales = []
    for o in cfg["origenes"]:
        for s in cfg["salidas"]:
            salida = dt.date.fromisoformat(s)
            if salida > HOY and salida + dt.timedelta(days=dias) <= limite:
                reales.append((o, salida))
    if reales:
        ultima = {}
        for f in historial:  # viene ordenado del más nuevo al más viejo
            ultima.setdefault((f["origen"], f["salida"]), f["fecha_consulta"])
        reales.sort(key=lambda c: ultima.get((c[0]["codigo"], c[1].isoformat()), ""))
        return reales[: cfg["busquedas_por_dia"]], False
    # Las fechas del viaje todavía no están a la venta: ensayo semanal con una
    # fecha lejana, para probar el sistema y comparar ciudades de salida.
    if HOY.weekday() != cfg["dia_ensayo"] and not SIMULAR and "--forzar" not in sys.argv:
        return [], True
    salida = limite - dt.timedelta(days=dias + 7)
    return [(o, salida) for o in cfg["origenes"]], True


def main():
    cfg = cargar_config()
    historial = leer_historial()
    consultas, ensayo = elegir_consultas(cfg, historial)
    if not consultas:
        print("Hoy no toca consultar (las fechas del viaje aún no están a la venta).")
        return
    factor = 1 if cfg["precio_es_total"] else cfg["pasajeros"]
    previos = [f for f in historial if not f["ensayo"]]
    minimo_previo = min((f["total_ajustado"] for f in previos), default=None)
    nuevas, alertas = [], []
    for origen, salida in consultas:
        regreso = salida + dt.timedelta(days=cfg["duracion_dias"])
        print(f"{origen['nombre']}: sale {salida}, vuelve {regreso}")
        try:
            r = buscar(cfg, origen, salida, regreso)
        except Exception as e:  # una búsqueda fallida no frena al resto
            print("  error:", e)
            continue
        if not r:
            continue
        total = round(r["precio"] * factor)
        ajustado = total + origen["costo_llegar_usd"]
        print(f"  USD {total} (+{origen['costo_llegar_usd']} para llegar = {ajustado}) {r['aerolinea']}")
        nuevas.append({"fecha_consulta": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "origen": origen["codigo"], "salida": salida.isoformat(),
                       "regreso": regreso.isoformat(), "pasajeros": cfg["pasajeros"],
                       "precio_total": total, "total_ajustado": ajustado,
                       "aerolinea": r["aerolinea"], "escalas": r["escalas"],
                       "link": r["link"], "ensayo": ensayo})
        if ensayo:
            continue
        motivos = []
        if cfg.get("umbral_usd") and ajustado <= cfg["umbral_usd"]:
            motivos.append(f"bajo tu umbral de USD {cfg['umbral_usd']}")
        del_origen = [f["total_ajustado"] for f in previos if f["origen"] == origen["codigo"]]
        if len(del_origen) >= cfg["minimo_historial"]:
            mediana = statistics.median(del_origen)
            if ajustado <= mediana * (1 - cfg["baja_alerta"]):
                motivos.append(f"{round((1 - ajustado / mediana) * 100)}% bajo lo habitual (USD {round(mediana)})")
        if minimo_previo and ajustado <= minimo_previo * 0.97:
            motivos.append(f"nuevo mínimo (antes USD {minimo_previo})")
            minimo_previo = ajustado  # para no repetir el aviso en la misma corrida
        if motivos:
            alertas.append((ajustado, origen, salida, regreso, total, r, motivos))
    guardar(nuevas)

    for ajustado, origen, salida, regreso, total, r, motivos in sorted(alertas, key=lambda a: a[0]):
        avisar(f"✈️ OFERTA desde {origen['nombre']}\n"
               f"USD {total} los {cfg['pasajeros']} (USD {ajustado} contando llegar al aeropuerto)\n"
               f"Ida {salida:%d/%m/%Y} → vuelta {regreso:%d/%m/%Y}\n"
               f"{r['aerolinea']}, {r['escalas']} escala(s) en la ida\n"
               f"Motivo: {'; '.join(motivos)}\n{r['link']}")
    if nuevas and (ensayo or HOY.weekday() == cfg["dia_ensayo"]):
        orden = sorted(nuevas, key=lambda f: f["total_ajustado"])
        lineas = [f"{f['origen']}: USD {f['total_ajustado']} (sale {f['salida']}, {f['aerolinea']})" for f in orden]
        titulo = ("🧪 Ensayo semanal (fecha de prueba, no la del viaje)" if ensayo
                  else "📊 Resumen semanal de lo consultado hoy")
        avisar(titulo + f"\nTotal para {cfg['pasajeros']}, incluido el costo de llegar:\n" + "\n".join(lineas)
               + f"\n\nVer el más barato en Google Flights:\n{orden[0]['link']}")
    print(f"\nListo: {len(nuevas)} precios guardados, {len(alertas)} alertas.")


if __name__ == "__main__":
    main()
