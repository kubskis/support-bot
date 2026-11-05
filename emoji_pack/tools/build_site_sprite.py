"""Собирает icons.svg для сайта: символы из наших иконок пака (emoji_pack/svg/main),
цвет берётся из текста (currentColor), поэтому иконки работают и в тёмной, и в светлой теме.
Запуск: python3 emoji_pack/tools/build_site_sprite.py  (пишет icons.svg в корень сайта)."""
import json, os, re

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.path.join(ROOT, "..", ".."))
PACK = os.path.join(ROOT, "..")

# глиф (как в тексте сайта) -> имя иконки в паке; глифы из манифеста подхватываются сами
EXTRA = {"★": "star", "✓": "check", "←": "arrowleft", "→": "arrow", "➔": "arrow", "⚪": "circle",
         "⭐": "star", "❄": "snow", "❄️": "snow", "☀": "sun", "☀️": "sun", "🕵": "spy", "🤬": "warn",
         "🔞": "no", "🫣": "eye", "👵": "user", "🗡": "dagger", "👊": "fist", "🎞": "film", "🧑\u200d💻": "dev", "💸": "money"}
EXCLUDE_SITE = set()  # глифы, которые на сайте не трогаем


def body_of(path, name):
    src = open(path, encoding="utf-8").read()
    vb = re.search(r'viewBox="([^"]+)"', src).group(1)
    i = src.index(">", src.index("<svg")) + 1
    j = src.rindex("</svg>")
    inner = src[i:j]
    inner = re.sub(r'rgb\(100%,\s*100%,\s*100%\)|#fff(?:fff)?\b|\bwhite\b', "currentColor", inner, flags=re.I)
    inner = re.sub(r'<\?xml[^>]*>', "", inner)
    # Safari не применяет маски (clipPath) из внешнего файла: фигуру из маски кладём обычным путём
    clips = re.findall(r'<clipPath id="[^"]+">(.*?)</clipPath>', inner, re.S)
    if clips:
        shape = [d for d in re.findall(r'<path[^>]*d="([^"]+)"', clips[-1])]
        if shape:
            inner = "".join(f'<path d="{d}" fill="currentColor"/>' for d in shape)
    # id внутри пака повторяются (clip-0 и т.д.): делаем их уникальными для каждого символа
    inner = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{name}-{m.group(1)}"', inner)
    inner = re.sub(r'url\(#([^)]+)\)', lambda m: f'url(#{name}-{m.group(1)})', inner)
    return vb, inner


def main():
    man = json.load(open(os.path.join(PACK, "main", "manifest.json"), encoding="utf-8"))["icons"]
    glyph2name = {}
    for name, glyphs in man.items():
        for g in glyphs:
            glyph2name.setdefault(g.replace("️", ""), name)
    for g, name in EXTRA.items():
        glyph2name.setdefault(g.replace("️", ""), name)
    names = sorted(set(glyph2name.values()))
    syms = []
    for name in names:
        p = os.path.join(PACK, "svg", "main", name + ".svg")
        if not os.path.isfile(p):
            continue
        vb, inner = body_of(p, 'i-' + name)
        syms.append(f'<symbol id="i-{name}" viewBox="{vb}">{inner}</symbol>')
    out = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="0" height="0" style="position:absolute" aria-hidden="true">' + "".join(syms) + "</svg>")
    open(os.path.join(SITE, "icons.svg"), "w", encoding="utf-8").write(out)
    # карта глиф -> id для сайта (встраивается в site.js отдельным файлом не нужно: читаем из icons-map.json)
    cmap = {g: "i-" + n for g, n in glyph2name.items() if os.path.isfile(os.path.join(PACK, "svg", "main", n + ".svg"))}
    # карта вшивается в site.js между маркерами (отдельного запроса не нужно)
    js_path = os.path.join(SITE, "site.js")
    js = open(js_path, encoding="utf-8").read()
    block = ("/*ICONMAP-START*/var ICON_MAP=" + json.dumps(cmap, ensure_ascii=False, sort_keys=True) + ";/*ICONMAP-END*/")
    if "/*ICONMAP-START*/" in js:
        js = re.sub(r"/\*ICONMAP-START\*/.*?/\*ICONMAP-END\*/", lambda m: block, js, flags=re.S)
    else:
        js = block + "\n" + js
    open(js_path, "w", encoding="utf-8").write(js)
    print("символов:", len(syms), "глифов:", len(cmap))


if __name__ == "__main__":
    main()
