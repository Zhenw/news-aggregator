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

FEED_F = ROOT / "feed_cache.json"

def _fetch_retry(fd, tries=3):
    """抓取单个源:失败自动重试(间隔 6s、18s),Google News 偶发 503/429 时通常能恢复。"""
    import time
    last = None
    for k in range(tries):
        try: return fetch_feed(fd)
        except Exception as e:
            last = e
            if k < tries - 1: time.sleep(6 * (3 ** k))
    raise last

def fetch_all():
    """抓全部源。成功的源写入 feed_cache.json;最终失败的源回退用上一次成功的结果(随 Actions 缓存保留),
    避免某个源临时 503 就把整类新闻(如路透)从页面上抹掉。"""
    try: cache = json.loads(FEED_F.read_text(encoding="utf-8")) if FEED_F.exists() else {}
    except Exception: cache = {}
    items, failed = [], []
    for fd in CFG["feeds"]:
        try:
            got = _fetch_retry(fd)
            if not got and cache.get(fd["name"]): raise RuntimeError("返回 0 条(疑似异常空结果)")
            items += got
            cache[fd["name"]] = [dict(g, dt=g["dt"].isoformat()) for g in got]
            print(f"[ok]   {fd['name']}: {len(got)}")
        except Exception as e:
            old = cache.get(fd["name"]) or []
            if old:
                try:
                    back = [dict(o, dt=datetime.fromisoformat(o["dt"])) for o in old]
                    items += back
                    print(f"[stale] {fd['name']}: {e} -> 回退使用上次成功结果 {len(back)} 条")
                except Exception: print(f"[fail] {fd['name']}: {e}(缓存不可用)")
            else:
                print(f"[fail] {fd['name']}: {e}(没有可回退的缓存)")
            failed.append(fd["name"])
    try: FEED_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e: print("[feed cache save fail]", str(e)[:60])
    if failed: print(f"[warn] {len(failed)} 个源最终失败: {', '.join(failed)}")
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
    head = ("把下面编号的英文新闻标题逐条翻译成简洁的中文。" if to_lang == "zh"
            else "Translate each numbered Chinese news headline into a concise, natural English headline. ")
    if to_lang == "zh": head += "术语对照:oil/crude=原油(油价=oil prices),iron ore=铁矿石,gold=黄金,copper=铜,natural gas=天然气,bitcoin=比特币,lithium=锂,soybeans=大豆。不要把一种商品译成另一种。"
    numbered = {str(i + 1): t for i, t in enumerate(titles)}
    out = _chat(head + '只返回一个JSON对象:键是原编号,值是译文,例如 {"1":"...","2":"..."}。不要合并、遗漏或调换任何一条。\n'
                + json.dumps(numbered, ensure_ascii=False))
    obj = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    return [str(obj.get(str(i + 1)) or "").strip() for i in range(len(titles))]

# 大宗商品/资产术语:原文有的词,译文必须有对应词;原文没有的词,译文不许凭空出现(挡住"oil→铁矿石"这类错译)
TERM_PAIRS = [(r"iron ore", r"铁矿"), (r"bitcoin|\bbtc\b", r"比特币"), (r"\bcopper\b", r"铜"), (r"\bgold\b", r"黄金|金价"),
              (r"natural gas|\blng\b", r"天然气|液化气"), (r"lithium", r"锂"), (r"soybeans?", r"大豆|豆"), (r"crude|brent|\bwti\b|\boil\b", r"油"),
              (r"\bsilver\b", r"白银|银价"), (r"\bcoal\b", r"煤"), (r"\bwheat\b", r"小麦"), (r"\bcocoa\b", r"可可")]
def _terms_ok(src, dst):
    en, zh = (dst, src) if has_cjk(src) else (src, dst)
    if not (has_cjk(zh) and not has_cjk(en)): return True
    for e, z in TERM_PAIRS:
        en_has, zh_has = bool(re.search(e, en, re.I)), bool(re.search(z, zh))
        if en_has == zh_has: continue
        if not en_has and e[:5] in ("crude", "soybe"): continue                  # 中文的"油""豆"太泛(汽油、咖啡豆),只在英文有这个词时才要求中文对应
        return False
    return True

