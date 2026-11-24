#!/usr/bin/env python3
"""WhatsApp / iMessage chat word counter -> shareable stats card + word cloud (one PNG per chat).

Usage: python3 wac.py CHAT [CHAT ...] [--outdir .] [--title "Name"] [--top 30]
       python3 wac.py --imessage                 # list your Messages chats
       python3 wac.py --imessage 3 "Mum" [--me "Your name"]
       [--keep-stopwords] [--bg "#ffffff" | --bg photo.jpg] [--font path.ttc] [--people 8] [--contrast 3]
CHAT can be the exported _chat.txt or the exported .zip.
Handles both iOS ("[dd/mm/yy, hh:mm:ss] Name: msg") and
Android ("dd/mm/yy, hh:mm - Name: msg") export formats.
"""
import argparse
import io
import math
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

MSG_RE = re.compile(
    r"^\[?(\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)\]?"
    r"\s*(?:-\s*)?([^:]+?):\s(.*)$"
)
SKIP = re.compile(
    r"(<media omitted>|<attached:|(image|video|audio|sticker|gif|document) omitted"
    r"|this message was deleted|you deleted this message|<this message was edited>"
    r"|messages and calls are end-to-end encrypted|^null$)",
    re.I,
)
URL = re.compile(r"https?://\S+|www\.\S+")
WORD = re.compile(r"[a-zA-ZÀ-ɏ']+")
STOP = set("""
a about above after again against all am an and any are aren't as at be because been before being
below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down
during each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here
here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself
let's me more most mustn't my myself no nor not of off on once only or other ought our ours ourselves
out over own same shan't she she'd she'll she's should shouldn't so some such than that that's the their
theirs them themselves then there there's these they they'd they'll they're they've this those through
to too under until up very was wasn't we we'd we'll we're we've were weren't what what's when when's
where where's which while who who's whom why why's with won't would wouldn't you you'd you'll you're
you've your yours yourself yourselves im ive dont cant didnt doesnt isnt wont its thats u ur just like
get got oh ok okay yeah yes also will one go going know think s t ll re ve d m
""".split())

PALETTE = ["#c8553d", "#2f6690", "#e0a526", "#4f7d5c", "#7a5c99", "#d17a22", "#3a8f8a", "#a8426b"]
OTHER = (160, 160, 160)  # colour for people beyond --people


def clean(text):
    # iOS exports use curly apostrophes (don’t) – normalise so they don't split into "don" + "t"
    return (text.replace("‎", "").replace(" ", " ").replace(" ", " ")
                .replace("’", "'").replace("‘", "'").replace("ʼ", "'"))


def read_lines(path):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not name:
                sys.exit(f"No .txt chat inside {path}")
            return io.TextIOWrapper(io.BytesIO(z.read(name)), encoding="utf-8", errors="replace").readlines()
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def parse(path):
    """Yield (date, sender, text) for each message, merging multi-line messages."""
    cur, buf = None, []
    for line in read_lines(path):
        line = clean(line.rstrip("\n"))
        m = MSG_RE.match(line)
        if m:
            if cur:
                yield (*cur, "\n".join(buf))
            cur, buf = (m.group(1), m.group(3).strip()), [m.group(4)]
        elif cur:
            buf.append(line)
    if cur:
        yield (*cur, "\n".join(buf))


def tokenize(text):
    text = URL.sub(" ", text)
    return [w.strip("'").lower() for w in WORD.findall(text) if w.strip("'")]


def chat_title(path):
    base = os.path.splitext(os.path.basename(path))[0]
    if base.lower() in ("_chat", "chat"):
        base = os.path.basename(os.path.dirname(os.path.abspath(path))) or base
    return re.sub(r"^WhatsApp Chat( with| -)?\s*", "", base, flags=re.I).strip() or "WhatsApp chat"


