#!/usr/bin/env python3
"""Recoge cada madrugada los vídeos nuevos de los canales del mapa.

Solo usa la biblioteca estándar de Python. Lee canales.json, consulta el
feed público de cada canal en YouTube, guarda las miniaturas de los vídeos
nuevos en thumbs/ y escribe data/AAAA-MM-DD.json y data/latest.json.
La tarea de las 5:00 de Claude lee esos ficheros y los pasa a la app.
"""
import json, os, re, statistics, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (atalaya-datos; +https://github.com)",
      "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
      "Cookie": "CONSENT=YES+cb; SOCS=CAI"}
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
GIGANTES = {"UC2D2CMWXMOVWx7giW1n3LIg", "UC3w193M5tYPJqF0Hi-7U-2g", "UC8kGsMa0LygSX9nkBcBH1Sg"}
VENTANA_NUEVOS_H = 50          # un vídeo es «nuevo» si se publicó en las últimas 50 horas
OUTLIER_MIN_DIAS, OUTLIER_MAX_DIAS = 7, 120


def leer(ruta, defecto):
    try:
        with open(os.path.join(RAIZ, ruta), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def escribir(ruta, datos):
    p = os.path.join(RAIZ, ruta)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)


def http(url, binario=False, intentos=3):
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                cuerpo = r.read()
                return cuerpo if binario else cuerpo.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if i == intentos - 1:
                raise
            time.sleep(2 + i * 3)
        except Exception:
            time.sleep(2 + i * 3)
    raise RuntimeError("sin respuesta: " + url)


def resolver(yt, cache):
    if yt in cache:
        return cache[yt]
    cid = None
    if yt.startswith("UC") and len(yt) == 24:
        cid = yt
    elif yt.startswith("@"):
        html = http("https://www.youtube.com/" + urllib.parse.quote(yt))
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
             or re.search(r'"externalId":"(UC[\w-]{22})"', html)
             or re.search(r'"channelId":"(UC[\w-]{22})"', html))
        cid = m.group(1) if m else None
    elif yt.startswith("buscar:"):
        q = urllib.parse.quote(yt[7:])
        html = http("https://www.youtube.com/results?search_query=" + q + "&sp=EgIQAg%253D%253D")
        m = re.search(r'"channelRenderer":\{"channelId":"(UC[\w-]{22})"', html) or re.search(r'"channelId":"(UC[\w-]{22})"', html)
        cid = m.group(1) if m else None
    if cid:
        cache[yt] = cid
    return cid