def _consistent(src, dst):
    """译成中文后,译文里出现的英文专名(>=3 字符)必须在原文里出现过,用来挡住错位、张冠李戴。"""
    if not _terms_ok(src, dst): return False
    if not has_cjk(dst): return True
    s = src.lower()
    return all(t.lower() in s for t in re.findall(r"[A-Za-z][A-Za-z0-9\-\.]{2,}", dst))

def translate(items):
    """双向标题翻译:英文->中文(zh)、中文->英文(en)。按编号对应,漏掉/对不上的整条丢弃(下次构建重试)。无 Key 时只保留原文。"""
    key = os.environ.get("OPENAI_API_KEY")
    raw = json.loads(CACHE_F.read_text(encoding="utf-8")) if CACHE_F.exists() else {}
    cache = raw if raw.get("v") == 2 else {"v": 2, "zh": {}, "en": {}}      # 旧版缓存可能含错位译文,整体作废
    cache.setdefault("zh", {}); cache.setdefault("en", {})
    for lg in ("zh", "en"):                                               # 旧缓存里术语错译的条目清掉,下次构建会重译
        bad_k = [k for k, v in cache[lg].items() if not _terms_ok(k, v)]
        for k in bad_k: del cache[lg][k]
        if bad_k: print(f"[translate] purged {len(bad_k)} cached {lg} entries with term mismatch")
    if key:
        for lang, need in (("zh", lambda t: not has_cjk(t)), ("en", has_cjk)):
            todo = list(dict.fromkeys(it["title"] for it in items if need(it["title"]) and it["title"] not in cache[lang]))
            bad = 0
            for i in range(0, len(todo), 10):
                batch = todo[i:i + 10]
                try:
                    for b, z in zip(batch, _llm_translate(batch, lang)):
                        if z and _consistent(b, z): cache[lang][b] = z
                        else: bad += 1
                except Exception as e:
                    print("[translate fail]", lang, str(e)[:60]); break
            if bad: print(f"[translate] {lang}: dropped {bad} unmatched/empty results")
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

def same_cluster(a, b):
    if jacc(a, b) >= CFG["similar_threshold"]: return True
    shared = {w for w in a & b if w.isascii()}   # 英文关键词至少 3 个相同,且占较短标题的 25% 以上
    return len(shared) >= CFG.get("merge_min_shared", 3) and len(a & b) / max(1, min(len(a), len(b))) >= CFG.get("merge_min_overlap", 0.25)

def build_headline(items):
    win = timedelta(hours=CFG["headline_window_hours"])
    pool = [it for it in items if NOW - it["dt"] <= win]
    clusters = []
    for it in pool:                         # items 已按时间倒序
        tk = tokens(it["title"] + " " + it.get("zh", ""))
        for c in clusters:
            if same_cluster(tk, c["tk"]):
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
    caps, used, picked, tused = CFG.get("headline_max_per_source", {}), {}, [], {}
    for r in ranked:                                   # 每个来源最多占 N 席,避免被某一类刷屏
        src = r[1]["src"]; cap = caps.get(src, caps.get("default", 99))
        if used.get(src, 0) >= cap: continue
        txt = r[1]["title"] + " " + r[1].get("zh", "")
        hit = [t["name"] for t in CFG.get("headline_topic_caps", []) if re.search(t["regex"], txt, re.I) and tused.get(t["name"], 0) >= t["max"]]
        if hit: continue                               # 同一主题(如关税)最多占 N 席,把位置让给别的新闻
        for t in CFG.get("headline_topic_caps", []):
            if re.search(t["regex"], txt, re.I): tused[t["name"]] = tused.get(t["name"], 0) + 1
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
    if not _good(desc): desc = ""                       # 网站标语、栏目名之类的短文本不是摘要
    paras = [unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)]
    body = " ".join(p for p in paras if len(p) > 40)[:1800]
    return desc, body

