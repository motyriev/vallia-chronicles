# -*- coding: utf-8 -*-
"""Летописец: телеграм-бот → OpenRouter → архив «Семейное древо».

Запускается GitHub Actions по крону. Ничего не хранит в памяти между
запусками: состояние — в letopisec/state.json, очередь модерации —
в letopisec/pending/*.json. Файлы коммитит workflow после скрипта.
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "index.html")
STATE_PATH = os.path.join(ROOT, "letopisec", "state.json")
PENDING_DIR = os.path.join(ROOT, "letopisec", "pending")

TG_API = "https://api.telegram.org/bot" + os.environ["TG_BOT_TOKEN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.environ.get("LETOPISEC_MODEL") or "anthropic/claude-sonnet-4.6"
ALLOWED = {s.strip() for s in (os.environ.get("TG_ALLOWED_IDS") or "").split(",") if s.strip()}
SITE_URL = (os.environ.get("SITE_URL") or "").rstrip("/")

# Разделы читаются из самого архива (index.html) — новые разделы
# подхватываются без правок кода. Заполняется в read_archive().
SECTIONS = set()
SECTION_RU = {}
PROMPT = open(os.path.join(HERE, "prompt.md"), encoding="utf-8").read()


# ── низкоуровневое ──────────────────────────────────────────────

def http(url, payload=None, headers=None, timeout=180):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tg(method, **kw):
    try:
        r = http(f"{TG_API}/{method}", kw, timeout=30)
        if method != "getUpdates":
            print(f"[tg] {method}: ok={r.get('ok')}")
        return r
    except Exception as e:  # телеграм упал — не роняем весь прогон
        print(f"[tg] {method}: ОШИБКА {e}")
        return {"ok": False}


def rich_extract(msg):
    """Текст и число фото из rich_message (Bot API 10.1+).
    Схема блоков может уточняться, поэтому обходим структуру целиком:
    собираем строки из полей text/caption/title в порядке следования,
    фото считаем по спискам PhotoSize."""
    rm = msg.get("rich_message")
    if not rm:
        return "", 0
    parts, photos = [], [0]

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("photo"), list):
                photos[0] += 1
            for key in ("text", "caption", "title"):
                v = node.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                elif isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip():
                    parts.append(v["text"].strip())
            for k, v in node.items():
                if k not in ("text", "caption", "title", "entities"):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(rm)
    # дедупликация соседних повторов (одна строка может встретиться и как блок, и как поле)
    out = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return "\n\n".join(out), photos[0]


def mask(x):
    s = str(x)
    return "…" + s[-4:] if len(s) > 4 else s


def say(chat_id, text, buttons=None):
    kw = {"chat_id": chat_id, "text": text[:4000]}
    if buttons:
        kw["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **kw)


# ── архив ───────────────────────────────────────────────────────

def read_archive():
    html = open(INDEX, encoding="utf-8").read()
    m = re.search(r'<script type="application/json" id="bestiary-data">([\s\S]*?)</script>', html)
    if not m:
        raise RuntimeError("index.html повреждён: нет блока bestiary-data")
    data = json.loads(m.group(1))
    SECTIONS.clear()
    SECTION_RU.clear()
    for s in data.get("sections") or []:
        SECTIONS.add(s["id"])
        SECTION_RU[s["id"]] = s.get("title") or s["id"]
    return html, data


def write_archive(html, data):
    dj = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    html2, n = re.subn(
        r'(<script type="application/json" id="bestiary-data">)[\s\S]*?(</script>)',
        lambda mm: mm.group(1) + "\n" + dj + "\n" + mm.group(2), html, count=1)
    if n != 1 or 'id="bestiary-data"' not in html2:
        raise RuntimeError("сборка index.html не удалась — файл не тронут")
    open(INDEX, "w", encoding="utf-8").write(html2)


DEFAULT_ENTRY = {
    "sub": None, "years": None, "yearLabel": None, "yearSort": None,
    "image": None, "epigraph": None, "body": [], "notes": [],
    "versions": [], "links": [], "participants": [],
}


def normalize(entry):
    e = dict(DEFAULT_ENTRY)
    e.update({k: v for k, v in entry.items() if v is not None or k in ("sub", "years")})
    e["id"] = entry.get("id", "")
    e["section"] = entry.get("section", "")
    e["title"] = (entry.get("title") or "").strip()
    if e["section"] != "chronicle":
        e["yearLabel"], e["yearSort"], e["participants"] = None, None, []
    e["image"] = None  # изображения — только вручную
    return e


def validate(entries, ops):
    ids = {e["id"] for e in entries}
    new_ids, errs = set(), []
    for op in ops:
        kind = op.get("op")
        if kind == "create":
            e = op.get("entry") or {}
            eid = e.get("id") or ""
            if not re.fullmatch(r"[a-z0-9-]{2,60}", eid):
                errs.append(f"недопустимый id: {eid!r}")
            elif eid in ids or eid in new_ids:
                errs.append(f"id уже занят: {eid}")
            else:
                new_ids.add(eid)
            if e.get("section") not in SECTIONS:
                errs.append(f"{eid}: неизвестный раздел {e.get('section')!r}")
            if not (e.get("title") or "").strip():
                errs.append(f"{eid}: пустое название")
            ys = e.get("yearSort")
            if ys is not None and not isinstance(ys, (int, float)):
                errs.append(f"{eid}: yearSort должен быть числом или null")
        elif kind == "append":
            if op.get("id") not in ids:
                errs.append(f"append к несуществующей записи: {op.get('id')!r}")
        elif kind != "need_human":
            errs.append(f"неизвестная операция: {kind!r}")
    all_ids = ids | new_ids
    for op in ops:
        links = ((op.get("entry") or {}).get("links") if op.get("op") == "create"
                 else op.get("add_links")) or []
        for l in links:
            if l.get("to") not in all_ids:
                errs.append(f"битая связь → {l.get('to')!r}")
        for p in ((op.get("entry") or {}).get("participants") or []):
            if p not in all_ids:
                errs.append(f"битый участник → {p!r}")
    return errs


def apply_ops(data, ops):
    by_id = {e["id"]: e for e in data["entries"]}
    for op in ops:
        if op["op"] == "create":
            data["entries"].append(normalize(op["entry"]))
        elif op["op"] == "append":
            e = by_id[op["id"]]
            e["body"] = (e.get("body") or []) + [s for s in (op.get("add_body") or []) if s]
            e["notes"] = (e.get("notes") or []) + [s for s in (op.get("add_notes") or []) if s]
            e["links"] = (e.get("links") or []) + (op.get("add_links") or [])


# ── редактор (OpenRouter) ───────────────────────────────────────

def call_editor(registry, posts_text):
    body = {
        "model": MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content":
                "РЕЕСТР АРХИВА (id | раздел | название | годы):\n" + registry +
                "\n\nНОВЫЙ ПОСТ (или несколько, разделены ---):\n" + posts_text},
        ],
    }
    resp = http("https://openrouter.ai/api/v1/chat/completions", body,
                {"Authorization": "Bearer " + OR_KEY,
                 "HTTP-Referer": SITE_URL or "https://github.com",
                 "X-Title": "Letopisec"})
    txt = resp["choices"][0]["message"]["content"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", txt)  # запасной план: модель добавила текст вокруг
        if not m:
            raise
        return json.loads(m.group(0))


# ── обработка новых постов ──────────────────────────────────────

def preview_ops(ops):
    lines = []
    for op in ops:
        if op["op"] == "create":
            e = op["entry"]
            lines.append(f"＋ {SECTION_RU.get(e.get('section'), '?')}: {e.get('title')}")
        elif op["op"] == "append":
            lines.append(f"✎ дополнение: {op['id']}")
    return "\n".join(lines)


def handle_posts(chat_id, texts, photos=0):
    print(f"[дiag] разбор пачки: {len(texts)} текст(а) для чата {mask(chat_id)}, фото в постах: {photos}")
    _, data = read_archive()
    print(f"[дiag] архив прочитан: {len(data['entries'])} записей; зову OpenRouter ({MODEL})…")
    sections_line = "; ".join(f"{sid} ({SECTION_RU[sid]})" for sid in SECTION_RU)
    registry = "РАЗДЕЛЫ: " + sections_line + "\n" + "\n".join(
        f"{e['id']} | {e['section']} | {e['title']} | {e.get('years') or ''}"
        for e in data["entries"])
    posts = "\n\n---\n\n".join(texts)
    say(chat_id, "Принял, разбираю летопись…")
    try:
        resp = call_editor(registry, posts)
    except Exception as e:
        say(chat_id, f"Редактор не справился ({e}). Перешлите пост ещё раз позже.")
        return
    print("[дiag] OpenRouter ответил")
    ops = [o for o in (resp.get("operations") or []) if isinstance(o, dict)]
    need = [o for o in ops if o.get("op") == "need_human"]
    real = [o for o in ops if o.get("op") in ("create", "append")]
    if not real:
        reason = ("; ".join(o.get("reason", "") for o in need)) or "не нашёл, что добавить"
        say(chat_id, f"Ничего не добавляю: {reason}")
        return
    errs = validate(data["entries"], real)
    if errs:
        say(chat_id, "Разобрал, но проверка не прошла:\n" + "\n".join(errs[:10]) +
            "\nЭтот пост нужно внести вручную через ✎ на сайте.")
        return
    pid = str(int(time.time() * 1000))
    os.makedirs(PENDING_DIR, exist_ok=True)
    json.dump(
        {"pid": pid, "chat_id": chat_id, "created": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
         "summary": resp.get("summary", ""), "operations": real, "posts": posts},
        open(os.path.join(PENDING_DIR, pid + ".json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)
    text = "Разобрал ✦\n\n" + (resp.get("summary") or "") + "\n\n" + preview_ops(real)
    if photos:
        text += f"\n\n※ В посте {photos} изображение(й) — я их не переношу, добавьте вручную через ✎ на сайте."
    if need:
        text += "\n\n✎ Доделать вручную на сайте (бот на это не отвечает):\n" + \
                "\n".join("— " + o.get("reason", "") for o in need)
    say(chat_id, text, buttons=[[
        {"text": "✦ Опубликовать", "callback_data": "ok:" + pid},
        {"text": "Пропустить", "callback_data": "no:" + pid},
    ]])


# ── обработка кнопок ────────────────────────────────────────────

def finalize_card(cq, status):
    """Убирает кнопки с исходной карточки и дописывает статус."""
    m = cq.get("message") or {}
    old_text = m.get("text") or ""
    tg("editMessageText",
       chat_id=m["chat"]["id"], message_id=m["message_id"],
       text=(old_text + "\n\n" + status)[:4000],
       reply_markup={"inline_keyboard": []})


def handle_callback(cq):
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    chat_id = cq["message"]["chat"]["id"]
    action, _, pid = (cq.get("data") or "").partition(":")
    path = os.path.join(PENDING_DIR, re.sub(r"[^0-9]", "", pid) + ".json")
    if not os.path.exists(path):
        finalize_card(cq, "※ Уже обработано ранее.")
        return
    pending = json.load(open(path, encoding="utf-8"))
    if action == "no":
        os.remove(path)
        finalize_card(cq, "✕ Пропущено — на сайт ничего не ушло.")
        return
    if action != "ok":
        return
    html, data = read_archive()  # заодно обновляет SECTIONS из архива
    errs = validate(data["entries"], pending["operations"])
    if errs:
        finalize_card(cq, "※ Не опубликовано: архив изменился, правки перестали сходиться.")
        say(chat_id, "Подробности:\n" + "\n".join(errs[:10]) + "\nВнесите этот пост вручную через ✎.")
        os.remove(path)
        return
    apply_ops(data, pending["operations"])
    write_archive(html, data)
    os.remove(path)
    link = (SITE_URL + "/") if SITE_URL else ""
    finalize_card(cq, "✦ Опубликовано. Через минуту-две правки появятся на сайте. " + link)


# ── главный цикл прогона ────────────────────────────────────────

def main():
    try:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        state = {"offset": 0}

    updates = tg("getUpdates", offset=state.get("offset", 0) + 1, timeout=0).get("result") or []
    if not updates:
        print("Новых сообщений нет.")
        return

    print(f"[дiag] получено обновлений: {len(updates)}; разрешённые чаты: {[mask(a) for a in ALLOWED] or 'ВСЕ (переменная пуста)'}")
    batches = {}  # chat_id -> [тексты постов]
    photo_counts = {}  # chat_id -> сколько фото в рич-постах пачки
    for u in updates:
        state["offset"] = max(state.get("offset", 0), u["update_id"])
        kinds = [k for k in u.keys() if k != "update_id"]
        print(f"[дiag] update {u['update_id']}: тип {kinds}")
        if "callback_query" in u:
            cq = u["callback_query"]
            print(f"[дiag]   callback от чата {mask(cq['message']['chat']['id'])}: {cq.get('data')}")
            if not ALLOWED or str(cq["message"]["chat"]["id"]) in ALLOWED:
                handle_callback(cq)
            continue
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            print("[дiag]   не message/channel_post — пропуск")
            continue
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or msg.get("caption") or ""
        photos = 0
        if not text.strip():
            text, photos = rich_extract(msg)
        print(f"[дiag]   чат {mask(chat_id)}, текст: {len(text)} симв. (rich: {bool(msg.get('rich_message'))}, фото в rich: {photos}), фото: {bool(msg.get('photo'))}, переслано: {bool(msg.get('forward_origin') or msg.get('forward_from_chat'))}")
        if not ALLOWED:
            say(chat_id, f"Архив пока никому не доверен. Ваш chat id: {chat_id} — "
                         f"добавьте его в переменную TG_ALLOWED_IDS репозитория.")
            continue
        if str(chat_id) not in ALLOWED:
            print(f"[дiag]   чат {mask(chat_id)} не в TG_ALLOWED_IDS — игнор")
            continue  # чужих молча игнорируем
        if text.strip():
            print("[дiag]   → в пачку на разбор")
            batches.setdefault(chat_id, []).append(text.strip())
            photo_counts[chat_id] = photo_counts.get(chat_id, 0) + photos
        elif msg.get("photo"):
            say(chat_id, "Изображения добавляются вручную через ✎ на сайте — "
                         "я работаю только с текстами.")
        else:
            kinds2 = [k for k in msg.keys() if k in
                      ("video", "document", "sticker", "voice", "audio", "poll", "story", "rich_message")]
            say(chat_id, "Не нашёл текста в этом сообщении"
                + (f" (тип: {', '.join(kinds2)})" if kinds2 else "")
                + ". Пересылайте текстовые посты — или напишите Артёму, если это выглядит как ошибка.")

    for chat_id, texts in batches.items():
        handle_posts(chat_id, texts, photo_counts.get(chat_id, 0))

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"))
    print(f"Обработано обновлений: {len(updates)}")


if __name__ == "__main__":
    main()
