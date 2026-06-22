"""
番剧 Tier 图生成脚本
====================
用法：
    python generate_tier.py [--anime-dir 番剧大全] [--output tier_chart.png]
                            [--thumb-w 120] [--thumb-h 170] [--cols 10]
                            [--deepseek-key YOUR_KEY] [--cache-dir .tier_cache]

参数说明：
  --anime-dir   番剧 md 文件所在目录，默认 ./番剧大全
  --output      输出图片路径，默认 tier_chart.png
  --thumb-w     每个封面宽度 px，默认 120
  --thumb-h     每个封面高度 px，默认 170
  --cols        每行最多放几张封面（0=自动根据图宽），默认 0
  --img-width   整体图片宽度 px，默认 2400
  --deepseek-key  DeepSeek API key（可选，用于兜底）
  --cache-dir   封面图缓存目录，默认 .tier_cache
  --force       忽略缓存，重新获取所有封面
"""

import os
import re
import sys
import json
import time
import hashlib
import argparse
import textwrap
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path


# ─────────────────────────────────────────────
# 工具函数：打印带颜色的进度
# ─────────────────────────────────────────────

def log(msg, color=None):
    codes = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m",
             "cyan": "\033[36m", "bold": "\033[1m", "reset": "\033[0m"}
    if color and color in codes:
        print(f"{codes[color]}{msg}{codes['reset']}", flush=True)
    else:
        print(msg, flush=True)


def log_step(step, total, msg):
    log(f"[{step}/{total}] {msg}", "cyan")


def log_ok(msg):
    log(f"  ✓ {msg}", "green")


def log_warn(msg):
    log(f"  ⚠ {msg}", "yellow")


def log_err(msg):
    log(f"  ✗ {msg}", "red")


# ─────────────────────────────────────────────
# Step 1：解析 md 文件
# ─────────────────────────────────────────────

TIER_ORDER = ["夯", "顶级", "人上人", "NPC", "拉完了"]
TIER_COLORS = {
    "夯":   {"bg": "#C0392B", "fg": "#FFFFFF"},
    "顶级":  {"bg": "#E67E22", "fg": "#FFFFFF"},
    "人上人": {"bg": "#F1C40F", "fg": "#000000"},
    "NPC":   {"bg": "#FAE5C8", "fg": "#000000"},
    "拉完了": {"bg": "#FFFFFF", "fg": "#000000"},
}


def parse_frontmatter(text):
    """解析 YAML frontmatter，返回 tags 列表和 source 字符串"""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return [], None
    fm = match.group(1)

    # 解析 tags
    tags = []
    in_tags = False
    for line in fm.split("\n"):
        stripped = line.strip()
        if re.match(r"^tags\s*:", stripped):
            in_tags = True
            continue
        if in_tags:
            if stripped.startswith("- "):
                tags.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                if ":" in stripped and not stripped.startswith("-"):
                    in_tags = False

    # 解析 source
    source = None
    src_match = re.search(r"^source:\s*(.+)$", fm, re.MULTILINE)
    if src_match:
        source = src_match.group(1).strip()

    return tags, source


def load_anime_list(anime_dir):
    """读取所有番剧 md，返回按 tier 分组的字典"""
    anime_dir = Path(anime_dir)
    if not anime_dir.exists():
        log_err(f"目录不存在：{anime_dir}")
        sys.exit(1)

    md_files = sorted(anime_dir.glob("*.md"))
    log(f"  发现 {len(md_files)} 个 md 文件", "bold")

    # tier → list of (title, source_url)
    grouped = {t: [] for t in TIER_ORDER}
    no_tier = []

    for f in md_files:
        title = f.stem
        try:
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            log_warn(f"读取失败：{f.name} — {e}")
            continue

        tags, source = parse_frontmatter(text)
        tier = None
        for tag in tags:
            if tag.startswith("评级-"):
                tier_name = tag[3:]
                if tier_name in grouped:
                    tier = tier_name
                    break

        if tier:
            grouped[tier].append({"title": title, "source": source})
        else:
            no_tier.append(title)

    # 统计
    for t in TIER_ORDER:
        log_ok(f"{t}：{len(grouped[t])} 部")
    if no_tier:
        log_warn(f"无评级（不纳入图）：{len(no_tier)} 部")

    return grouped


