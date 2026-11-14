#!/usr/bin/env python3
"""Plan de pagos y ahorro del viaje. Usa las mismas claves que vigia.py.

Todos los días lee lo que le escribiste al bot de Telegram:
    ahorre 500            suma 500 dólares a lo ahorrado
    pague vuelos 4100     marca ese pago como hecho, con el monto real
    plan                  pide el plan actualizado
El día 1 de cada mes (o cuando le escribís) manda el plan: qué toca pagar,
cuánto hay que ahorrar por mes y cómo viene el calendario.

Uso:
    python plan.py             corrida normal
    python plan.py --forzar    manda el plan aunque hoy no toque
    python plan.py --simular   prueba sin internet
"""
import datetime as dt
import json
import os
import re
import sys
import unicodedata

from vigia import AQUI, HOY, SIMULAR, _sb_cabeceras, avisar, http_json

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def cargar_plan():
    with open(os.path.join(AQUI, "plan.json"), encoding="utf-8") as f:
        return json.load(f)


def sin_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def numero(texto):
    """'1.500' -> 1500, '1500,50' -> 1500.5, 'usd 800' -> 800. None si no hay número."""
    m = re.search(r"\d[\d.,]*", texto)
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s:                               # coma decimal: 1.500,50
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):  # punto de miles: 1.500
        s = s.replace(".", "")
    try:
        return round(float(s))
    except ValueError:
        return None


def nombre_mes(aaaa_mm):
    a, m = aaaa_mm.split("-")
    return f"{MESES[int(m) - 1]} de {a}"


def meses_hasta(aaaa_mm):
    a, m = (int(x) for x in aaaa_mm.split("-"))
    return (a - HOY.year) * 12 + (m - HOY.month)


# ------------------------------------------------------------ Supabase
def leer_movimientos():
    if SIMULAR:
        return [{"tipo": "ahorro", "clave": None, "monto": 1500}]
    url = (os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/plan_movimientos"
           "?select=tipo,clave,monto&order=fecha.asc&limit=5000")
    filas = http_json(url, cabeceras=_sb_cabeceras())
    if not isinstance(filas, list):
        raise RuntimeError(f"Supabase respondió algo inesperado: {filas}")
    return filas


def guardar_movimiento(tipo, clave, monto):
    if SIMULAR:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/plan_movimientos"
    resp = http_json(url, datos=[{"tipo": tipo, "clave": clave, "monto": monto}],
                     cabeceras=_sb_cabeceras(), metodo="POST")
    if resp:
        raise RuntimeError(f"Supabase no guardó el movimiento: {resp}")


# ------------------------------------------------------------ Telegram
def leer_mensajes():
    """Textos nuevos que mandó el dueño del bot. Los marca como leídos."""
    if SIMULAR:
        return []
    base = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getUpdates"
    datos = http_json(base + "?timeout=0")
    if not (datos or {}).get("ok"):
        print("No se pudieron leer los mensajes del bot:", datos)
        return []
    textos, ultimo = [], None
    for u in datos.get("result", []):
        ultimo = u["update_id"]
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) == str(os.environ["TELEGRAM_CHAT_ID"]) and m.get("text"):
            textos.append(m["text"])
    if ultimo is not None:
        http_json(f"{base}?offset={ultimo + 1}&timeout=0")   # confirma lectura
    return textos


def procesar(texto, plan):
    """Devuelve (respuesta, pide_plan)."""
    t = sin_acentos(texto.strip().lower()).lstrip("/")
    palabras = t.split()
    if not palabras:
        return None, False
    if palabras[0] == "plan":
        return None, True
    claves = {p["clave"] for p in plan["pagos"]}
    if palabras[0] in ("ahorre", "ahorro", "ahorramos"):
        monto = numero(t)
        if not monto:
            return "No entendí el monto. Ejemplo: ahorre 500", False
        guardar_movimiento("ahorro", None, monto)
        return f"Anotado: ahorro de USD {monto}.", True
    if palabras[0] in ("pague", "pago", "pagamos"):
        clave = next((p for p in palabras[1:] if p in claves), None)
        monto = numero(" ".join(palabras[1:]))
        if not clave or not monto:
            return ("No entendí. Ejemplo: pague vuelos 4100\nPagos posibles: "
                    + ", ".join(sorted(claves))), False
        guardar_movimiento("pago", clave, monto)
        return f"Anotado: pago de '{clave}' por USD {monto}.", True
    return None, False