def es_short(vid):
    try:
        req = urllib.request.Request("https://www.youtube.com/shorts/" + vid, headers=UA, method="HEAD")

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedir)
        with op.open(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def feed(cid):
    # El feed de YouTube a veces responde 404 sin motivo: se reintenta y se prueba
    # también la lista de subidas del canal (UULF = solo vídeos largos, UU = todos).
    base = "https://www.youtube.com/feeds/videos.xml?"
    urls = [base + "playlist_id=UULF" + cid[2:], base + "channel_id=" + cid, base + "playlist_id=UU" + cid[2:]]
    xml, ultimo = None, None
    for vuelta in range(3):
        for u in urls:
            try:
                xml = http(u, intentos=1)
                break
            except Exception as ex:
                ultimo = ex
                time.sleep(2)
        if xml:
            break
        time.sleep(5 + vuelta * 5)
    if not xml:
        raise RuntimeError(f"feed no disponible ({ultimo})")
    raiz = ET.fromstring(xml)
    videos = []
    for e in raiz.findall("a:entry", NS):
        g = e.find("media:group", NS)
        stats = g.find("media:community/media:statistics", NS) if g is not None else None
        videos.append({
            "id": e.findtext("yt:videoId", default="", namespaces=NS),
            "titulo": e.findtext("a:title", default="", namespaces=NS),
            "publicado": e.findtext("a:published", default="", namespaces=NS),
            "descripcion": (g.findtext("media:description", default="", namespaces=NS) if g is not None else "")[:1500],
            "vistas": int(stats.get("views", "0")) if stats is not None else None,
        })
    return videos


def main():
    ahora = datetime.now(timezone.utc)
    hoy = (ahora + timedelta(hours=2)).strftime("%Y-%m-%d")  # fecha de Madrid aproximada
    canales = leer("canales.json", {"canales": []})["canales"]
    cache = leer("state/channel_ids.json", {})
    vistos = set(leer("state/vistos.json", []))
    nuevos, outliers, errores = [], [], []

    for c in canales:
        try:
            cid = resolver(c["youtube"], cache)
            if not cid:
                errores.append(f'{c["nombre"]}: no se encontró el canal ({c["youtube"]})')
                continue
            vids = feed(cid)
        except Exception as ex:
            errores.append(f'{c["nombre"]}: {ex}')
            continue
        # outliers: vídeos de entre 7 y 120 días frente a la mediana de su propio feed
        con_edad = []
        for v in vids:
            try:
                pub = datetime.fromisoformat(v["publicado"].replace("Z", "+00:00"))
            except ValueError:
                continue
            v["_edad_h"] = (ahora - pub).total_seconds() / 3600
            if v["vistas"] is not None:
                con_edad.append(v)
        base = [v["vistas"] for v in con_edad if v["_edad_h"] >= OUTLIER_MIN_DIAS * 24]
        mediana = statistics.median(base) if len(base) >= 5 else None
        umbral = 3 if cid in GIGANTES else 5
        for v in vids:
            mult = round(v["vistas"] / mediana, 1) if (mediana and v.get("vistas")) else None
            ficha = {"canal": c["nombre"], "n": c["n"], "seccion": c["seccion"], "idioma": c["idioma"],
                     "channel_id": cid, "video_id": v["id"], "url": "https://www.youtube.com/watch?v=" + v["id"],
                     "titulo_original": v["titulo"], "publicado": v["publicado"], "descripcion": v["descripcion"],
                     "vistas": v["vistas"], "mediana_canal": mediana, "multiplicador": mult}
            if v["id"] not in vistos and v.get("_edad_h", 1e9) <= VENTANA_NUEVOS_H:
                ficha["es_short"] = es_short(v["id"])
                try:
                    img = http(f'https://i.ytimg.com/vi/{v["id"]}/mqdefault.jpg', binario=True)
                    with open(os.path.join(RAIZ, "thumbs", v["id"] + ".jpg"), "wb") as f:
                        f.write(img)
                    ficha["miniatura"] = "thumbs/" + v["id"] + ".jpg"
                except Exception as ex:
                    ficha["miniatura"] = None
                    errores.append(f'{c["nombre"]}: miniatura de {v["id"]} no descargada ({ex})')
                nuevos.append(ficha)
                vistos.add(v["id"])
            if mult and mult >= umbral and OUTLIER_MIN_DIAS * 24 <= v.get("_edad_h", 0) <= OUTLIER_MAX_DIAS * 24:
                outliers.append(ficha)
        time.sleep(1)

    # poda: miniaturas de más de 30 días
    for f in os.listdir(os.path.join(RAIZ, "thumbs")):
        p = os.path.join(RAIZ, "thumbs", f)
        if f.endswith(".jpg") and time.time() - os.path.getmtime(p) > 30 * 86400:
            os.remove(p)

    salida = {"fecha": hoy, "generado": ahora.isoformat(timespec="seconds"),
              "nuevos": sorted(nuevos, key=lambda x: x["publicado"], reverse=True),
              "outliers": sorted(outliers, key=lambda x: x["multiplicador"] or 0, reverse=True),
              "errores": errores, "canales_revisados": len(canales)}
    escribir(f"data/{hoy}.json", salida)
    escribir("data/latest.json", salida)
    escribir("state/channel_ids.json", cache)
    escribir("state/vistos.json", sorted(vistos)[-5000:])
    print(f"{len(nuevos)} nuevos, {len(outliers)} outliers, {len(errores)} errores")


if __name__ == "__main__":
    sys.exit(main())
