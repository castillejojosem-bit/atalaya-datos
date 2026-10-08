#!/usr/bin/env python3
"""Recoge cada madrugada los vídeos nuevos de los canales del mapa.

Solo usa la biblioteca estándar de Python. Lee canales.json, consulta la
YouTube Data API v3 (API oficial, no RSS ni scraping) para cada canal,
guarda las miniaturas de los vídeos nuevos en data/thumbs/ y escribe
data/AAAA-MM-DD.json y data/latest.json. La tarea de las 5:00 de Claude
lee esos ficheros y los pasa a la app.

Necesita la variable de entorno YT_API_KEY (secreto "YT_API_KEY" en la
configuración de GitHub Actions del repositorio) con una clave de la API
de datos de YouTube v3, activada en Google Cloud Console.

Coste de cuota por ejecución, una vez la caché está caliente (los canales
ya resueltos no vuelven a pedirse): ~1 unidad por canal para la lista de
"Subidas" la primera vez (luego cae a 0, se guarda en state/playlists.json),
1 unidad por canal para los vídeos recientes, y 1 unidad cada 50 vídeos
para sus detalles — unas 60-100 unidades/noche sobre una cuota diaria de
10.000. Resolver un canal nuevo por "buscar:texto" cuesta 100 unidades,
pero solo la primera vez (se guarda en state/channel_ids.json).
"""
import json, os, re, statistics, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://www.googleapis.com/youtube/v3/"
CLAVE = os.environ.get("YT_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (atalaya-datos; +https://github.com)"}
GIGANTES = {"UC2D2CMWXMOVWx7giW1n3LIg", "UC3w193M5tYPJqF0Hi-7U-2g", "UC8kGsMa0LygSX9nkBcBH1Sg"}
VENTANA_NUEVOS_H = 50           # un vídeo es «nuevo» si se publicó en las últimas 50 horas
OUTLIER_MIN_DIAS, OUTLIER_MAX_DIAS = 7, 120
VIDEOS_POR_CANAL = 15           # cuántos vídeos recientes se piden por canal
ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


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


def api(recurso, **params):
    """Llama a un endpoint de la YouTube Data API v3 y devuelve el JSON."""
    params["key"] = CLAVE
    url = API + recurso + "?" + urllib.parse.urlencode(params)
    for intento in range(3):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")
            if e.code == 403 and "quotaExceeded" in cuerpo:
                raise RuntimeError("cuota diaria de la API de YouTube agotada")
            if intento == 2:
                raise RuntimeError(f"HTTP {e.code} en {recurso}: {cuerpo[:200]}")
            time.sleep(2 + intento * 3)
        except Exception:
            if intento == 2:
                raise
            time.sleep(2 + intento * 3)
    raise RuntimeError("sin respuesta: " + recurso)


def resolver_canal(yt, cache):
    """Convierte el identificador de canales.json en un channel_id (UC…), con caché."""
    if yt in cache:
        return cache[yt]
    cid = None
    if yt.startswith("UC") and len(yt) == 24:
        cid = yt
    elif yt.startswith("@"):
        d = api("channels", part="id", forHandle=yt)
        items = d.get("items", [])
        if items:
            cid = items[0]["id"]
    elif yt.startswith("buscar:"):
        d = api("search", part="snippet", type="channel", maxResults=1, q=yt[7:])
        items = d.get("items", [])
        if items:
            cid = items[0]["id"]["channelId"]
    if cid:
        cache[yt] = cid
    return cid


def playlist_subidas(cid, cache_playlists):
    """Id de la lista de reproducción «Subidas» del canal, con caché."""
    if cid in cache_playlists:
        return cache_playlists[cid]
    d = api("channels", part="contentDetails", id=cid)
    items = d.get("items", [])
    if not items:
        return None
    pl = items[0]["contentDetails"]["relatedPlaylists"].get("uploads")
    if pl:
        cache_playlists[cid] = pl
    return pl


