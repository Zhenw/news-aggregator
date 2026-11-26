#!/usr/bin/env python3
"""新闻聚合构建脚本:抓取 -> 去重 -> 标签 -> (可选)翻译 -> 头条打分 -> 生成静态 HTML
用法:
  python build.py            # 真实抓取
  python build.py --demo     # 用内置示例数据(离线预览)
  python build.py --no-resolve   # 不解析 Google 跳转链接(更快)
可选翻译环境变量: OPENAI_API_KEY, OPENAI_BASE_URL(默认 https://api.openai.com/v1), TRANSLATE_MODEL
"""
import json, os, re, sys, math, hashlib, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = timezone(timedelta(hours=CFG["timezone_offset_hours"]))
NOW = datetime.now(TZ)
UA = "Mozilla/5.0 (compatible; NewsAggregator/0.1)"
WEEK = "一二三四五六日"

# ---------- 标题清理 / 垃圾过滤 ----------
PUB_SUFFIX = re.compile(
    r"(?:^|\s+)[-–—|]\s*(?:中华人民共和国[\u4e00-\u9fff]{1,12}|[\u4e00-\u9fff]{2,12}(?:部|委|局|署|网|办|院|会|厅)|"
    r"Reuters|Bloomberg(?:\.com)?|BBC(?: News)?|全球法规网|新华网|人民网)\s*$")

def clean_title(t, source=""):
    t = re.sub(r"\s+", " ", unescape(t or "")).strip()
    if source and t.endswith(source):                     # 精确匹配 RSS 的 <source>
        t = t[: -len(source)].rstrip(" -–—|").strip()
    for _ in range(2): t = PUB_SUFFIX.sub("", t).strip()
    return t.strip(" -–—|").strip()

JUNK = re.compile(CFG.get("junk_title_regex", r"^$"))
LOWV = re.compile(CFG.get("low_value_regex", r"$^"))
def is_junk(t):
    t = (t or "").strip()
    return len(re.sub(r"\s", "", t)) < 6 or bool(JUNK.search(t))

def clean_desc(desc, title):
    d = unescape(re.sub(r"<[^>]+>", " ", desc or "")); d = re.sub(r"\s+", " ", d).strip()
    if len(d) < 30 or d.lower().startswith(title[:25].lower()): return ""   # Google News 的描述只是标题+来源,丢弃
    return d

# ---------- 抓取 ----------
def parse_dt(s):
    if not s: return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try: d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception: return None
    if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(TZ)

def strip_ns(tag): return tag.split("}", 1)[-1]

