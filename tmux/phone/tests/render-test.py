#!/usr/bin/env python3
"""Unit tests for phone-hub's pane renderer (colours, rules, prompt area, reflow, exact view, hostile input)."""
import importlib.machinery, importlib.util, os, re, sys, time

sys.dont_write_bytecode = True
HUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "phone-hub")
os.environ["PHONE_HUB_CONFIG_DIR"] = "/nonexistent-phtest"; os.environ["PHONE_HUB_STATE_DIR"] = "/nonexistent-phtest2"
loader = importlib.machinery.SourceFileLoader("phone_hub", HUB)
spec = importlib.util.spec_from_loader("phone_hub", loader); H = importlib.util.module_from_spec(spec); loader.exec_module(H)

passed = failed = 0
def ck(name, ok, detail=""):
    global passed, failed
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   {detail}"), flush=True)
    passed += bool(ok); failed += (not ok)

def R(raw, width=131, mode="smart", n=500, cap=40000):
    return H.render_pane(raw, width, mode, n, cap)

ESC = "\x1b"
def no_markup_leak(html_text, hostile):
    """Tags that the input tried to inject must never appear unescaped."""
    return not any(re.search(h, html_text) for h in hostile)

print("== colours ==");
h = R(f"{ESC}[1;38;5;231mBold white{ESC}[0m plain text")
ck("256-colour index 231 becomes #ffffff, bold kept", "color:#ffffff" in h and "font-weight:700" in h, h[:200])
ck("reset ends the style (plain text after is outside the span)", "</span> plain text" in h, h[:200])
h = R(f"{ESC}[38;2;145;152;161mgrey{ESC}[39m")
ck("truecolor 145,152,161 becomes #9198a1", "color:#9198a1" in h, h[:200])
h = R(f"{ESC}[31mred{ESC}[0m {ESC}[92mbright green{ESC}[0m")
ck("16-colour red and bright green", "color:#cd3131" in h and "color:#23d18b" in h, h[:200])
h = R(f"{ESC}[7mrev{ESC}[0m")
ck("reverse swaps the default colours", "color:#0d1117" in h and "background:#d6deeb" in h, h[:200])
h = R(f"{ESC}[2mdim{ESC}[22m {ESC}[3mit{ESC}[23m {ESC}[4mul{ESC}[24m {ESC}[9mst{ESC}[29m")
ck("dim, italic, underline, strike", all(x in h for x in ("opacity:.72", "font-style:italic", "text-decoration:underline", "text-decoration:line-through")), h[:300])
ck("colour comes only from validated numbers (no stray style text)", all(re.fullmatch(r"(color|background|font-weight|opacity|font-style|text-decoration):[#a-z0-9.\- ]+", p) for st in re.findall(r'style="([^"]*)"', h) for p in st.split(";") if p))