# ─────────────────────────────────────────────
# Step 2：获取封面图
# ─────────────────────────────────────────────

def http_get(url, timeout=15, retries=2):
    """
    HTTP GET 带重试和限流检测。
    遇到 429 自动等 3/6/12 秒后重试。
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AnimeTierGen/1.0 (anime tier list generator)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429 and attempt < retries:
                wait = 3 * (2 ** attempt)
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def extract_wiki_title_from_url(url):
    """从 Wikipedia URL 提取词条标题"""
    m = re.search(r"wikipedia\.org/wiki/(.+)$", url)
    if m:
        return urllib.parse.unquote(m.group(1))
    return None


def wiki_rest_cover(wiki_title, lang="en"):
    """
    用 Wikimedia REST API /page/summary/ 获取词条封面图 URL。
    REST API 的 thumbnail 字段比 Action API 更稳定可靠。
    """
    encoded = urllib.parse.quote(wiki_title, safe="")
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    data = http_get(url)
    if not data:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        # 优先用 originalimage（分辨率更高），fallback 到 thumbnail
        orig = j.get("originalimage", {})
        if orig.get("source"):
            return orig["source"]
        thumb = j.get("thumbnail", {})
        return thumb.get("source")
    except Exception:
        return None


def wiki_action_cover(wiki_title, lang="zh"):
    """用 Wikipedia Action API 获取词条封面图 URL（备用）"""
    encoded = urllib.parse.quote(wiki_title, safe="")
    api_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=query&titles={encoded}"
        f"&prop=pageimages&pithumbsize=500&format=json&redirects=1"
    )
    data = http_get(api_url)
    if not data:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        pages = j.get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail", {})
            if thumb.get("source"):
                return thumb["source"]
    except Exception:
        pass
    return None


def wiki_search_candidates(query, lang="zh", limit=5, retries=2):
    """用 Wikipedia opensearch 搜索，返回候选词条名列表（带重试）"""
    encoded = urllib.parse.quote(query, safe="")
    search_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=opensearch&search={encoded}&limit={limit}&format=json"
    )
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(1.5 * attempt)
        data = http_get(search_url)
        if not data:
            continue
        try:
            j = json.loads(data.decode("utf-8"))
            result = j[1] if len(j) > 1 else []
            if result:
                return result
        except Exception:
            pass
    return []


def try_get_cover_from_title(wiki_title, lang):
    """
    对一个词条名尝试：REST API → Action API，
    返回封面图 URL 或 None。
    """
    img = wiki_rest_cover(wiki_title, lang)
    if not img:
        img = wiki_action_cover(wiki_title, lang)
    return img


def wikidata_id_from_title(wiki_title, lang="zh"):
    """
    通过 Wikipedia API 获取词条的 Wikidata 实体 ID。
    e.g. 'Q40397' for '猫'
    """
    encoded = urllib.parse.quote(wiki_title, safe="")
    api_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=query&titles={encoded}&prop=pageprops&format=json&redirects=1"
    )
    data = http_get(api_url)
    if not data:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        pages = j.get("query", {}).get("pages", {})
        for page in pages.values():
            return page.get("pageprops", {}).get("wikibase_item")
    except Exception:
        pass
    return None


def en_title_from_wikidata(wikidata_id):
    """
    通过 Wikidata API 获取英文 Wikipedia 词条名。
    e.g. 'Q40397' → 'Cat'
    """
    encoded = urllib.parse.quote(wikidata_id, safe="")
    url = (
        f"https://www.wikidata.org/wiki/Special:EntityData/{encoded}.json"
    )
    data = http_get(url)
    if not data:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        entity = j.get("entities", {}).get(wikidata_id, {})
        sitelinks = entity.get("sitelinks", {})
        en_link = sitelinks.get("enwiki", {})
        return en_link.get("title")
    except Exception:
        return None


def search_cover_multilang(anime_title):
    """
    多语言搜索封面，4 层策略（每层都先尝试当前语言的封面）：
      1. 搜中文维基 → 找到则拿 zh 封面；同时获取 Wikidata ID 跨语言到 en 拿封面
      2. 搜日文维基 → ja 封面 + Wikidata 跨语言
      3. 搜英文维基 → en 封面
      4. 搜日文维基（如果用中文没搜到日文条目）
    返回 (img_url, method_desc) 或 (None, None)
    """
    # 策略1：中文维基搜索
    zh_candidates = wiki_search_candidates(anime_title, "zh", limit=3)
    for candidate in zh_candidates[:2]:
        time.sleep(0.2)
        img = try_get_cover_from_title(candidate, "zh")
        if img:
            return img, f"search_zh({candidate})"
        # zh 没封面 → 跨语言到英文
        wd_id = wikidata_id_from_title(candidate, "zh")
        if wd_id:
            en_title = en_title_from_wikidata(wd_id)
            if en_title:
                time.sleep(0.2)
                img = try_get_cover_from_title(en_title, "en")
                if img:
                    return img, f"wikidata_zh→en({en_title})"

    # 策略2：日文维基搜索
    ja_candidates = wiki_search_candidates(anime_title, "ja", limit=3)
    for candidate in ja_candidates[:2]:
        time.sleep(0.2)
        img = try_get_cover_from_title(candidate, "ja")
        if img:
            return img, f"search_ja({candidate})"
        # ja 没封面 → 跨语言到英文
        wd_id = wikidata_id_from_title(candidate, "ja")
        if wd_id:
            en_title = en_title_from_wikidata(wd_id)
            if en_title:
                time.sleep(0.2)
                img = try_get_cover_from_title(en_title, "en")
                if img:
                    return img, f"wikidata_ja→en({en_title})"

    # 策略3：英文维基直接搜索
    en_candidates = wiki_search_candidates(anime_title, "en", limit=3)
    for candidate in en_candidates[:2]:
        time.sleep(0.2)
        img = try_get_cover_from_title(candidate, "en")
        if img:
            return img, f"search_en({candidate})"

    return None, None


def deepseek_guess_wiki_title(anime_title, api_key):
    """
    用 DeepSeek 推断正确的英文 Wikipedia 词条名。
    仅在其他方法都失败时调用。
    返回 (en_title, ja_title) 或 (None, None)
    """
    if not api_key:
        return None, None
    try:
        payload = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an anime/manga encyclopedia expert. "
                        "Given a Chinese anime title, find its EXACT Wikipedia article titles.\n"
                        "Reply with ONLY two lines:\n"
                        "Line 1: English Wikipedia title (e.g. 'Charlotte (TV series)') or NONE\n"
                        "Line 2: Japanese Wikipedia title (e.g. 'シャーロット (アニメ)') or NONE\n"
                        "For manga/anime, include disambiguation like '(TV series)', '(manga)', '(film)'.\n"
                        "No explanation, no extra text."
                    )
                },
                {"role": "user", "content": anime_title}
            ],
            "max_tokens": 120,
            "temperature": 0.0,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            answer = result["choices"][0]["message"]["content"].strip()
            lines = answer.split("\n")
            en_title = lines[0].strip() if len(lines) > 0 else None
            ja_title = lines[1].strip() if len(lines) > 1 else None
            if en_title and en_title.upper() == "NONE":
                en_title = None
            if ja_title and ja_title.upper() == "NONE":
                ja_title = None
            return en_title, ja_title
    except Exception as e:
        return None, None


def fetch_cover(anime, cache_dir, deepseek_key, force=False):
    """
    获取单个番剧封面图字节流。
    策略优先级：
      1. 本地缓存
      2. source URL → 提取词条名 → REST API (zh/ja/en)
      3. 按番名多语言搜索 Wikipedia
      4. DeepSeek 推断英文词条名 → REST API
      5. 返回 None（绘占位色块）
    """
    title = anime["title"]
    source = anime.get("source")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_key = hashlib.md5(title.encode("utf-8")).hexdigest()
    cache_img = cache_dir / f"{cache_key}.jpg"
    cache_meta = cache_dir / f"{cache_key}.meta"

    # 读缓存
    if not force:
        if cache_img.exists() and cache_img.stat().st_size > 500:
            return cache_img.read_bytes(), "cache"
        if cache_meta.exists():
            meta = json.loads(cache_meta.read_text("utf-8"))
            status = meta.get("status", "")
            if status == "not_found":
                # 确认过 Wikipedia 上没有封面 → 跳过
                return None, "cache_miss"
            elif status == "net_error":
                # 上次是网络错误 → 可以重试（但加个小延时避免频繁撞墙）
                pass
            elif status == "no_wikipedia":
                # 确认 Wikipedia 无此条目 → 跳过
                return None, "cache_miss"
            # 未知状态 → 重试

    # --force 时删除旧缓存
    if force:
        if cache_img.exists():
            cache_img.unlink()
        if cache_meta.exists():
            cache_meta.unlink()

    img_url = None
    method = None

    # 策略1：source URL → 提取词条名 → 三语言 REST API
    if source:
        wiki_title = extract_wiki_title_from_url(source)
        if wiki_title:
            # 先尝试 source 的原语言，再转其他语言
            src_lang = "zh"
            if "en.wikipedia" in source:
                src_lang = "en"
            elif "ja.wikipedia" in source:
                src_lang = "ja"
            for lang in [src_lang] + [l for l in ["zh", "ja", "en"] if l != src_lang]:
                time.sleep(0.2)
                img_url = try_get_cover_from_title(wiki_title, lang)
                if img_url:
                    method = f"source_{lang}"
                    break

    # 策略2：多语言搜索
    if not img_url:
        img_url, method = search_cover_multilang(title)

    # 策略3：DeepSeek 兜底（同时试 en + ja）
    if not img_url and deepseek_key:
        guessed_en, guessed_ja = deepseek_guess_wiki_title(title, deepseek_key)
        for guessed, lang in [(guessed_en, "en"), (guessed_ja, "ja")]:
            if not guessed:
                continue
            time.sleep(0.3)
            img_url = try_get_cover_from_title(guessed, lang)
            if img_url:
                method = f"deepseek_{lang}({guessed})"
                break

    # 下载图片
    img_bytes = None
    download_error = None
    if img_url:
        img_bytes = http_get(img_url)
        if img_bytes is None:
            download_error = f"下载失败：{img_url[:60]}"

    # 写缓存（区分错误类型）
    if img_bytes and len(img_bytes) > 500:
        cache_img.write_bytes(img_bytes)
        # 下载成功，清掉旧的 meta（如果有）
        if cache_meta.exists():
            cache_meta.unlink()
    elif method:
        # 有搜索方法但最终没拿到有效图片
        img_bytes = None
        reason_map = {
            "source_zh": "zh wikipedida 词条无封面",
            "source_ja": "ja wikipedia 词条无封面",
            "source_en": "en wikipedia 词条无封面",
        }
        reason = reason_map.get(method, f"搜索到词条但无封面（方法：{method}）")
        cache_meta.write_text(json.dumps({
            "status": "net_error",
            "method": method,
            "error": download_error or reason,
            "img_url": img_url,
        }), "utf-8")
    elif source:
        # 有 source 链接但多语言都搜不到封面
        img_bytes = None
        method = method or "all_failed_source"
        wiki_title = extract_wiki_title_from_url(source) or "?"
        cache_meta.write_text(json.dumps({
            "status": "net_error",
            "method": "all_failed",
            "error": f"source 词条「{wiki_title}」在所有语言维基均无封面（可能限流或重定向）",
        }), "utf-8")
    else:
        # 无 source 且多语言搜不到 → 标记为可重试（下次会再试 + DeepSeek）
        img_bytes = None
        method = "all_failed"
        prev_attempts = 0
        if cache_meta.exists():
            old_meta = json.loads(cache_meta.read_text("utf-8"))
            prev_attempts = old_meta.get("attempts", 0)
        suffix = "（已重试多次，可能维基无此条目）" if prev_attempts >= 2 else ""
        cache_meta.write_text(json.dumps({
            "status": "net_error",
            "method": "all_failed",
            "error": f"中/日/英维基均未搜到「{title}」{suffix}",
            "attempts": prev_attempts + 1,
        }), "utf-8")

    return img_bytes, method


def fetch_all_covers(grouped, cache_dir, deepseek_key, force=False):
    """批量获取所有番剧封面，返回 {title: img_bytes_or_None}"""
    log(f"\n  开始获取封面图，缓存目录：{cache_dir}", "bold")
    all_anime = []
    for tier in TIER_ORDER:
        all_anime.extend(grouped[tier])

    total = len(all_anime)
    covers = {}
    found = 0
    not_found = 0
    cached = 0
    failed = []  # 记录失败的 (title, error_reason)

    for i, anime in enumerate(all_anime, 1):
        title = anime["title"]
        cache_key = hashlib.md5(title.encode("utf-8")).hexdigest()
        cache_img_path = cache_dir / f"{cache_key}.jpg"
        cache_meta_path = cache_dir / f"{cache_key}.meta"
        is_cached = not force and cache_img_path.exists() and cache_img_path.stat().st_size > 500

        # 进度指示：C=缓存, ·=需要网络
        prefix = " C" if is_cached else " ·"
        sys.stdout.write(f"\r  [{i:3d}/{total}]{prefix} {title[:24]:<24}  ")
        sys.stdout.flush()

        img_bytes, method = fetch_cover(anime, cache_dir, deepseek_key, force)
        covers[title] = img_bytes

        if img_bytes:
            found += 1
            if method == "cache":
                cached += 1
        else:
            not_found += 1
            # 读取 meta 输出失败原因
            reason = "未知原因"
            if cache_meta_path.exists():
                meta = json.loads(cache_meta_path.read_text("utf-8"))
                reason = meta.get("error", "未知原因")
            failed.append((title, reason))

        # 已缓存的秒过，实际请求的等 0.6s 防限流
        if method not in ("cache", "cache_miss"):
            time.sleep(0.6)

    sys.stdout.write("\r" + " " * 60 + "\r")
    log_ok(f"封面获取完成：{found} 成功（{cached} 缓存） / {not_found} 未找到")

    # 失败详情
    if failed:
        log(f"\n  未获取到封面的番剧（{len(failed)} 部）：", "yellow")
        for title, reason in failed:
            log_warn(f"    {title}")
            log(f"      原因：{reason}", "yellow")
        log(f"\n  💡 提示：失败多为网络限流导致，再次运行本脚本即可补全", "bold")
        log(f"     已提供的 DeepSeek API key 也会辅助搜索冷门番剧", "bold")

    return covers


# ─────────────────────────────────────────────
# Step 3：绘制 Tier 大图
# ─────────────────────────────────────────────

def load_image_from_bytes(img_bytes):
    """从字节流加载 PIL Image"""
    from PIL import Image
    import io
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        return img
    except Exception:
        return None


def make_placeholder(w, h, bg_hex, title):
    """生成占位色块（对应 tier 颜色底 + 番名）"""
    from PIL import Image, ImageDraw
    r, g, b = hex_to_rgb(bg_hex)
    # 占位块用浅色系：bg 颜色 + 透明感
    fill = (min(r + 80, 255), min(g + 80, 255), min(b + 80, 255))
    img = Image.new("RGBA", (w, h), color=(*fill, 255))
    draw = ImageDraw.Draw(img)
    # 细边框
    draw.rectangle([0, 0, w-1, h-1], outline=(max(r-20,0), max(g-20,0), max(b-20,0), 200), width=1)
    # 番名居中换行
    font = get_font(10)
    chars_per_line = max(1, w // 11)
    lines = []
    current = ""
    for ch in title:
        current += ch
        if len(current) >= chars_per_line:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    lines = lines[:4]
    total_h = len(lines) * 15
    y = (h - total_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        draw.text((x, y), line, font=font, fill=(50, 50, 50, 230))
        y += 15
    return img


def fit_cover(img_bytes, w, h, title, tier_bg):
    """将封面图缩放裁剪到 w×h，失败则返回占位块"""
    from PIL import Image
    result = None
    if img_bytes:
        result = load_image_from_bytes(img_bytes)
    if result is None:
        return make_placeholder(w, h, tier_bg, title)
    # 等比缩放后居中裁剪
    src_w, src_h = result.size
    scale = max(w / src_w, h / src_h)
    nw, nh = int(src_w * scale), int(src_h * scale)
    result = result.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    result = result.crop((left, top, left + w, top + h))
    return result


def get_font(size, bold=False):
    """获取字体，优先系统中文字体，无则用默认"""
    from PIL import ImageFont
    candidates = []
    if sys.platform == "win32":
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
            "C:/Windows/Fonts/simhei.ttf",     # 黑体
            "C:/Windows/Fonts/simsun.ttc",     # 宋体
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/Library/Fonts/Arial Unicode MS.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def draw_tier_chart(grouped, covers, output_path, thumb_w, thumb_h, img_width, cols_override):
    """绘制完整 Tier 大图"""
    from PIL import Image, ImageDraw

    LABEL_W = 160       # 左侧 tier 标签宽度
    PADDING = 8         # 封面间距
    ROW_PADDING_V = 12  # 行内上下留白
    BORDER_W = 3        # tier 行边框
    FONT_TIER = 42      # tier 标签字体大小
    FONT_TITLE = 11     # 封面下方番名字体大小

    content_w = img_width - LABEL_W
    # 每行能放多少个封面
    if cols_override > 0:
        cols = cols_override
    else:
        cols = max(1, (content_w + PADDING) // (thumb_w + PADDING))

    log(f"  布局参数：label={LABEL_W}px, 封面={thumb_w}×{thumb_h}px, 每行{cols}列")

    # 计算总高度
    total_h = 0
    row_heights = []
    for tier in TIER_ORDER:
        anime_list = grouped[tier]
        if not anime_list:
            continue
        rows = max(1, -(-len(anime_list) // cols))  # ceil div
        row_h = rows * (thumb_h + FONT_TITLE + 4 + PADDING) + ROW_PADDING_V * 2
        row_heights.append((tier, row_h))
        total_h += row_h + BORDER_W

    log(f"  最终图片尺寸：{img_width} × {total_h} px")

    # 创建画布
    canvas = Image.new("RGB", (img_width, total_h), color=(30, 30, 30))
    draw = ImageDraw.Draw(canvas)

    font_tier = get_font(FONT_TIER, bold=True)
    font_title = get_font(FONT_TITLE)

    y_offset = 0
    for tier, row_h in row_heights:
        anime_list = grouped[tier]
        colors = TIER_COLORS[tier]
        bg = hex_to_rgb(colors["bg"])
        fg = hex_to_rgb(colors["fg"])

        # 绘制行背景
        draw.rectangle([0, y_offset, img_width - 1, y_offset + row_h - 1],
                       fill=(20, 20, 20))
        # 绘制左侧标签区
        draw.rectangle([0, y_offset, LABEL_W - 1, y_offset + row_h - 1],
                       fill=bg)
        # tier 名称居中
        bbox = draw.textbbox((0, 0), tier, font=font_tier)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (LABEL_W - tw) // 2
        ty = y_offset + (row_h - th) // 2
        draw.text((tx, ty), tier, font=font_tier, fill=fg)

        # 绘制封面
        x0 = LABEL_W + PADDING
        y0 = y_offset + ROW_PADDING_V

        for idx, anime in enumerate(anime_list):
            col = idx % cols
            row = idx // cols
            x = x0 + col * (thumb_w + PADDING)
            y = y0 + row * (thumb_h + FONT_TITLE + 4 + PADDING)

            img_bytes = covers.get(anime["title"])
            thumb = fit_cover(img_bytes, thumb_w, thumb_h, anime["title"], colors["bg"])
            canvas.paste(thumb.convert("RGB"), (x, y))

            # 番名（截断后显示）
            short_title = anime["title"]
            if len(short_title) > 10:
                short_title = short_title[:9] + "…"
            tbbox = draw.textbbox((0, 0), short_title, font=font_title)
            tw2 = tbbox[2] - tbbox[0]
            tx2 = x + (thumb_w - tw2) // 2
            draw.text((tx2, y + thumb_h + 2), short_title,
                      font=font_title, fill=(200, 200, 200))

        # 底部分隔线
        y_offset += row_h
        draw.rectangle([0, y_offset, img_width - 1, y_offset + BORDER_W - 1],
                       fill=(50, 50, 50))
        y_offset += BORDER_W

    log(f"  保存图片到：{output_path}")
    canvas.save(str(output_path), quality=95)
    log_ok(f"图片已保存：{output_path}（{img_width}×{total_h} px）")


# ─────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="番剧 Tier 图生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--anime-dir", default="番剧大全", help="番剧 md 目录")
    parser.add_argument("--output", default="tier_chart.png", help="输出图片路径")
    parser.add_argument("--thumb-w", type=int, default=120, help="封面宽度 px")
    parser.add_argument("--thumb-h", type=int, default=170, help="封面高度 px")
    parser.add_argument("--cols", type=int, default=0,
                        help="每行列数（0=自动）")
    parser.add_argument("--img-width", type=int, default=2400, help="图片总宽 px")
    parser.add_argument("--deepseek-key",
                        default="sk-12b59a1c4acb4e54afde4d61a3203f63",
                        help="DeepSeek API key（已预置，可选覆盖）")
    parser.add_argument("--cache-dir", default=".tier_cache", help="封面缓存目录")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新获取")
    args = parser.parse_args()

    # 处理路径（相对路径相对于脚本所在目录）
    script_dir = Path(__file__).parent
    anime_dir = Path(args.anime_dir) if Path(args.anime_dir).is_absolute() \
        else script_dir / args.anime_dir
    output_path = Path(args.output) if Path(args.output).is_absolute() \
        else script_dir / args.output
    cache_dir = Path(args.cache_dir) if Path(args.cache_dir).is_absolute() \
        else script_dir / args.cache_dir

    log("\n========================================", "bold")
    log("   番剧 Tier 图生成器", "bold")
    log("========================================\n", "bold")

    # 检查依赖
    log_step(1, 4, "检查依赖（Pillow）")
    try:
        from PIL import Image, ImageDraw, ImageFont
        log_ok("Pillow 已安装")
    except ImportError:
        log_err("缺少 Pillow，请运行：pip install Pillow")
        sys.exit(1)

    # 读取番剧列表
    log_step(2, 4, f"读取番剧列表：{anime_dir}")
    grouped = load_anime_list(anime_dir)
    total = sum(len(v) for v in grouped.values())
    log_ok(f"共 {total} 部番剧纳入 Tier 图")

    # 获取封面
    log_step(3, 4, "获取封面图（带缓存）")

    # 每次运行前清空旧的 meta 文件，确保之前因限流失败的番剧会被重新尝试
    meta_files = list(cache_dir.glob("*.meta"))
    if meta_files:
        for mf in meta_files:
            mf.unlink()
        log_ok(f"已清除 {len(meta_files)} 个旧 meta 缓存（图片缓存保留，将重新检查失败的番剧）")

    covers = fetch_all_covers(grouped, cache_dir, args.deepseek_key, args.force)

    # 绘制大图
    log_step(4, 4, "绘制 Tier 大图")
    draw_tier_chart(
        grouped, covers, output_path,
        thumb_w=args.thumb_w,
        thumb_h=args.thumb_h,
        img_width=args.img_width,
        cols_override=args.cols
    )

    log("\n========================================", "bold")
    log_ok(f"完成！输出：{output_path}")
    log("========================================\n", "bold")


if __name__ == "__main__":
    main()