def fetch_feed(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for el in root.iter():
        if strip_ns(el.tag) not in ("item", "entry"): continue
        f = {strip_ns(c.tag): c for c in el}
        title = (f["title"].text or "").strip() if "title" in f else ""
        link = ""
        if "link" in f:
            link = (f["link"].text or "").strip() or f["link"].attrib.get("href", "")
        dt = None
        for _k in ("pubDate", "published", "updated", "date"):   # 注意:不能用 or 判断 Element(无子节点的 Element 为假值)
            if _k in f and f[_k].text:
                dt = parse_dt(f[_k].text); break
        if not title or not link or not dt: continue
        source = (f["source"].text or "").strip() if "source" in f else ""
        title = clean_title(title, source)
        if is_junk(title): continue
        desc = clean_desc(f["description"].text, title) if "description" in f else ""
        out.append({"src": feed["src"], "dept": feed.get("dept", ""), "title": title, "url": link, "dt": dt, "desc": desc})
    return out

def fetch_all():
    items = []
    for fd in CFG["feeds"]:
        try:
            got = fetch_feed(fd); items += got
            print(f"[ok]   {fd['name']}: {len(got)}")
        except Exception as e:
            print(f"[fail] {fd['name']}: {e}")
    return items

# ---------- Google News 跳转链接 -> 原文直链(带缓存,失败回退) ----------
RES_F = ROOT / "resolve_cache.json"
def _gn_id(url):
    m = re.match(r"https://news\.google\.com/(?:rss/)?articles/([^?/]+)", url)
    return m.group(1) if m else None

def _resolve_one(url):
    aid = _gn_id(url)
    if not aid: return url
    h = {"User-Agent": UA}
    page = urllib.request.urlopen(urllib.request.Request(
        f"https://news.google.com/rss/articles/{aid}?hl=en-US&gl=US&ceid=US:en", headers=h), timeout=20).read().decode("utf-8", "ignore")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page); ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts): return url
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]' % (aid, ts.group(1), sg.group(1)))
    body = ("f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))).encode()
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", body,
        {**h, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    txt = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    m = re.search(r'garturlres\\",\\"(https?:[^\\]+)', txt)
    return m.group(1) if m else url

def resolve_links(items, workers=4):
    """给每条设置 it['link'](直链或原跳转链接)。已解析的走缓存,只处理新条目。"""
    from concurrent.futures import ThreadPoolExecutor
    cache = json.loads(RES_F.read_text(encoding="utf-8")) if RES_F.exists() else {}
    todo = [it for it in items if _gn_id(it["url"]) and it["url"] not in cache]
    def work(it):
        try: cache[it["url"]] = _resolve_one(it["url"])
        except Exception as e: print("[resolve fail]", it["title"][:40], str(e)[:60])
    if todo:
        print(f"resolving {len(todo)} links ...")
        with ThreadPoolExecutor(workers) as ex: list(ex.map(work, todo))
        RES_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    for it in items: it["link"] = cache.get(it["url"], it["url"])
    return items

# ---------- 去重 ----------
def norm(t): return re.sub(r"[\W_]+", "", t.lower())

def dedupe(items):
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["dt"], reverse=True):
        k = norm(it["title"])[:60]
        u = it["url"].split("?")[0]
        if k in seen or u in seen: continue
        seen.add(k); seen.add(u); out.append(it)
    return out

# ---------- 翻译(可选,带缓存) ----------
CACHE_F = ROOT / "translate_cache.json"
def has_cjk(s): return bool(re.search(r"[\u4e00-\u9fff]", s))

def _llm_translate(titles, to_lang):
    head = ("把下面的英文新闻标题逐条翻译成简洁的中文。" if to_lang == "zh"
            else "Translate each of the following Chinese news headlines into a concise, natural English headline. ")
    out = _chat(head + "只返回JSON字符串数组,顺序和条数一致。\n" + json.dumps(titles, ensure_ascii=False))
    return json.loads(re.search(r"\[.*\]", out, re.S).group(0))

def translate(items):
    """双向标题翻译:英文->中文(zh)、中文->英文(en)。无 Key 时只保留原文。"""
    key = os.environ.get("OPENAI_API_KEY")
    raw = json.loads(CACHE_F.read_text(encoding="utf-8")) if CACHE_F.exists() else {}
    cache = raw if ("zh" in raw or "en" in raw) else {"zh": raw, "en": {}}      # 兼容旧版扁平缓存
    cache.setdefault("zh", {}); cache.setdefault("en", {})
    if key:
        for lang, need in (("zh", lambda t: not has_cjk(t)), ("en", has_cjk)):
            todo = [it["title"] for it in items if need(it["title"]) and it["title"] not in cache[lang]]
            for i in range(0, len(todo), 30):
                batch = todo[i:i + 30]
                try:
                    for b, z in zip(batch, _llm_translate(batch, lang)): cache[lang][b] = z
                except Exception as e:
                    print("[translate fail]", lang, str(e)[:60]); break
        CACHE_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    for it in items:
        t = it["title"]
        it["zh"] = t if has_cjk(t) else cache["zh"].get(t, "")
        it["en"] = t if not has_cjk(t) else cache["en"].get(t, "")
    return items

# ---------- 标签 ----------
def tag_item(it):
    text = " " + (it["title"] + " " + it.get("zh", "")).lower() + " "
    tags = [k for k, v in CFG["tags"].items() if any(w in text for w in v["kw"])]
    return tags or [CFG["other_tag"]["name"]]

# ---------- 头条:聚类 + 加权打分 ----------
def tokens(t):
    t = t.lower()
    words = set(re.findall(r"[a-z0-9]{3,}", t))
    cjk = re.findall(r"[\u4e00-\u9fff]+", t)
    for seg in cjk: words |= {seg[i:i+2] for i in range(len(seg) - 1)}
    return words

def jacc(a, b): return len(a & b) / len(a | b) if a and b else 0

def build_headline(items):
    win = timedelta(hours=CFG["headline_window_hours"])
    pool = [it for it in items if NOW - it["dt"] <= win]
    clusters = []
    for it in pool:                         # items 已按时间倒序
        tk = tokens(it["title"] + " " + it.get("zh", ""))
        for c in clusters:
            if jacc(tk, c["tk"]) >= CFG["similar_threshold"]:
                c["items"].append(it); break
        else:
            clusters.append({"items": [it], "tk": tk})
    ranked = []
    for c in clusters:
        its = c["items"]
        srcs = {i["src"] for i in its}
        w = sum(CFG["sources"][i["src"]]["weight"] for i in its)
        newest = max(i["dt"] for i in its)
        age_h = (NOW - newest).total_seconds() / 3600
        decay = 0.5 ** (age_h / 6)           # 半衰期 6 小时
        score = 100 * (w + 0.8 * (len(srcs) - 1)) * (0.4 + 0.6 * decay)
        rep = max(its, key=lambda i: (CFG["sources"][i["src"]]["weight"], i["dt"]))
        if LOWV.search(rep["title"] + " " + rep.get("zh", "")): score *= CFG.get("low_value_factor", 0.5)   # 例行记者会/预报等降权
        ranked.append((round(score), rep, len(its), len(srcs), age_h))
    ranked.sort(key=lambda x: -x[0])
    caps, used, picked = CFG.get("headline_max_per_source", {}), {}, []
    for r in ranked:                                   # 每个来源最多占 N 席,避免被某一类刷屏
        src = r[1]["src"]; cap = caps.get(src, caps.get("default", 99))
        if used.get(src, 0) >= cap: continue
        used[src] = used.get(src, 0) + 1; picked.append(r)
        if len(picked) >= CFG["headline_top_n"]: break
    return [{"gid": r[1]["gid"], "score": r[0], "n": r[2], "nsrc": r[3], "age": round(r[4], 2)} for r in picked]

# ---------- TOP10 大意(抓原文 → 有 API Key 则 LLM 提炼中文大意,否则取摘要) ----------
GIST_F = ROOT / "gist_cache.json"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _article_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"})
    html = urllib.request.urlopen(req, timeout=15).read(600000).decode("utf-8", "ignore")
    def meta(name):
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']' + name + r'["\'][^>]+content=["\']([^"\']+)', html, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + name + r'["\']', html, re.I))
        return unescape(m.group(1)).strip() if m else ""
    desc = meta("og:description") or meta("description")
    paras = [unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)]
    body = " ".join(p for p in paras if len(p) > 40)[:1800]
    return desc, body