def analyse(messages, keep_stop):
    words, msgs, freq, dates = Counter(), Counter(), defaultdict(Counter), []
    for date, sender, text in messages:
        text = clean(text)
        if SKIP.search(text) and len(text) < 60:
            continue
        toks = tokenize(text)
        dates.append(date)
        msgs[sender] += 1
        words[sender] += len(toks)
        freq[sender].update(w for w in toks if keep_stop or (w not in STOP and len(w) > 1))
    return words, msgs, freq, dates


def print_stats(title, words, msgs, freq, overall, top):
    grand = sum(words.values())
    print(f"\n##### {title} #####")
    print(f"=== Words per person (total: {grand:,} words, {sum(msgs.values()):,} messages) ===")
    print(f"{'#':>3}  {'Name':<28}{'Words':>10}{'Share':>8}{'Msgs':>8}{'W/msg':>7}")
    for i, (name, n) in enumerate(words.most_common(), 1):
        print(f"{i:>3}  {name[:27]:<28}{n:>10,}{n / grand:>8.1%}{msgs[name]:>8,}{n / msgs[name]:>7.1f}")
    print(f"\n=== Top {top} words overall ===")
    for i, (w, c) in enumerate(overall.most_common(top), 1):
        print(f"{i:>3}. {w:<20}{c:>8,}")
    for name, _ in words.most_common():
        print(f"\n[{name}] top words: " + ", ".join(f"{w} ({c})" for w, c in freq[name].most_common(10)))


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c):
    c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return max(0, min(255, round(c * 255)))