def _chat(prompt, model=None, extra=None):
    """调用 OpenAI 兼容接口(OpenAI / Gemini 兼容端点均可)。空环境变量视为未设置;遇到 429/5xx 自动退避重试。"""
    import time
    key = os.environ["OPENAI_API_KEY"]
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("TRANSLATE_MODEL") or "gpt-4o-mini"
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, **(extra or {})}).encode()
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
                if t and dsc: out[norm(t)[:80]] = {"t": t, "d": dsc}; n += 1
            print(f"[ok]   desc feed {url.split('/')[-3] if url.count('/')>3 else url}: {n}")
        except Exception as e:
            print("[desc feed fail]", url, str(e)[:60])
    return out

def attach_descs(items):
    """把官方摘要按标题匹配到条目上(先精确、再相似度>=0.6);摘要缓存在 gist_cache.json(随 Actions 缓存保留)。"""
    cache = json.loads(GIST_F.read_text(encoding="utf-8")) if GIST_F.exists() else {}
    for k, v in fetch_desc_feeds().items():
        cache.pop("desc:" + k, None); cache["desc:" + k] = v
    for k in [k for k in cache if k.startswith("desc:")][:-800]: del cache[k]
    GIST_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    entries = [(tokens(v["t"]), v["d"]) for k, v in cache.items() if k.startswith("desc:") and isinstance(v, dict)]
    hit = 0
    for it in items:
        if it.get("desc"): continue
        v = cache.get("desc:" + norm(it["title"])[:80]); d = v["d"] if isinstance(v, dict) else None
        if not d:
            tk, best = tokens(it["title"]), 0.6
            for etk, ed in entries:
                j = jacc(tk, etk)
                if j >= best: d, best = ed, j
        if d: it["desc"] = d; hit += 1
    print(f"attached official summaries to {hit} items")

# ---------- 无法抓取的文章:借同一事件在其他媒体(BBC/卫报/CNBC)的公开 RSS 摘要补大意,并标注出处 ----------
ALT_STOP = set("the and for with from that this has have will are was were its his her new says say after over into amid about than more most also but not can could would may how why who what".split())
def _etok(t): return set(re.findall(r"[a-z0-9]{3,}", (t or "").lower())) - ALT_STOP

def _first_para(raw):
    m = re.search(r"<p[^>]*>(.*?)</p>", raw or "", re.S | re.I)
    return m.group(1) if m else (raw or "")