def _chat(prompt):
    """调用 OpenAI 兼容接口(OpenAI / Gemini 兼容端点均可)。空环境变量视为未设置;遇到 429/5xx 自动退避重试。"""
    import time
    key = os.environ["OPENAI_API_KEY"]
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("TRANSLATE_MODEL") or "gpt-4o-mini"
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}).encode()
    last = None
    for attempt in range(4):
        req = urllib.request.Request(base + "/chat/completions", body, {"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            out = json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]["content"].strip()
            time.sleep(1.5)                                  # 免费层有每分钟请求数限制,轻微限速
            return out
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(8 * (attempt + 1)); continue
            raise
    raise last

def _cut(t, n):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= n: return t
    seg = t[:n]; k = max(seg.rfind("。"), seg.rfind(". "), seg.rfind("!"), seg.rfind("?"))
    return seg[:k + 1] if k > n * 0.4 else seg.rstrip() + "…"

def _good(t, n=None):
    """正文是否足够长:中文 >=40 字,英文 >=60 字符。网站标语、栏目名之类的短文本不算。"""
    t = (t or "").strip()
    return len(t) >= (n or (40 if has_cjk(t) else 60))

def fetch_desc_feeds():
    """官方 RSS 自带正文摘要(例如 Bloomberg 各栏目)。只用来补充摘要,不新增条目。"""
    out = {}
    for url in CFG.get("desc_feeds", []):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
            n = 0
            for el in root.iter("item"):
                f = {strip_ns(c.tag): c for c in el}
                t = clean_title((f["title"].text or "") if "title" in f else "")
                dsc = clean_desc(f["description"].text if "description" in f and f["description"].text else "", t)
                if t and dsc: out[norm(t)[:80]] = dsc; n += 1
            print(f"[ok]   desc feed {url.split('/')[-3] if url.count('/')>3 else url}: {n}")
        except Exception as e:
            print("[desc feed fail]", url, str(e)[:60])
    return out

def attach_descs(items):
    """把官方摘要按标题匹配到条目上;摘要缓存在 gist_cache.json(随 Actions 缓存保留),这样已经滑出 RSS 的旧条目也还能用。"""
    cache = json.loads(GIST_F.read_text(encoding="utf-8")) if GIST_F.exists() else {}
    for k, v in fetch_desc_feeds().items():
        cache.pop("desc:" + k, None); cache["desc:" + k] = v
    keys = [k for k in cache if k.startswith("desc:")]
    for k in keys[:-800]: del cache[k]
    GIST_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    hit = 0
    for it in items:
        if not it.get("desc"):
            v = cache.get("desc:" + norm(it["title"])[:80])
            if v: it["desc"] = v; hit += 1
    print(f"attached official summaries to {hit} items")

def make_gists(headline, items, demo=False):
    """TOP10 双语大意:gist(中文) + gist_en(英文)。没有足够原文时不让模型编造。"""
    by = {i["gid"]: i for i in items}
    cache = json.loads(GIST_F.read_text(encoding="utf-8")) if GIST_F.exists() else {}
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    for h in headline:
        it = by.get(h["gid"])
        if not it: continue
        if demo:
            h["gist"] = "这是演示数据的大意:" + (it.get("zh") or it["title"]) + "。真实运行时这里会是根据原文提炼的两三句话摘要。"
            h["gist_en"] = "Demo summary: " + (it.get("en") or it["title"]) + ". In a real run this is a two-to-three sentence summary of the article."
            continue
        ck = h["gid"] + ":v3"
        if ck in cache: h.update(cache[ck]); continue
        g = {"gist": "", "gist_en": ""}
        desc = body = ""
        try: desc, body = _article_text(it.get("link", it["url"]))
        except Exception as e: print("[gist fetch fail]", (it.get("zh") or it["title"])[:30], str(e)[:50])
        text = (desc + " " + body).strip()
        if not _good(text): text = it.get("desc", "")      # 原文抓不到/太短时,用官方 RSS 摘要
        if has_key and _good(text):
            try:
                out = _chat("根据下面这条新闻的标题和正文片段,分别写中文和英文各2到3句话的大意(中文80到140字;英文40到70词),适合语音播报。"
                            "不要出现链接和特殊符号,不要编造原文没有的信息;信息不足时宁可写短一些,只概括已有信息。只输出JSON对象:{\"zh\":\"...\",\"en\":\"...\"}。\n"
                            "标题:" + it["title"] + "\n正文片段:" + text[:1500])
                j = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
                g = {"gist": (j.get("zh") or "").strip(), "gist_en": (j.get("en") or "").strip()}
            except Exception as e: print("[gist llm fail]", str(e)[:60])
            if re.search(r"无法|没有提供|不足以|抱歉|I'm sorry|I cannot|unable to (?:summar|provide)", g["gist"] + " " + g["gist_en"], re.I):
                g = {"gist": "", "gist_en": ""}
        if not (g["gist"] or g["gist_en"]) and _good(text):        # 没有 Key:用原文摘要,语言不翻译
            if has_cjk(text): g["gist"] = _cut(desc or text, 140)
            else: g["gist_en"] = _cut(desc or text, 260)
        h.update(g)
        if g["gist"] or g["gist_en"]: cache[ck] = g
    GIST_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return headline

# ---------- 输出 ----------
DEMO_EN = {"财政部等五部门印发《常态化帮扶资金绩效评价办法》": "Five ministries issue measures on performance evaluation of regular assistance funds",
           "中国—葡萄牙经贸混委会第11次会议在里斯本召开": "11th China-Portugal Joint Economic and Trade Commission meeting held in Lisbon",
           "外交部发言人主持例行记者会": "Foreign Ministry spokesperson holds regular press briefing"}

def demo_items():
    base = NOW.replace(second=0, microsecond=0)
    rows = [("reuters","","Fed officials signal patience on rate cuts as inflation stays sticky",5,"美联储官员表示通胀仍具粘性,降息需要耐心"),
     ("bloomberg","","Fed's Powell says policy well positioned, rate cut path uncertain",20,"鲍威尔称政策处于合适位置,降息路径不确定"),
     ("reuters","","Nvidia shares jump as AI chip demand outlook lifts tech stocks",35,"AI芯片需求前景提振科技股,英伟达股价大涨"),
     ("bloomberg","","Oil climbs after drone attack hits Russian refinery",50,"俄罗斯炼油厂遭无人机袭击,油价上涨"),
     ("gov","财政部","财政部等五部门印发《常态化帮扶资金绩效评价办法》",70,""),
     ("gov","商务部","中国—葡萄牙经贸混委会第11次会议在里斯本召开",90,""),
     ("reuters","","China soybean oil exports cool as India turns to palm oil",130,"印度转向棕榈油,中国豆油出口热潮降温"),
     ("bloomberg","","IMF says South Africa needs strong fiscal rule to boost reforms",200,"IMF称南非需要强有力的财政规则推动改革"),
     ("gov","外交部","外交部发言人主持例行记者会",260,""),
     ("other","","UK house prices rise as mortgage rates ease",420,"抵押贷款利率下降,英国房价上涨"),
     ("reuters","","Amazon to launch Amazon Autos service in UK in early 2027",600,"亚马逊将于2027年初在英国推出Amazon Autos")]
    out = []
    for s, d, t, m, zh in rows:
        out.append({"src": s, "dept": d, "title": t, "zh": zh, "url": "https://example.com/" + hashlib.md5(t.encode()).hexdigest()[:8], "dt": base - timedelta(minutes=m)})
    return out

def to_record(it, i):
    return {"id": i, "gid": it["gid"], "src": it["src"], "dept": it["dept"],
            "t": it["title"], "zh": it.get("zh") or (it["title"] if has_cjk(it["title"]) else ""), "en": it.get("en") or (it["title"] if not has_cjk(it["title"]) else ""), "url": it.get("link", it["url"]), "ts": it["dt"].strftime("%Y-%m-%d %H:%M"),
            "d": it["dt"].strftime("%Y-%m-%d"), "wk": "周" + WEEK[it["dt"].weekday()], "tags": it["tags"]}

def main():
    demo = "--demo" in sys.argv
    items = demo_items() if demo else fetch_all()
    if demo:
        for _i in items: _i["en"] = DEMO_EN.get(_i["title"], "" if has_cjk(_i["title"]) else _i["title"])
    items = dedupe(items)
    cutoff = NOW - timedelta(hours=CFG["keep_hours"])
    items = [i for i in items if i["dt"] >= cutoff]
    for i in items: i["gid"] = hashlib.sha1(i["url"].encode()).hexdigest()[:12]
    if not demo:
        items = translate(items)
        if "--no-resolve" not in sys.argv: items = resolve_links(items)
    for i in items: i["tags"] = tag_item(i)
    if not demo: attach_descs(items)
    headline = make_gists(build_headline(items), items, demo)
    data = [to_record(it, n) for n, it in enumerate(items)]
    tags = [{"k": k, "c": v["color"]} for k, v in CFG["tags"].items()] + [{"k": CFG["other_tag"]["name"], "c": CFG["other_tag"]["color"]}]
    boot = {"src": {k: {"label": v["label"], "color": v["color"]} for k, v in CFG["sources"].items()},
            "tags": tags, "data": data, "headline": headline, "meta": {"built": NOW.strftime("%Y-%m-%d %H:%M"), "n": len(data)}}
    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("/*__BOOT__*/null", json.dumps(boot, ensure_ascii=False))
    out = ROOT / "dist"; out.mkdir(exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(boot, ensure_ascii=False), encoding="utf-8")
    print(f"built {len(data)} items, {len(headline)} headlines -> {out/'index.html'}")

if __name__ == "__main__":
    main()