def srgb_to_oklab(rgb):
    r, g, b = (_lin(v) for v in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklab_to_srgb(lab):
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (_gam(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            _gam(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            _gam(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


# ---------------------------------------------------------------- rendering

def render_card(title, words, msgs, freq, overall, dates, args, out_path):
    try:
        from wordcloud import WordCloud
        from PIL import Image, ImageColor, ImageDraw, ImageFont
    except ImportError:
        sys.exit("\nInstall wordcloud for the image:  pip install wordcloud")

    people = [n for n, _ in words.most_common()]
    colours = {p: ImageColor.getrgb(PALETTE[i % len(PALETTE)]) for i, p in enumerate(people[:args.people])}

    labs = {p: srgb_to_oklab(colours.get(p, OTHER)) for p in people}
    totals = {p: sum(freq[p].values()) or 1 for p in people}

    def mix(word):
        # Hue always belongs to whoever uses the word most *relative to how much they talk*
        # (never an invented in-between colour). The size of their lead sets saturation:
        # shares are sharpened (share ** contrast) so even a slight edge reads clearly,
        # and only a near-dead heat fades towards grey.
        w = {p: (freq[p][word] / totals[p]) ** args.contrast for p in people}
        tot = sum(w.values()) or 1
        leader = max(w, key=w.get)
        n = sum(1 for v in w.values() if v) or 1
        lead = (w[leader] / tot - 1 / n) / (1 - 1 / n) if n > 1 else 1.0   # 0 = tie, 1 = only them
        strength = 0.35 + 0.65 * lead ** 0.5
        L, A, B = labs[leader]
        return oklab_to_srgb((L, A * strength, B * strength))

    have_font = os.path.exists(args.font)
    def F(size, bold=False):
        if not have_font:
            return ImageFont.load_default(size)
        try:
            return ImageFont.truetype(args.font, size, index=1 if bold else 0)
        except OSError:
            return ImageFont.truetype(args.font, size)

    bg_img = args.bg if os.path.isfile(args.bg) else None
    bg_rgb = (255, 255, 255) if bg_img else ImageColor.getrgb(args.bg)
    dark = not bg_img and (bg_rgb[0] * 299 + bg_rgb[1] * 587 + bg_rgb[2] * 114) / 1000 < 128
    fg = (235, 235, 235) if dark else (20, 20, 20)
    grey = (150, 150, 150) if dark else (120, 120, 120)
    rule = (70, 70, 70) if dark else (225, 225, 225)

    W, PAD = 1600, 90
    inner = W - 2 * PAD
    grand = sum(words.values())
    total_msgs = sum(msgs.values())
    max_rows = 12
    rows = people[:max_rows]
    ROW_H = 84
    COLS, LIST_ROWS = 3, 8
    top_words = overall.most_common(COLS * LIST_ROWS)
    list_h = 48 * min(LIST_ROWS, -(-len(top_words) // COLS))
    CLOUD_H = 820

    H = (PAD + 76 + 50 + 50            # title, subtitle
         + 50 + 64 + len(rows) * ROW_H + (44 if len(people) > max_rows else 0) + 50
         + 50 + 64 + list_h + 50
         + 50 + CLOUD_H + PAD)

    card = Image.new("RGBA", (W, H), bg_rgb + (255,))
    if bg_img:  # cover-fit photo, washed out so text stays readable
        bg = Image.open(bg_img).convert("RGBA")
        s = max(W / bg.width, H / bg.height)
        bg = bg.resize((int(bg.width * s) + 1, int(bg.height * s) + 1))
        l, t = (bg.width - W) // 2, (bg.height - H) // 2
        card = Image.alpha_composite(bg.crop((l, t, l + W, t + H)),
                                     Image.new("RGBA", (W, H), (255, 255, 255, 215)))
    d = ImageDraw.Draw(card)

    def hrule(y):
        d.line((PAD, y, W - PAD, y), fill=rule, width=2)

    def right_text(x, y, s, font, fill):
        d.text((x - d.textlength(s, font=font), y), s, font=font, fill=fill)

    # header
    y = PAD
    d.text((PAD, y - 8), title, font=F(64, True), fill=fg)
    y += 86
    span = f"  ·  {dates[0]} – {dates[-1]}" if dates else ""
    d.text((PAD, y), f"{grand:,} words  ·  {total_msgs:,} messages  ·  {len(people)} people{span}",
           font=F(28), fill=grey)
    y += 50 + 50
    hrule(y)
    y += 50

    # who talks most
    d.text((PAD, y), "Who talks most", font=F(32, True), fill=fg)
    y += 64
    top_n = words[rows[0]] if rows else 1
    name_f, sub_f, num_f = F(30), F(22), F(30)
    bar_x0, bar_x1 = PAD + 470, W - PAD - 360
    for i, p in enumerate(rows):
        c = colours.get(p, OTHER)
        d.rectangle((PAD, y + 12, PAD + 18, y + 30), fill=c)
        d.text((PAD + 34, y + 4), p if len(p) <= 24 else p[:23] + "…", font=name_f, fill=fg)
        d.text((PAD + 34, y + 44), ", ".join(w for w, _ in freq[p].most_common(4))[:44], font=sub_f, fill=grey)
        d.rectangle((bar_x0, y + 18, bar_x0 + max(3, int((bar_x1 - bar_x0) * words[p] / top_n)), y + 26), fill=c)
        right_text(W - PAD - 110, y + 4, f"{words[p]:,}", num_f, fg)
        right_text(W - PAD, y + 4, f"{words[p] / grand:.0%}", num_f, grey)
        right_text(W - PAD, y + 44, f"{msgs[p]:,} messages · {words[p] / msgs[p]:.1f} words each", sub_f, grey)
        y += ROW_H
        if i < len(rows) - 1:
            d.line((PAD + 34, y - 8, W - PAD, y - 8), fill=rule, width=1)
    if len(people) > max_rows:
        d.text((PAD + 34, y + 4), f"and {len(people) - max_rows} more", font=sub_f, fill=grey)
        y += 44
    y += 50
    hrule(y)
    y += 50

    # most used words: numbered list in columns
    d.text((PAD, y), "Most used words", font=F(32, True), fill=fg)
    y += 64
    col_w = (inner - (COLS - 1) * 60) // COLS
    wf = F(28)
    rows_per_col = -(-len(top_words) // COLS) if top_words else 0
    for k, (w, cnt) in enumerate(top_words):
        col, row = divmod(k, rows_per_col)
        x0, y0 = PAD + col * (col_w + 60), y + row * 48
        right_text(x0 + 40, y0, f"{k + 1}", wf, grey)
        d.rectangle((x0 + 58, y0 + 9, x0 + 72, y0 + 23), fill=mix(w))
        d.text((x0 + 86, y0), w, font=wf, fill=fg)
        right_text(x0 + col_w, y0, f"{cnt:,}", wf, grey)
    y += list_h + 50
    hrule(y)
    y += 50

    # word cloud
    if overall:
        wc = WordCloud(width=inner, height=CLOUD_H, font_path=args.font if have_font else None,
                       max_words=150, mode="RGBA", background_color=None, prefer_horizontal=1.0,
                       margin=6, relative_scaling=0.5,
                       color_func=lambda w, **_: "rgb(%d,%d,%d)" % mix(w)
                       ).generate_from_frequencies(overall)
        card.alpha_composite(wc.to_image().convert("RGBA"), (PAD, y))
    y += CLOUD_H + 40

    card.convert("RGB").save(out_path)
    print(f"\nCard saved to {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chats", nargs="*",
                    help="WhatsApp _chat.txt / .zip exports, or with --imessage: chat names / numbers")
    ap.add_argument("--imessage", action="store_true",
                    help="read from the Mac's Messages app; with no chats given, lists them")
    ap.add_argument("--me", default="Me", help="your name on iMessage cards")
    ap.add_argument("--list", type=int, default=40, help="how many iMessage chats to list")
    ap.add_argument("--chat-db", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--addressbook", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--outdir", default=".", help="where to write the PNG cards")
    ap.add_argument("--title", help="card title (single chat only; default: from filename / chat name)")
    ap.add_argument("--top", type=int, default=30, help="top N words to list")
    ap.add_argument("--keep-stopwords", action="store_true",
                    help="include common words (the, and, ...) in rankings/cloud")
    ap.add_argument("--bg", default="#ffffff",
                    help="background: a colour (#hex or name) or path to an image")
    ap.add_argument("--font", default="/System/Library/Fonts/Helvetica.ttc")
    ap.add_argument("--contrast", type=float, default=3,
                    help="how hard a slight lead pulls a word to that person's colour (1 = linear)")
    ap.add_argument("--people", type=int, default=8, help="max people to colour (most talkative first)")
    args = ap.parse_args()

    jobs = []  # (title, message iterable)
    if args.imessage:
        import imessage
        kw = {"me": args.me}
        if args.chat_db:
            kw["db"] = args.chat_db
        if args.addressbook:
            kw["addressbook"] = args.addressbook
        im = imessage.Messages(**kw)
        chats = im.chats()
        if not args.chats:
            print(f"{'#':>4}  {'Chat':<40}{'Messages':>10}  Last message")
            for c in chats[:args.list]:
                last = imessage._date(c["last"])
                print(f"{c['index']:>4}  {c['title'][:39]:<40}{c['n']:>10,}  {f'{last:%d %b %Y}' if last else ''}")
            print('\nRender one or more:  python3 wac.py --imessage 3 "Pizza Crew" "+44 7700 900123"')
            return
        for q in args.chats:
            c = im.find(q, chats)
            if not c:
                print(f'\nNo iMessage chat matches "{q}" — run with just --imessage to list them.')
                continue
            jobs.append((c["title"], im.messages(c)))
    else:
        if not args.chats:
            ap.error("give a WhatsApp export, or use --imessage")
        jobs = [(chat_title(p), parse(p)) for p in args.chats]

    os.makedirs(args.outdir, exist_ok=True)
    for title, messages in jobs:
        if args.title and len(jobs) == 1:
            title = args.title
        words, msgs, freq, dates = analyse(messages, args.keep_stopwords)
        if not msgs:
            print(f"\nSkipping {title}: no messages found.")
            continue
        overall = sum(freq.values(), Counter())
        print_stats(title, words, msgs, freq, overall, args.top)
        slug = re.sub(r"[^\w-]+", "_", title).strip("_") or "chat"
        render_card(title, words, msgs, freq, overall, dates, args, os.path.join(args.outdir, f"{slug}.png"))

if __name__ == "__main__":
    main()