def fetch_alt_feeds():
    out = []
    for f in CFG.get("alt_feeds", []):
        try:
            req = urllib.request.Request(f["url"], headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r: root = ET.fromstring(r.read())
            n = 0
            for el in root.iter("item"):
                g = {strip_ns(c.tag): c for c in el}
                t = clean_title((g["title"].text or "") if "title" in g else "")
                raw = g["description"].text if "description" in g and g["description"].text else ""
                dsc = clean_desc(_first_para(raw), t)
                if t and _good(dsc): out.append({"name": f["name"], "t": t, "d": _cut(dsc, 320), "tk": _etok(t)}); n += 1
            print(f"[ok]   alt feed {f['name']} ({f['url'].split('/')[-2] or f['url'][:30]}): {n}")
        except Exception as e:
            print("[alt feed fail]", f.get("name"), str(e)[:60])
    return out

def _same_event(a, b):
    """让模型判断两条标题是不是同一个具体事件(不是同一话题)。只在相似度处于灰区时调用。
    用 config 里的 verify_model(判断更准);该模型不可用时回退到默认模型。宁可漏判也不误配。"""
    prompt = ("Two news headlines. Do they report the SAME specific news event? Different outlets often word the same event differently, "
              "and one headline may cover several stories (a news digest) - if the core event of A is the same as B, answer yes. "
              "Different aspects, reactions or related-but-separate events are NOT the same. "
              'Reply with JSON only: {"same": true} or {"same": false}.\nA: ' + a + "\nB: " + b)
    vm = CFG.get("verify_model") or ""
    for model, extra in (([(vm, {"enable_thinking": False} if "Qwen3" in vm else {})] if vm else []) + [(None, None)]):
        try:
            out = _chat(prompt, model, extra)
            return json.loads(re.search(r"\{.*\}", out, re.S).group(0)).get("same") is True
        except Exception as e:
            print("[same-event check fail]", model or "default", str(e)[:50])
    return False

def attach_alt(headline, items):
    """头条里没有摘要的英文条目:到其他媒体的公开 RSS 里找同一事件的摘要。结果写入 it['desc'] / it['desc_via']。"""
    by = {i["gid"]: i for i in items}
    need = [by[h["gid"]] for h in headline if h["gid"] in by and not by[h["gid"]].get("desc") and not has_cjk(by[h["gid"]]["title"])]
    if not need or not CFG.get("alt_feeds"): return
    alts = fetch_alt_feeds()
    if not alts: return
    cache = json.loads(GIST_F.read_text(encoding="utf-8")) if GIST_F.exists() else {}
    has_key = bool(os.environ.get("OPENAI_API_KEY")); hi, lo, hit = CFG.get("alt_match_hi", 0.3), CFG.get("alt_match_lo", 0.2), 0
    for it in need:
        ta = _etok(it["title"])
        best = max(alts, key=lambda a: jacc(ta, a["tk"])); sc = jacc(ta, best["tk"])
        ok = sc >= hi
        if not ok and sc >= lo and has_key:
            ck = "same:" + hashlib.sha1((it["title"] + "|" + best["t"]).encode()).hexdigest()[:16]
            if ck not in cache: cache[ck] = _same_event(it["title"], best["t"])
            ok = bool(cache[ck])
        if ok:
            it["desc"], it["desc_via"] = best["d"], best["name"]; hit += 1
            print(f"[alt] {sc:.2f} {it['title'][:40]}  <-  {best['name']}: {best['t'][:44]}")
    GIST_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"filled {hit}/{len(need)} headline items from other outlets")

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
        ck = h["gid"] + ":v5"
        if ck in cache: h.update(cache[ck]); continue
        g = {"gist": "", "gist_en": ""}
        desc = body = ""
        try: desc, body = _article_text(it.get("link", it["url"]))
        except Exception as e: print("[gist fetch fail]", (it.get("zh") or it["title"])[:30], str(e)[:50])
        text = (desc + " " + body).strip()
        via = ""
        if not _good(text):
            text = it.get("desc", ""); via = it.get("desc_via", "")      # 原文抓不到/太短时,用官方 RSS 摘要(或其他媒体的同事件摘要)
        if has_key and _good(text):
            try:
                out = _chat(("注意:下面的摘要来自" + via + "对同一事件的报道,不是这条新闻原稿;只概括摘要里有的信息,不要写成原稿内容。\n" if via else "") + "根据下面这条新闻的标题和正文片段,分别写中文和英文各2到3句话的大意(中文80到140字;英文40到70词),适合语音播报。"
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
        if g["gist"] and not has_cjk(g["gist"]): g["gist"] = ""          # 中文大意必须是中文
        if g["gist_en"] and has_cjk(g["gist_en"]): g["gist_en"] = ""     # 英文大意不能夹中文
        if via and (g["gist"] or g["gist_en"]): g["via"] = via
        h.update(g)
        if g["gist"] or g["gist_en"]: cache[ck] = g
    GIST_F.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return headline

# ---------- 品种影响指数(模型只依据近24小时标题打分,不预测价格) ----------
IMPACT_INST = [("USOIL", "美国原油", "US Crude Oil"), ("XAUUSD", "黄金", "Gold"), ("BTCUSD", "比特币", "Bitcoin"),
               ("US500", "标普500", "S&P 500"), ("HK50", "恒生指数", "Hang Seng"), ("SSE", "上证指数", "Shanghai Composite")]

# 依据质量过滤(两层):
#  1) 品种专属词(strong)命中才算直接相关;通用宏观词(macro:关税、美联储、利率……)命中时,必须同时带明确的方向词才算依据
#  2) 方向以标题里的方向词为准(覆盖模型给的方向);同一标题多空方向词并存则视为方向不明,丢弃
_EQ_NEG = (r"tariffs?\s+(?:hikes?|raise\w*|threats?|target\w*|levies|plans?|on|against)|(?:new|fresh|higher|revive[sd]?|reveals?|unveil\w*|announce[sd]?|impos\w+|threat\w*)\s+(?:\w+\s+){0,3}tariffs?"
           r"|sell-?off|\brout\b|slump|tumble|plunge|\bslides?\b|hawkish|rate hikes?|hike rates|sticky inflation|recession|export controls?|chip curbs?|\bcurbs?\b|crackdown|sanctions?"
           r"|抛售|暴跌|下跌|加征关税|新关税|加息|鹰派|衰退|制裁|芯片限制|出口管制")
_EQ_POS = (r"\brall(?:y|ies|ied)\b|rebound\w*|bounce\w*|\bgains?\b|record high|\bsurge[sd]?\b|\bsoar\w*|rate cuts?|cuts? rates|dovish|truce|trade (?:deal|pact|agreement)|stimulus|easing"
           r"|tariff (?:cut|relief|exemption)|not as dire|lifts? (?:sanctions|curbs|restrictions)|eases? (?:sanctions|curbs|export)|removed? from (?:the )?(?:sanctions|entity) list"
           r"|反弹|大涨|降息|刺激|停战|贸易协议|宽松|鸽派|降低关税|取消关税|移出|解除|豁免|放宽|取消制裁")
_CHINA = r"hong kong|hang seng|china|chinese|beijing|\bxi\b|yuan|renminbi|alibaba|tencent|xiaomi|byd|huawei|港股|恒生|中国|北京|人民币|阿里|腾讯|华为"
IMPACT_SPEC = {
    "USOIL": dict(strong=r"\boil\b|crude|\bopec\b|brent|\bwti\b|refiner|gasoline|diesel|pipeline|tanker|原油|石油|油价|炼油|油轮|欧佩克",
                  macro=r"iran|houthi|red sea|middle east|hormuz|saudi|libya|iraq|russia\w* (?:oil|energy|sanction)|伊朗|胡塞|红海|中东|霍尔木兹|沙特",
                  pos=r"attacks?|strikes?|drone|missile|houthi|sanction|supply (?:concerns?|disruption|cuts?|fears)|output cuts?|production cuts?|outage|blockade|\brises?\b|\bclimbs?\b|\bjumps?\b|\bsurges?\b|袭击|攻击|制裁|减产|供应担忧|上涨",
                  neg=r"glut|oversuppl\w+|demand (?:concerns?|fears|slump|weak\w*)|(?:output|production) (?:increases?|hikes?)|raises? (?:output|production)|ceasefire|peace (?:deal|talks)|inventory build|\bfalls?\b|\bdrops?\b|\bslumps?\b|recession|增产|供应过剩|需求疲软|停火|下跌"),
    "XAUUSD": dict(strong=r"\bgold\b|bullion|precious metals?|safe[- ]haven|黄金|金价|贵金属|避险",
                   macro=r"\bfed\b|federal reserve|powell|fomc|interest rates?|rate (?:hike|cut)s?|inflation|treasury|bond yields?|dollar|nuclear|\bwar\b|escalat\w+|conflict|美联储|鲍威尔|加息|降息|利率|通胀|美元|国债|核武|战争",
                   pos=r"dollar (?:falls?|slides?|weak\w*|drops?)|dovish|rate cuts?|cuts? rates|safe[- ]haven|yields? (?:fall|drop|slip)|\bwar\b|nuclear|escalat\w+|conflict|美元(?:走弱|下跌)|降息|鸽派|避险|战争|核武|paus(?:e|es|ed)\b",
                   neg=r"dollar (?:rall\w+|strength\w*|gains?|surges?|climbs?)|strong dollar|hawkish|rate hikes?|hike rates|yields? (?:rise|climb|jump|surge)|sticky inflation|美元(?:走强|上涨)|加息|鹰派|通胀(?:顽固|居高)"),
    "BTCUSD": dict(strong=r"bitcoin|crypto|stablecoin|\bbtc\b|ether(?:eum)?\b|digital assets?|比特币|加密|稳定币|数字资产",
                   macro=r"\bai\b|nasdaq|risk appetite|credit|liquidity|\bfed\b|interest rates?|rate (?:hike|cut)s?|纳斯达克|人工智能|风险偏好|流动性|美联储|加息|降息",
                   pos=r"\brall(?:y|ies|ied)\b|rebound\w*|bounce\w*|\bsurge[sd]?\b|\bsoar\w*|record high|inflows?|etf approv\w+|rate cuts?|dovish|easing|premature|overdone|overblown|反弹|大涨|流入|降息|宽松|鸽派",
                   neg=r"sell-?off|\brout\b|slump|plunge|tumble|crash|panic|crackdown|\bbans?\b|banned|hawkish|rate hikes?|tighten\w*|liquidat\w+|outflows?|抛售|暴跌|恐慌|打压|禁止|鹰派|加息|收紧|流出"),
    "US500": dict(strong=r"nasdaq|s&p|wall street|dow jones|u\.?s\.? (?:stocks|equities|shares)|\bai (?:stocks?|trade|rout)|nvidia|美股|纳斯达克|标普|英伟达",
                  macro=r"\bfed\b|federal reserve|powell|interest rates?|rate (?:hike|cut)s?|inflation|tariffs?|trade (?:war|deal|truce)|earnings|recession|美联储|加息|降息|通胀|关税|财报|衰退",
                  pos=_EQ_POS, neg=_EQ_NEG),
    "HK50": dict(strong=_CHINA, macro=r"tariffs?|chip curbs?|export controls?|关税|芯片限制|出口管制", pos=_EQ_POS, neg=_EQ_NEG),
    "SSE": dict(strong=r"china|chinese|beijing|shanghai|\bxi\b|yuan|renminbi|pboc|csrc|mofcom|a-shares?|商务部|中国|北京|上海|央行|证监会|A股|财政部|国务院|人民币",
                macro=r"tariffs?|chip curbs?|export controls?|关税|芯片限制|出口管制", pos=_EQ_POS + r"|支持|促进|扩大开放", neg=_EQ_NEG),
}
IMPACT_SPEC = {k: {n: re.compile(v, re.I) for n, v in d.items()} for k, d in IMPACT_SPEC.items()}

def _ev_check(k, title, zh=""):
    """返回 (是否可作依据, 强制方向)。强制方向 0 表示沿用模型给的方向。"""
    sp, txt = IMPACT_SPEC[k], title + " " + zh
    strong, macro = bool(sp["strong"].search(txt)), bool(sp["macro"].search(txt))
    if not (strong or macro): return False, 0
    pos, neg = bool(sp["pos"].search(txt)), bool(sp["neg"].search(txt))
    if pos and neg: return False, 0                    # 多空方向词并存:方向不明,不作依据
    if pos or neg: return True, (1 if pos else -1)
    return strong, 0                                   # 没有方向词:只有品种专属词命中才放行


def make_impact(items, headline, demo=False):
    if demo or not os.environ.get("OPENAI_API_KEY"): return None
    hg = {h["gid"] for h in headline}
    pool = [it for it in items if NOW - it["dt"] <= timedelta(hours=24) and not LOWV.search(it["title"] + " " + it.get("zh", ""))]
    pool.sort(key=lambda it: (it["gid"] not in hg, -it["dt"].timestamp()))
    picked, gov = [], 0
    for it in pool:                                   # 部委稿子多为例行通知,最多取 8 条
        if it["src"] == "gov":
            if gov >= 8: continue
            gov += 1
        picked.append(it)
        if len(picked) >= CFG.get("impact_max_items", 70): break
    if len(picked) < 8: return None
    lines = []
    for n, it in enumerate(picked, 1):                # 带编号,让模型引用依据;英文用原标题,避免翻译错误带偏判断
        age = round((NOW - it["dt"]).total_seconds() / 3600, 1)
        lines.append("[%d] (%sh前) %s" % (n, age, it["title"]))
    keys = ",".join(k for k, _, _ in IMPACT_INST)
    prompt = ("你是金融新闻分析助手。下面是近24小时的新闻标题,每条有编号。请只依据这些标题,判断其对以下6个品种未来1到2天的消息面影响:"
              "USOIL(美国原油WTI)、XAUUSD(黄金)、BTCUSD(比特币)、US500(标普500)、HK50(恒生指数)、SSE(上证指数)。\n"
              "规则:score 为整数,范围 -5 到 5;正数=利好(有利于价格上行),负数=利空,0=标题里没有明显相关信息。\n"
              "参考:原油——中东/红海/俄乌供应冲击、库存下降为利好,需求担忧、增产为利空;黄金——避险、降息预期、美元走弱为利好,加息/鹰派、美元走强为利空;"
              "比特币——风险偏好和流动性改善为利好,紧缩、风险资产抛售、监管打压为利空;股指——经济与盈利、政策支持为利好,关税升级、利率预期偏鹰、科技股抛售为利空。\n"
              "要求:1) 不要编造标题里没有的信息或具体价格;2) 每个品种的理由 zh 不超过40个汉字,en 不超过25个英文单词,必须点出具体是哪条消息;"
              "3) 利好利空并存时给较小的分数;4) summary_zh/summary_en 各用一句话概括整体基调;"
              "5) 没有直接提到该品种的标题时,不要写“未提及”或直接判0,而要结合全球风险情绪、美元与利率预期、关税与贸易摩擦、AI股票抛售、地缘冲突等宏观线索推断方向,"
              "线索明确时给 ±1 或 ±2,理由写成“某某消息→影响逻辑”;只有确实找不到任何关联时才给0;"
              "6) 每个品种必须用 ev 列出打分所依据的新闻:n=新闻编号(只能用下面出现过的编号),d=该条的方向(1利好或-1利空),w=影响大小(1到3);"
              "每个品种最多4条,score 不为0时至少1条,不得引用无关新闻。\n"
              "只输出JSON对象,格式:{\"summary_zh\":\"...\",\"summary_en\":\"...\",\"items\":{\"USOIL\":{\"score\":0,\"zh\":\"...\",\"en\":\"...\",\"ev\":[{\"n\":1,\"d\":1,\"w\":2}]},...}},"
              "items 必须包含这些键:" + keys + "。\n新闻标题:\n" + "\n".join(lines))
    def one(model, extra):                              # 单次打分:解析 + 依据校验 + 一致性规整
        out = _chat(prompt, model, extra)
        j = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        res = {}
        for k, zh_name, en_name in IMPACT_INST:
            x = j["items"][k]
            sc = max(-5, min(5, int(round(float(x["score"])))))
            zh = str(x.get("zh", "")).strip(); en = str(x.get("en", "")).strip()
            if not has_cjk(zh): zh = ""
            if has_cjk(en): en = ""
            ev, seen, dropped = [], set(), 0
            for e in (x.get("ev") or [])[:6]:           # 依据:编号必须存在,方向只取正负号,权重限制在 1~3
                try:
                    n = int(e["n"]); d = 1 if float(e["d"]) > 0 else -1; w = max(1, min(3, int(round(float(e.get("w", 1))))))
                except Exception: continue
                if not (1 <= n <= len(picked)) or n in seen: continue
                seen.add(n); it = picked[n - 1]
                ok, fd = _ev_check(k, it["title"], it.get("zh", ""))           # 依据质量过滤:相关性 + 方向
                if not ok:
                    dropped += 1; continue
                ev.append({"g": it["gid"], "d": fd or d, "w": w})
            ev = ev[:4]
            res[k] = _impact_fix(sc, ev, zh, en, zh_name, en_name, k, dropped)
        sz, se = str(j.get("summary_zh", "")).strip(), str(j.get("summary_en", "")).strip()
        return res, (sz if has_cjk(sz) else ""), (se if not has_cjk(se) else "")

    runs, n_want = [], max(1, int(CFG.get("impact_runs", 3)))
    for r in range(n_want):                             # 同一批新闻打多次,取中位数,减小单次随机波动
        temp = 0.1 if r == 0 else 0.5
        for model in (CFG.get("impact_model") or None, None):
            extra = {"enable_thinking": False, "temperature": temp, "max_tokens": 2500} if model else {"temperature": temp, "max_tokens": 2500}
            try:
                runs.append(one(model, extra)); break
            except Exception as e:
                print("[impact fail] run", r + 1, (model or "default"), str(e)[:80])
    if not runs: return None
    res = []
    for k, zh_name, en_name in IMPACT_INST:
        rs = [x[0][k] for x in runs]
        sc_all = sorted(x["score"] for x in rs); m = len(sc_all)
        med = sc_all[m // 2] if m % 2 else min(sc_all[m // 2 - 1: m // 2 + 1], key=abs)      # 偶数次取离 0 更近的一个,偏保守
        sgn = lambda v: (v > 0) - (v < 0)
        agree = [x for x in rs if sgn(x["score"]) == sgn(med)]
        freq = {}
        for x in agree:
            for e in x["ev"]:
                f = freq.setdefault(e["g"], {"n": 0, "d": 0, "w": 0}); f["n"] += 1; f["d"] += e["d"]; f["w"] += e["w"]
        pool_ev = [(g, f) for g, f in freq.items() if f["n"] >= 2] if m >= 3 else []
        if not pool_ev: pool_ev = list(freq.items())
        pool_ev.sort(key=lambda gf: (-gf[1]["n"], -gf[1]["w"]))
        ev = [{"g": g, "d": 1 if f["d"] > 0 else -1, "w": max(1, min(3, round(f["w"] / f["n"])))} for g, f in pool_ev[:4]] if med != 0 else []
        pick = next((x for x in agree if x["score"] == med and x["zh"]), None) or next((x for x in agree if x["zh"]), None) or rs[0]
        fixed = _impact_fix(med, ev, pick["zh"], pick["en"], zh_name, en_name, k, 0)
        fixed["runs"] = m; fixed["agree"] = len(agree)
        fixed["weak"] = bool(fixed["score"] != 0 and (len(fixed["ev"]) <= 1 or len(agree) * 3 < m * 2))   # 只有一条依据,或不到 2/3 的运行一致
        res.append(fixed)
    base = runs[0]
    print("[impact] %d runs, spread %s" % (len(runs), {x["k"]: sorted(r[0][x["k"]]["score"] for r in runs) for x in res}))
    return {"built": NOW.strftime("%Y-%m-%d %H:%M"), "n": len(picked), "items": res, "runs": len(runs), "summary": base[1], "summary_en": base[2]}

def _impact_fix(sc, ev, zh, en, zh_name, en_name, k, dropped):
    note = ""
    if sc != 0 and not ev:                          # 没有(相关)依据的分数不展示:归零
        sc, note = 0, "没有直接相关依据"
    elif sc != 0 and sc * sum(e["d"] * e["w"] for e in ev) < 0:      # 分数方向与依据方向矛盾:归零
        sc, note = 0, "依据方向与判断矛盾,按中性处理"
    elif sc != 0 and abs(sc) > sum(e["w"] for e in ev):             # 分数不能超过依据所能支撑的强度
        sc = (1 if sc > 0 else -1) * sum(e["w"] for e in ev)
    if dropped: print("[impact] %s 丢弃 %d 条不相关依据" % (k, dropped))
    if sc == 0 and not ev and not note: note = "没有直接相关依据"
    if note: zh, en = note, ("No directly relevant evidence" if "没有" in note else "Evidence conflicts with the call; treated as neutral")
    return {"k": k, "name": zh_name, "name_en": en_name, "score": sc, "zh": zh, "en": en, "ev": ev}

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
    return {"id": i, "gid": it["gid"], "src": it["src"], "dept": it["dept"], "dept_en": CFG.get("dept_en", {}).get(it["dept"], ""),
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
    headline = build_headline(items)
    if not demo: attach_alt(headline, items)
    headline = make_gists(headline, items, demo)
    impact = make_impact(items, headline, demo)
    data = [to_record(it, n) for n, it in enumerate(items)]
    tags = [{"k": k, "c": v["color"]} for k, v in CFG["tags"].items()] + [{"k": CFG["other_tag"]["name"], "c": CFG["other_tag"]["color"]}]
    boot = {"src": {k: {"label": v["label"], "color": v["color"]} for k, v in CFG["sources"].items()},
            "tags": tags, "data": data, "headline": headline, "impact": impact, "meta": {"built": NOW.strftime("%Y-%m-%d %H:%M"), "n": len(data)}}
    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("/*__BOOT__*/null", json.dumps(boot, ensure_ascii=False))
    out = ROOT / "dist"; out.mkdir(exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(boot, ensure_ascii=False), encoding="utf-8")
    print(f"built {len(data)} items, {len(headline)} headlines -> {out/'index.html'}")

if __name__ == "__main__":
    main()
