"""Генератор новых иконок пака (белые контурные SVG 512 → PNG 100×100 с альфой) в стиле main/.
Запуск: python3 emoji_pack/tools/gen_new_icons.py  (нужен playwright+chromium).
Добавляет иконки в emoji_pack/svg/main, emoji_pack/main и дописывает emoji_pack/main/manifest.json."""
import json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
S = 'stroke="#fff" stroke-width="40" stroke-linecap="round" stroke-linejoin="round" fill="none"'
F = 'fill="#fff"'


def P(d, extra=S):
    return f'<path d="{d}" {extra}/>'


def C(cx, cy, r, extra=S):
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" {extra}/>'


HEART = "M256 440C120 340 70 260 70 190a96 96 0 0 1 186-34 96 96 0 0 1 186 34c0 70-50 150-186 250Z"
SPEAKER = "M70 200h80l110-90v290l-110-90H70Z"

ICONS = {
    # имя: ([глифы], тело svg)
    "beach": (["🏖"], P("M70 270a186 186 0 0 1 372 0Z", F) + P("M256 270v150a40 40 0 0 1-80 0") + P("M60 452h392")),
    "shield": (["🛡"], P("M256 60l160 60v110c0 110-70 180-160 222C166 410 96 340 96 230V120Z")),
    "heart": (["🤍", "💖", "💞", "💘", "💕", "💗", "❤"], P(HEART)),
    "brokenheart": (["💔"], P(HEART) + P("M285 120l-55 90 70 55-60 100")),
    "coin": (["🪙"], C(256, 256, 190) + P("M310 200c-12-36-100-36-100 10 0 56 100 24 100 80 0 44-90 46-104 8M256 140v232")),
    "excl": (["❕", "❗"], P("M256 80v220") + C(256, 410, 30, F)),
    "hammer": (["🔨"], P("M215 130l90-60 140 140-60 90Z", F) + P("M265 260L90 435", S.replace("40", "56"))),
    "link": (["🔗"], P("M220 292a80 80 0 0 0 113 0l70-70a80 80 0 0 0-113-113l-35 35M292 220a80 80 0 0 0-113 0l-70 70a80 80 0 0 0 113 113l35-35")),
    "chartup": (["📈"], P("M80 70v370h360M140 340l90-90 60 60 110-140M350 170h50v50")),
    "chartdown": (["📉"], P("M80 70v370h360M140 170l90 90 60-60 110 140M350 340h50v-50")),
    "mute": (["🔇", "🔕"], P(SPEAKER) + P("M350 200l110 110M460 200L350 310")),
    "swords": (["⚔"], P("M100 100l250 250M412 100L162 350M80 330l100 100M432 330L332 430M60 450l50-50M452 450l-50-50")),
    "ring": (["💍"], C(256, 320, 120) + P("M200 160l56-70 56 70-56 50Z")),
    "flame": (["🔥"], P("M256 450c-90 0-150-60-150-140 0-80 70-130 90-220 40 40 60 80 60 120 30-20 40-60 40-90 50 50 110 110 110 190 0 80-60 140-150 140Z")),
    "hash": (["🔢"], P("M200 90l-30 332M340 90l-30 332M100 190h330M80 320h330")),
    "flag": (["🏳", "🏁", "🚩"], P("M120 70v380M120 90h270l-50 70 50 70H120")),
    "doc": (["📜", "📄", "📃"], P("M130 70h250v372H130Z") + P("M180 160h150M180 240h150M180 320h100")),
    "gift": (["🎁"], P("M90 220h332v220H90ZM70 140h372v80H70ZM256 140v300") + P("M256 140c-60-80-130-30-90 0M256 140c60-80 130-30 90 0")),
    "arrowdown": (["⬇"], P("M256 90v320M150 300l106 110 106-110")),
    "arrowup": (["⬆"], P("M256 420V100M150 210L256 100l106 110")),
    "arrowleft": (["◀", "⬅"], P("M420 256H100M210 150L100 256l110 106")),
    "door": (["🚪"], P("M150 70h212v372H150ZM110 442h292") + C(320, 262, 16, F)),
    "bookmark": (["🔖"], P("M150 70h212v372l-106-80-106 80Z")),
    "circle": (["⭕"], C(256, 256, 190)),
    "disk": (["💾"], P("M90 90h270l62 62v270H90Z") + P("M170 90v100h170V90M170 422V300h170v122")),
    "rocket": (["🚀"], P("M256 60c80 60 110 150 80 260H176c-30-110 0-200 80-260Z") + C(256, 200, 34) + P("M176 320l-60 80 80-20M336 320l60 80-80-20M256 360v70")),
    "key": (["🔑"], C(170, 342, 84) + P("M230 282L420 92M360 152l50 50M310 202l40 40")),
    "unlock": (["🔓"], P("M110 240h292v200H110Z") + P("M170 240V170a86 86 0 0 1 166-30")),
    "robot": (["🤖"], P("M110 170h292v240H110ZM256 170V90") + C(256, 70, 20, F) + C(190, 270, 28, F) + C(322, 270, 28, F) + P("M190 350h132")),
    "dice": (["🎲"], P("M110 110h292v292H110Z") + "".join(C(x, y, 26, F) for x, y in ((190, 190), (322, 190), (256, 256), (190, 322), (322, 322)))),
    "target": (["🎯"], C(256, 256, 190) + C(256, 256, 110) + C(256, 256, 30, F)),
    "sound": (["🔊"], P(SPEAKER) + P("M340 200a80 80 0 0 1 0 112M390 150a150 150 0 0 1 0 212")),
    "checkbox": (["☑", "✔"], P("M110 110h292v292H110Z") + P("M170 270l60 60 120-140")),
    "box": (["⬜", "☐"], P("M110 110h292v292H110Z")),
    "play": (["▶"], P("M150 90l260 166-260 166Z", S + " " + F.replace("fill=", "data-f=")) .replace('fill="none"', 'fill="#fff"')),
    "books": (["📚", "📖", "📕"], P("M110 70h100v372H110ZM230 70h100v372H230ZM350 110l90-20 60 340-90 20Z")),
    "home": (["🏠"], P("M70 250L256 80l186 170M120 220v220h272V220M220 440V320h72v120")),
    "plane": (["✈"], P("M256 70c20 0 30 20 30 40v110l150 100v50l-150-40v100l50 40v30l-80-20-80 20v-30l50-40V330l-150 40v-50l150-100V110c0-20 10-40 30-40Z")),
    "next": (["⏭"], P("M120 110l200 146-200 146Z", S.replace('fill="none"', 'fill="#fff"')) + P("M380 100v312")),
    "square": (["⏹"], P("M120 120h272v272H120Z", S.replace('fill="none"', 'fill="#fff"'))),
    "clock": (["🕒", "🕓", "🕐", "🕑", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛", "⏲"], C(256, 256, 190) + P("M256 140v120l80 50")),
    "wave": (["👋"], P("M140 290V150a30 30 0 0 1 60 0v100") + P("M200 250V120a30 30 0 0 1 60 0v130") + P("M260 250V130a30 30 0 0 1 60 0v120") + P("M320 260V170a30 30 0 0 1 60 0v150c0 90-50 150-130 150h-40c-60 0-90-30-120-80l-50-90a30 30 0 0 1 50-34l40 50")),
    "snow": (["❄"], P("M256 60v392M90 158l332 196M90 354l332-196") + P("M226 96l30 30 30-30M226 416l30-30 30 30")),
    "laptop": (["💻"], P("M130 110h252v200H130Z") + P("M70 350h372l-30 50H100Z")),
    "castle": (["🏰"], P("M90 440V170h70v50h40v-50h152v50h40v-50h70v270Z") + P("M220 440v-90a36 36 0 0 1 72 0v90")),
    "palette": (["🎨"], P("M256 66a190 190 0 1 0 0 380c30 0 40-20 30-40s0-40 30-40h50a60 60 0 0 0 60-60A190 190 0 0 0 256 66Z") + C(170, 230, 26, F) + C(250, 170, 26, F) + C(340, 200, 26, F)),
    "sun": (["☀"], C(256, 256, 96) + P("M256 40v64M256 408v64M40 256h64M408 256h64M99 99l45 45M368 368l45 45M99 413l45-45M368 144l45-45")),
    "menu": (["☰"], P("M90 150h332M90 256h332M90 362h332")),
    "briefcase": (["💼"], P("M70 170h372v260H70Z") + P("M190 170V120h132v50M70 280h372")),
    "dagger": (["🗡"], P("M256 60l36 40v240h-72V100Z") + P("M130 340h252M240 360v90h32v-90")),
    "fist": (["👊"], P("M120 230V170a30 30 0 0 1 60 0v-20a30 30 0 0 1 60 0v20a30 30 0 0 1 60 0v10a30 30 0 0 1 60 0v150a120 120 0 0 1-240 0Z")),
    "film": (["🎬", "🎞"], P("M90 120h332v272H90Z") + P("M90 120v272M422 120v272M90 190h60M90 322h60M362 190h60M362 322h60")),
    "mic": (["🎤"], P("M206 100a50 50 0 0 1 100 0v140a50 50 0 0 1-100 0Z") + P("M140 240a116 116 0 0 0 232 0M256 356v70M200 426h112")),
    "music": (["🎵"], C(180, 380, 50) + C(360, 340, 50) + P("M230 380V120l180-60v260")),
    "clip": (["📎"], P("M330 250l-130 130a70 70 0 0 1-100-100l140-140a50 50 0 0 1 70 70L190 320a30 30 0 0 1-42-42l130-130")),
    "smile": (["🙂"], C(256, 256, 190) + P("M170 300c50 60 122 60 172 0") + C(200, 210, 20, F) + C(312, 210, 20, F)),
}

# дополнительные глифы для уже существующих иконок
ALT = {"envelope": ["📨", "📤", "💌", "✉"], "no": ["⛔"], "cross": ["✖"], "pin": ["📍"], "camera": ["🖼"], "search": ["🔎"],
       "hourglass": ["⌛"], "refresh": ["🔁", "🔀", "♻", "↪"], "trophy": ["🏅", "🎖"], "lock": ["🔐"]}


def svg(body):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
            '<g transform="translate(256 256) scale(.8) translate(-256 -256)">' + body + "</g></svg>")


def thumbdown():
    src = open(os.path.join(ROOT, "svg/main/thumb.svg"), encoding="utf-8").read()
    i = src.index(">", src.index("<svg")) + 1
    j = src.rindex("</svg>")
    return src[:i] + '<g transform="translate(0 24) scale(1 -1)">' + src[i:j] + "</g>" + src[j:]


def main():
    from playwright.sync_api import sync_playwright
    man_path = os.path.join(ROOT, "main/manifest.json")
    man = json.load(open(man_path, encoding="utf-8"))
    used = {g.replace("️", "") for v in man["icons"].values() for g in v}
    files = {}
    for name, (glyphs, body) in ICONS.items():
        files[name] = svg(body)
        man["icons"].setdefault(name, glyphs)
    files["thumbdown"] = thumbdown()
    man["icons"].setdefault("thumbdown", ["👎"])
    for name, extra in ALT.items():
        cur = man["icons"][name]
        for g in extra:
            if g.replace("️", "") not in {x.replace("️", "") for x in cur}:
                cur.append(g)
    # проверка: глифы не повторяются между иконками
    seen = {}
    for n, gl in man["icons"].items():
        for g in gl:
            k = g.replace("️", "")
            assert k not in seen or seen[k] == n, f"дубль {g}: {seen[k]} и {n}"
            seen[k] = n
    os.makedirs(os.path.join(ROOT, "svg/main"), exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 100, "height": 100})
        for name, s in files.items():
            open(os.path.join(ROOT, f"svg/main/{name}.svg"), "w", encoding="utf-8").write(s)
            pg.set_content(f'<html><body style="margin:0;background:transparent"><div style="width:100px;height:100px">'
                           f'<img src="data:image/svg+xml;utf8,{__import__("urllib.parse").parse.quote(s)}" width="100" height="100"></div></body></html>')
            pg.wait_for_timeout(50)
            pg.screenshot(path=os.path.join(ROOT, f"main/{name}.png"), omit_background=True, clip={"x": 0, "y": 0, "width": 100, "height": 100})
        br.close()
    json.dump(man, open(man_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("иконок в паке:", len(man["icons"]))


if __name__ == "__main__":
    main()