def videos_recientes(playlist_id):
    """Los últimos VIDEOS_POR_CANAL vídeos subidos a la lista."""
    d = api("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=VIDEOS_POR_CANAL)
    return [it["contentDetails"]["videoId"] for it in d.get("items", [])
            if it.get("contentDetails", {}).get("videoId")]


def duracion_segundos(iso):
    m = ISO_DUR.fullmatch(iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def detalles_videos(ids):
    """snippet + statistics + contentDetails de hasta 50 ids por llamada."""
    detalles = {}
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        d = api("videos", part="snippet,statistics,contentDetails", id=",".join(lote))
        for it in d.get("items", []):
            sn, st, cd = it["snippet"], it.get("statistics", {}), it.get("contentDetails", {})
            dur = duracion_segundos(cd.get("duration"))
            detalles[it["id"]] = {
                "titulo": sn.get("title", ""),
                "publicado": sn.get("publishedAt", ""),
                "descripcion": (sn.get("description") or "")[:1500],
                "vistas": int(st["viewCount"]) if st.get("viewCount") is not None else None,
                # Youtube permite Shorts de hasta 3 minutos: duración <= 180s es la aproximación.
                "es_short": dur is not None and dur <= 180,
            }
    return detalles


def bajar_mini(vid, errores, canal):
    ruta = os.path.join(RAIZ, "data", "thumbs", vid + ".jpg")
    if os.path.exists(ruta):
        return "data/thumbs/" + vid + ".jpg"
    try:
        req = urllib.request.Request(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            img = r.read()
        with open(ruta, "wb") as f:
            f.write(img)
        return "data/thumbs/" + vid + ".jpg"
    except Exception as ex:
        errores.append(f"{canal}: miniatura de {vid} no descargada ({ex})")
        return None


def main():
    if not CLAVE:
        print("Falta la variable de entorno YT_API_KEY (secreto de GitHub Actions)", file=sys.stderr)
        return 1

    ahora = datetime.now(timezone.utc)
    hoy = (ahora + timedelta(hours=2)).strftime("%Y-%m-%d")  # fecha de Madrid aproximada
    canales = leer("canales.json", {"canales": []})["canales"]
    cache = leer("state/channel_ids.json", {})
    cache_playlists = leer("state/playlists.json", {})
    vistos = set(leer("state/vistos.json", []))
    nuevos, outliers, errores = [], [], []

    for c in canales:
        try:
            cid = resolver_canal(c["youtube"], cache)
            if not cid:
                errores.append(f'{c["nombre"]}: no se encontró el canal ({c["youtube"]})')
                continue
            pl = playlist_subidas(cid, cache_playlists)
            if not pl:
                errores.append(f'{c["nombre"]}: sin lista de vídeos subidos')
                continue
            ids = videos_recientes(pl)
            det = detalles_videos(ids) if ids else {}
        except Exception as ex:
            errores.append(f'{c["nombre"]}: {ex}')
            continue

        # outliers: vídeos de entre 7 y 120 días frente a la mediana de los recientes del canal
        con_edad = []
        for vid in ids:
            info = det.get(vid)
            if not info or not info["publicado"]:
                continue
            try:
                pub = datetime.fromisoformat(info["publicado"].replace("Z", "+00:00"))
            except ValueError:
                continue
            info["_edad_h"] = (ahora - pub).total_seconds() / 3600
            if info["vistas"] is not None:
                con_edad.append(info)

        base = [v["vistas"] for v in con_edad if v["_edad_h"] >= OUTLIER_MIN_DIAS * 24]
        mediana = statistics.median(base) if len(base) >= 5 else None
        umbral = 3 if cid in GIGANTES else 5

        for vid in ids:
            info = det.get(vid)
            if not info or not info["publicado"]:
                continue
            mult = round(info["vistas"] / mediana, 1) if (mediana and info.get("vistas")) else None
            ficha = {"canal": c["nombre"], "n": c["n"], "seccion": c["seccion"], "idioma": c["idioma"],
                     "channel_id": cid, "video_id": vid, "url": "https://www.youtube.com/watch?v=" + vid,
                     "titulo_original": info["titulo"], "publicado": info["publicado"],
                     "descripcion": info["descripcion"], "vistas": info["vistas"],
                     "mediana_canal": mediana, "multiplicador": mult, "es_short": info["es_short"]}
            edad_h = info.get("_edad_h", 1e9)
            if vid not in vistos and edad_h <= VENTANA_NUEVOS_H:
                ficha["miniatura"] = bajar_mini(vid, errores, c["nombre"])
                nuevos.append(ficha)
                vistos.add(vid)
            if mult and mult >= umbral and OUTLIER_MIN_DIAS * 24 <= edad_h <= OUTLIER_MAX_DIAS * 24:
                if "miniatura" not in ficha:
                    ficha["miniatura"] = bajar_mini(vid, errores, c["nombre"])
                outliers.append(ficha)

    # miniaturas que pide la búsqueda de Claude (outliers que llegan por vidIQ)
    pedidas = leer("state/pedir_miniaturas.json", [])
    for vid in pedidas:
        if re.fullmatch(r"[\w-]{11}", str(vid)):
            bajar_mini(vid, errores, "pedida")
    escribir("state/pedir_miniaturas.json", [])

    # poda: miniaturas de más de 30 días
    for f in os.listdir(os.path.join(RAIZ, "data", "thumbs")):
        p = os.path.join(RAIZ, "data", "thumbs", f)
        if f.endswith(".jpg") and time.time() - os.path.getmtime(p) > 30 * 86400:
            os.remove(p)

    salida = {"fecha": hoy, "generado": ahora.isoformat(timespec="seconds"),
              "nuevos": sorted(nuevos, key=lambda x: x["publicado"], reverse=True),
              "outliers": sorted(outliers, key=lambda x: x["multiplicador"] or 0, reverse=True),
              "errores": errores, "canales_revisados": len(canales)}
    escribir(f"data/{hoy}.json", salida)
    escribir("data/latest.json", salida)
    escribir("state/channel_ids.json", cache)
    escribir("state/playlists.json", cache_playlists)
    escribir("state/vistos.json", sorted(vistos)[-5000:])
    print(f"{len(nuevos)} nuevos, {len(outliers)} outliers, {len(errores)} errores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