print("== hostile input ==")
bad = [r"<script", r"<img", r"<[^>]*onerror", r"<[^>]*javascript:"]
h = R(f'{ESC}]0;<script>alert(1)</script>\x07 title injected {ESC}[31;"><img src=x onerror=alert(1)>m text <script>alert(2)</script>')
ck("OSC title, broken SGR and raw tags cannot inject markup", no_markup_leak(h, bad), h[:400])
ck("the text itself is still shown, escaped", "&lt;script&gt;alert(2)&lt;/script&gt;" in h and "title injected" in h)
h = R(f"{ESC}[38;5;999mx{ESC}[38;2;999;-5;zzz;1my{ESC}[" + ";".join(["1"] * 10000) + f"mz{ESC}[48;5;mw")
ck("out-of-range, malformed and huge SGR parameters are harmless", "x" in h and "y" in h and "z" in h and "w" in h and no_markup_leak(h, bad))
ck("no escape or control characters reach the page", not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", h))
h = R("‮evil⁦ text⁩ ﻿ zero width")
ck("bidi/format control characters are removed", not re.search(r"[‪-‮⁦-⁩﻿  ]", h))
h = R(f"    indented <script>alert(1)</script> code line\n     {ESC}[31m<b>x</b>{ESC}[0m")
ck("escaping also holds inside scrolling blocks", no_markup_leak(h, bad + [r"<b>"]) and "&lt;b&gt;x&lt;/b&gt;" in h, h)

print("== rules, prompt area, status (Claude style) ==")
rule = f"{ESC}[38;5;244m" + "─" * 131 + f"{ESC}[39m"
claude = "\n".join([
    f"{ESC}[38;5;231m⏺{ESC}[39m First part of the answer",
    "",
    rule,
    f"{ESC}[38;5;246m❯{ESC}[39m my typed text",
    rule,
    "  ~/dir ❯ model [ctx: 99%]",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
])
h = R(claude)
ck("a 131-character rule becomes one <hr>, not wrapped text", h.count("<hr>") >= 2 and "─────" not in h, h[:300])
ck("the area between the last two rules is the prompt area", '<div class="prompt">' in h and "my typed text" in h.split('class="prompt"')[1].split("</div></div>")[0])
ck("lines after the last rule are the status block", '<div class="status">' in h and "bypass permissions" in h.split('class="status"')[1])
ck("the body before the prompt is not inside it", "First part of the answer" not in h.split('class="prompt"')[1] and "First part" in h.split('class="prompt"')[0])
copilot = "\n".join(["  normal answer text here",
                     "╻" + "▄" * 129 + "╸", "┃ typed in copilot", "╹" + "▀" * 129 + "╸", " ← keys · help"])
h = R(copilot)
ck("Copilot's block-character bars are rules too", h.count("<hr>") >= 2 and '<div class="prompt">' in h and "typed in copilot" in h, h[:300])
ck("and its status line is below the prompt", "keys" in h.split('class="status"')[-1])
h = R("only prose\n" + rule + "\nstatus after one rule")
ck("a single rule near the bottom still separates a status block", '<div class="status">' in h and "status after one rule" in h.split('class="status"')[1])
h = R("before\n" + rule + "\nmiddle\nlots\nof\nlines\n" + "\n".join(f"line {i}" for i in range(30)))
ck("a rule far from the bottom is just a divider, not a prompt box", '<div class="prompt">' not in h and h.count("<hr>") == 1)

print("== reflow of hard-wrapped prose ==")
W = 131
words = ("The quick brown fox jumps over the lazy dog while the agent keeps explaining something rather long and detailed "
         "about the design so that this sentence must wrap over several lines in a wide terminal ").split()
def wrap(text, width, indent):
    lines, cur = [], ""
    for w in text.split():
        if len(cur) + len(w) + 1 + indent > width and cur:
            lines.append(" " * indent + cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(" " * indent + cur)
    return lines
para = wrap(" ".join(words * 3) + " The end of paragraph.", 124, 2)
ck("test paragraph spans several full-width lines", len(para) >= 4 and sum(len(l) >= 0.8 * W for l in para[:-1]) == len(para) - 1, [len(l) for l in para])
raw = "⏺" + para[0][1:] + "\n" + "\n".join(para[1:])
h = R(raw, W)
divs = re.findall(r'<div class="l[^"]*">', h)
ck("hard-wrapped lines are joined into ONE paragraph", len(divs) == 1, f"{len(divs)} divs")
ck("joined with single spaces, no doubled spaces or lost words", "sentence must wrap over several lines" in re.sub(r"<[^>]+>", "", h) and "  " not in re.sub(r"<[^>]+>", "", h).replace("⏺ ", ""))
raw = "Short line one\nShort line two\nDone."
h = R(raw, W); ck("short lines are NOT merged", len(re.findall(r'<div class="l', h)) == 3, h)
raw = "- first item\n- second item\n1. numbered\n2. numbered two"
h = R(raw, W); ck("list items stay separate and get hanging indent", len(re.findall(r'class="l li"', h)) == 4, h)
raw = "x" * 120 + "\n- but this bullet starts a new thing"
h = R(raw, W); ck("a full-width line is not glued to a following bullet", len(re.findall(r'<div class="l', h)) == 2, h)
raw = "y" * 125 + "\n" + " " * 12 + "deeply indented, not a continuation"
h = R(raw, W); ck("a much deeper indent is not treated as a continuation", "blk" in h or len(re.findall(r'<div class="l', h)) == 2, h)
h = R("> a quoted comment line\n## 1. (line 9) Feedback on: \"x\"\nnormal");
ck("quote and heading lines get their own look", 'class="l q"' in h and 'class="l h"' in h, h)
h = R("para one\n\n\n\npara two"); ck("blank lines become one small gap", h.count('class="gap"') == 1, h)

print("== list items whose continuation lines are indented under the text (a bug seen on the phone) ==")
item_text = ("Wrapping: the agents break their text at the full pane width, which is why your phone showed it in three or four line scraps. "
             "The page now joins those pieces back into proper paragraphs that wrap to your screen.")
first = ("  1. " + item_text)[:124].rsplit(" ", 1)[0]
rest = ("  1. " + item_text)[len(first):].strip()
numbered = "\n".join([first, "     " + rest, "  2. Second item, short.", "  3. " + " ".join(["third"] * 22), "     continuation of the third item."])
h = R(numbered, W)
ck("an indented continuation line is NOT sent to a scrolling block", "<pre" not in h, h)
ck("it is joined to its list item", "scraps. The page now joins those pieces back into proper paragraphs" in re.sub(r"<[^>]+>", "", h), re.sub(r"<[^>]+>", "", h)[:300])
ck("each numbered item is its own list line", len(re.findall(r'class="l li"', h)) == 3, h)
bul = "\n".join(["  - " + " ".join(["bullet"] * 20), "    continuation under a bullet that is deeper than the marker", "  - next bullet"])
h = R(bul, W); ck("bullet continuation lines too", "<pre" not in h and len(re.findall(r'class="l li"', h)) == 2, h)
tool2 = "\n".join(["  1. a numbered item whose line is long enough to be considered a full-width line of text " + "x" * 40, "regular prose", "⏺ Bash(cmd)", "  ⎿  $ run", "     real tool output stays in a block"])
h = R(tool2, W); ck("real tool output after a ⎿ line is still a block", h.count("<pre") == 1 and "real tool output" in h.split("<pre")[1], h)
h = R("  1. short item\n     deeper line that is not a continuation of a full line"); ck("a deep line after a SHORT item is not glued on (stays a block)", "<pre" in h)

print("== your own highlighted messages, indentation of blocks, non-breaking spaces ==")
msg = f"{ESC}[48;5;237m❯ please explain how the page now joins those pieces back into paragraphs for a narrow screen, thanks a lot{ESC}[49m"
h = R(msg, W); ck("a highlighted ❯ message is prose, keeps its coloured band, and wraps", "<pre" not in h and 'class="l bgl"' in h and "background:#3a3a3a" in h, h)
hint = " " * 30 + "? for shortcuts · context 99%\n" + " " * 34 + "indented deeper"
h = R(hint, W); body = re.sub(r"<[^>]+>", "", h.split("<pre")[1]); 
ck("a scrolling block drops the indentation all its lines share", body.split(">", 1)[-1].startswith("?") or body.lstrip("\n").startswith("? for") or "\n    indented deeper" in body, repr(body[:90]))
ck("but keeps the RELATIVE indentation inside it", "\n    indented deeper" in body, repr(body))
nb = "Run the \xa0command-name\xa0 and then \xa0other-thing\xa0 to see how it behaves in a normal sentence of prose."
h = R(nb, W); ck("non-breaking spaces around inline code do not make a sentence a 'column' block", "<pre" not in h, h)
cols = "Tab complete   Esc cancel   Enter accept   Up history"
h = R(cols, W); ck("a real aligned hint line (several wide gaps) is still a block", "<pre" in h, h)

code_line = f"Then run {ESC}[48;2;40;44;52mgit rebase --onto{ESC}[49m and check the {ESC}[48;2;40;44;52mresult{ESC}[49m."
h = R(code_line, W); ck("a sentence with inline `code` backgrounds stays prose", "<pre" not in h, h)
ck("and the inline code keeps its own background", "background:#282c34" in h, h)
print("== layout-sensitive text stays in sideways-scrolling blocks ==")
tool = "\n".join(["⏺ Bash(run something)", "  ⎿  $ cmd one", "     indented tool output line one", "     indented tool output line two", "regular again"])
h = R(tool)
ck("indented tool output is one <pre> block", h.count('<pre class="blk">') == 1 and "tool output line one" in h.split("<pre")[1] and "tool output line two" in h.split("<pre")[1], h)
ck("prose after it is back to wrapping text", 'class="l">regular again' in h or "regular again</div>" in h, h)
table = "┌──────┬──────┐\n│ a    │ b    │\n└──────┴──────┘"
h = R(table); ck("box-drawing tables stay exactly as drawn", h.count('<pre class="blk">') == 1 and "│ a    │ b" in h, h)
diff = f"{ESC}[48;2;30;60;30m  12 + added line{ESC}[0m\n{ESC}[48;2;70;30;30m  13 - removed line{ESC}[0m"
h = R(diff); ck("diff lines with background colour are a block, colours kept", '<pre class="blk">' in h and "background:#1e3c1e" in h and "background:#461e1e" in h, h)
ck("added and removed lines keep DIFFERENT colours", h.count("background:#1e3c1e") == 1 and h.count("background:#461e1e") == 1)
h = R("name    size    date\nfoo     12      today\nbar     34      later"); ck("aligned columns stay aligned", "<pre" in h, h)
h = R("+ plain added\n@@ -1,3 +1,4 @@\n12 + numbered added"); ck("plain-text diff markers are blocks too", h.count("<pre") >= 1 and "@@ -1,3" in h.split("<pre")[1], h)

print("== exact view, limits, edge cases ==")
h = R(claude, mode="exact")
ck("exact view is a single scrolling block with no guessing", h.count('<pre class="blk">') == 1 and "<hr>" not in h and 'class="l' not in h and "────" in h, h[:200])
big = "\n".join(f"line {i:04d} " + "y" * 100 for i in range(1000))
h = R(big, n=60); ck("only the last N lines are shown", "line 0999" in h and "line 0940" in h and "line 0939" not in h and "line 0001" not in h)
h = R(big, n=500, cap=5000); txt = re.sub(r"<[^>]+>", "", h); ck("the character cap keeps the END", "line 0999" in txt and len(txt) <= 5300 and "line 0900" not in txt, len(txt))
ck("empty capture renders nothing", R("") == "" and R("\n\n   \n") == "")
t0 = time.time(); R("\n".join(["⏺ " + "word " * 25] * 250) + "\n" + rule); el = time.time() - t0
ck(f"500 lines render quickly ({el:.2f}s)", el < 1.0)
h = R("a\tb\tc\n"); ck("tabs become spaces", "\t" not in h)
lines_html = R("\n".join(["plain"] * 5)); ck("trailing whitespace padding never creates horizontal scroll", "  </div>" not in R("text   \nmore    "))

print(f"\nPASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