# ------------------------------------------------------------ Plan
def armar_plan(plan, movimientos):
    ahorrado = sum(m["monto"] for m in movimientos if m["tipo"] == "ahorro")
    pagos_hechos = {}
    for m in movimientos:
        if m["tipo"] == "pago":
            pagos_hechos[m["clave"]] = pagos_hechos.get(m["clave"], 0) + m["monto"]
    pagado = sum(pagos_hechos.values())
    disponible = ahorrado - pagado
    pendientes = sorted((p for p in plan["pagos"] if p["clave"] not in pagos_hechos),
                        key=lambda p: p["mes"])
    falta = sum(p["monto"] for p in pendientes)

    # Ritmo de ahorro: el mayor esfuerzo mensual que exige algún pago futuro.
    ritmo, acumulado, cuello = 0, 0, None
    for p in pendientes:
        acumulado += p["monto"]
        meses = max(meses_hasta(p["mes"]), 1)
        necesario = (acumulado - disponible) / meses
        if necesario > ritmo:
            ritmo, cuello = necesario, p

    lineas = ["📅 Plan de pagos y ahorro del viaje",
              f"Ahorrado hasta hoy: USD {ahorrado}",
              f"Ya pagado: USD {pagado}  |  Disponible: USD {disponible}",
              f"Falta pagar: USD {falta}"]
    if cuello:
        lineas.append(f"\n💵 Para llegar, hay que ahorrar USD {round(ritmo)} por mes.\n"
                      f"(Lo que más aprieta: {cuello['nombre']} en {nombre_mes(cuello['mes'])}.)")
    elif pendientes:
        lineas.append("\n💵 Con lo ahorrado ya alcanza para todo lo que falta.")

    este_mes = [p for p in pendientes if meses_hasta(p["mes"]) <= 0]
    if este_mes:
        lineas.append("\n🔔 ESTE MES TOCA:")
        for p in este_mes:
            lineas.append(f" • {p['nombre']}: USD {p['monto']} ({p['medio']})")
            if p.get("nota"):
                lineas.append(f"   {p['nota']}")

    lineas.append("\nCalendario:")
    for p in sorted(plan["pagos"], key=lambda p: p["mes"]):
        if p["clave"] in pagos_hechos:
            lineas.append(f" ✅ {p['nombre']}: pagado USD {pagos_hechos[p['clave']]}")
        else:
            lineas.append(f" ⏳ {nombre_mes(p['mes'])}: {p['nombre']}, USD {p['monto']} ({p['medio']})")
    lineas.append(f"\nTotal del viaje según el plan: USD {pagado + falta}"
                  f" (tope: USD {plan['tope_usd']})")
    if plan.get("recordatorios"):
        lineas.append("\nPara no olvidar:")
        lineas += [f" • {r}" for r in plan["recordatorios"]]
    lineas.append("\nEscribime: ahorre 500  |  pague vuelos 4100  |  plan")
    return "\n".join(lineas)


def main():
    plan = cargar_plan()
    pide_plan = "--forzar" in sys.argv or SIMULAR or HOY.day == 1
    for texto in leer_mensajes():
        try:
            respuesta, pide = procesar(texto, plan)
        except Exception as e:
            respuesta, pide = f"No pude anotar '{texto}': {e}", False
        if respuesta:
            avisar(respuesta)
        pide_plan = pide_plan or pide
    if not pide_plan:
        print("Hoy no toca mandar el plan.")
        return
    avisar(armar_plan(plan, leer_movimientos()))


if __name__ == "__main__":
    main()
