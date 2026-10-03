# Atalaya — datos diarios

Cada madrugada (04:20 en Madrid en verano) este repositorio recoge los vídeos nuevos de los canales de `canales.json`, guarda sus miniaturas en `thumbs/` y deja el resumen en `data/latest.json`. A las 04:50 la búsqueda diaria de Claude lee estos ficheros, traduce los títulos, escribe una reseña de cada vídeo y lo pasa todo a la app Atalaya del Canal.

- Para añadir o quitar un canal, edita `canales.json`. El campo `youtube` admite un identificador `@usuario`, un identificador de canal `UC…` o `buscar:texto` para que el proceso lo encuentre solo.
- Para lanzarlo a mano: pestaña **Actions** → «Recoger novedades de los canales» → **Run workflow**.
- Los fallos de un canal no paran el resto: quedan anotados en `errores` dentro de `data/latest.json`.
