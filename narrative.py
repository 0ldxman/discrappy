"""
Текстовые эвристики для превращения лога в повествование.

Здесь только чистые функции над строками — ни БД, ни HTTP. Их используют
и разрезание сообщений, и массовые операции, и экспорт:

  * split_fragments  — где резать склеенный пост (строки / абзацы / «умно»);
  * extract_author   — вытащить «(Имя) - » из начала реплики;
  * detect_role      — речь / действие / нарратив / OOC;
  * cleanup_text     — набор чисток (OOC, упоминания, markdown-мусор…).

Материал, под который всё это затачивалось, — ролевые логи Discord вида:

    **Пророк остановился, после чего обернулся к ним**
    (Пророк) - Революция. Пора освободить наших парней.

то есть два поста, отправленных одним сообщением.
"""

from __future__ import annotations

import re

# Роли реплик. Пустая строка — «не размечено».
ROLE_SPEECH = "speech"
ROLE_ACTION = "action"
ROLE_NARRATION = "narration"
ROLE_OOC = "ooc"
ROLES = (ROLE_SPEECH, ROLE_ACTION, ROLE_NARRATION, ROLE_OOC)

# «(Имя) - текст», «[Имя]: текст», «{Имя} — текст». Имя не длиннее 48 символов
# и без переводов строк — иначе это не префикс, а часть текста в скобках.
_BRACKET_NAME = re.compile(
    r"^[ \t]*[\(\[\{][ \t]*(?P<name>[^\)\]\}\n]{1,48}?)[ \t]*[\)\]\}][ \t]*[-—–:>]*[ \t]*",
)
# «Имя: текст» / «Имя — текст» без скобок. Опаснее (ловит «Он сказал: …»),
# поэтому применяется только к правдоподобным именам — см. _looks_like_name.
_BARE_NAME = re.compile(r"^[ \t]*(?P<name>[^\s:—\-][^:\n—]{0,46}?)[ \t]*[:—][ \t]+")

# Текст целиком в * или ** — типичная разметка действия.
_WRAPPED_ACTION = re.compile(r"^\*{1,3}(?P<text>.+?)\*{1,3}$", re.DOTALL)
# Реплика в кавычках или после тире в начале строки.
_QUOTED = re.compile(r"[\"«“„].+?[\"»“”]", re.DOTALL)
_DASH_SPEECH = re.compile(r"^[ \t]*[-—–][ \t]+\S")

_OOC_INLINE = re.compile(r"\(\((?:[^)]|\)(?!\)))*\)\)", re.DOTALL)
_MENTION = re.compile(r"<[@#][!&]?\d+>|@everyone|@here")
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_MD_CHARS = re.compile(r"(\*{1,3}|_{1,3}|~~|`{1,3}|\|\|)")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def _looks_like_name(value: str) -> bool:
    """Правдоподобно ли, что строка — имя персонажа, а не начало фразы."""
    value = value.strip()
    if not value or len(value) > 48:
        return False
    words = value.split()
    if len(words) > 4:
        return False
    # Каждое слово начинается с заглавной (или это не буква — «X-7», «№2»).
    return all(not w[0].isalpha() or w[0].isupper() for w in words)


def extract_author(text: str, known: set[str] | frozenset[str] = frozenset()
                   ) -> tuple[str | None, str]:
    """
    Отделяет имя говорящего от начала реплики.

    → (имя | None, текст без префикса). Скобочная форма «(Имя) - …» берётся
    всегда; форма без скобок «Имя: …» — только если имя уже встречалось в
    прогоне (`known`) или выглядит именем.
    """
    if not text:
        return (None, text or "")

    m = _BRACKET_NAME.match(text)
    if m:
        name = m.group("name").strip()
        if name:
            return (name, text[m.end():].strip())

    m = _BARE_NAME.match(text)
    if m:
        name = m.group("name").strip()
        if name and (name.casefold() in {k.casefold() for k in known}
                     or _looks_like_name(name)):
            return (name, text[m.end():].strip())

    return (None, text.strip())


def detect_role(text: str) -> str:
    """Роль реплики по её тексту. Пустой текст считаем нарративом."""
    t = (text or "").strip()
    if not t:
        return ROLE_NARRATION
    low = t.casefold()
    if (t.startswith("((") and t.endswith("))")) or low.startswith(("//", "ooc:", "ооц:")):
        return ROLE_OOC
    # «(Имя) - …» — почти всегда прямая речь.
    if _BRACKET_NAME.match(t):
        return ROLE_SPEECH
    if _WRAPPED_ACTION.match(t):
        return ROLE_ACTION
    if _QUOTED.search(t) or _DASH_SPEECH.match(t):
        return ROLE_SPEECH
    return ROLE_NARRATION


def strip_action_marks(text: str) -> str:
    """Убирает обрамляющие звёздочки у действия («**встал**» → «встал»)."""
    m = _WRAPPED_ACTION.match((text or "").strip())
    return m.group("text").strip() if m else (text or "").strip()


# --------------------------- Разрезание сообщений ----------------------------

def split_fragments(text: str, mode: str = "smart",
                    known: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """
    Режет склеенный пост на фрагменты.

      lines      — каждая непустая строка отдельно;
      paragraphs — по пустым строкам;
      smart      — по строкам, но соседние строки с одинаковой ролью и
                   одинаковым говорящим склеиваются обратно.

    Возвращает список фрагментов (>= 1). Если резать нечего — [text].
    """
    text = (text or "").strip()
    if not text:
        return [text]

    if mode == "paragraphs":
        parts = [p.strip() for p in re.split(r"\n[ \t]*\n", text)]
        return [p for p in parts if p] or [text]

    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) <= 1:
        return [text]
    if mode == "lines":
        return lines

    # smart: склеиваем подряд идущие строки с той же ролью и тем же говорящим.
    fragments: list[str] = []
    prev_key: tuple[str, str] | None = None
    for line in lines:
        name, _ = extract_author(line, known)
        key = (detect_role(line), (name or "").casefold())
        if prev_key is not None and key == prev_key:
            fragments[-1] += "\n" + line
        else:
            fragments.append(line)
        prev_key = key
    return fragments


def split_at_selection(text: str, start: int, end: int) -> list[str]:
    """
    Режет текст выделением на «до / выделенное / после», сохраняя порядок.

    Пустые части выбрасываются, поэтому результат — от 1 до 3 фрагментов.
    Границы приводятся к диапазону строки; выделение схлопнутое (start == end)
    режет текст надвое по курсору.
    """
    text = text or ""
    start = max(0, min(int(start), len(text)))
    end = max(0, min(int(end), len(text)))
    if start > end:
        start, end = end, start
    parts = [text[:start], text[start:end], text[end:]]
    return [p.strip() for p in parts if p.strip()] or [text.strip()]


# ------------------------------ Чистка текста --------------------------------

CLEANUP_OPS = {
    "ooc": "убрать ((OOC)) в скобках",
    "mentions": "убрать упоминания <@id>, @everyone",
    "emoji": "убрать кастомные эмодзи <:name:id>",
    "markdown": "убрать markdown-разметку (* _ ~ ` ||)",
    "blank": "схлопнуть пустые строки",
    "spaces": "схлопнуть повторяющиеся пробелы",
    "dashes": "дефис в начале реплики → тире",
}


def cleanup_text(text: str, ops: list[str] | set[str]) -> str:
    """Применяет выбранные чистки к тексту. Неизвестные операции игнорируются."""
    out = text or ""
    ops = set(ops or ())
    if "ooc" in ops:
        out = _OOC_INLINE.sub("", out)
    if "mentions" in ops:
        out = _MENTION.sub("", out)
    if "emoji" in ops:
        out = _CUSTOM_EMOJI.sub("", out)
    if "markdown" in ops:
        out = _MD_CHARS.sub("", out)
    if "dashes" in ops:
        out = re.sub(r"^([ \t]*)-(?=[ \t]+\S)", r"\1—", out, flags=re.MULTILINE)
    if "spaces" in ops:
        out = _MULTI_SPACE.sub(" ", out)
    if "blank" in ops:
        out = _MULTI_BLANK.sub("\n\n", out)
    return "\n".join(ln.rstrip() for ln in out.split("\n")).strip()


# --------------------------- Поиск и замена ----------------------------------

def apply_replace(text: str, find: str, repl: str, *, regex: bool = False,
                  case: bool = False) -> tuple[str, int]:
    """
    Замена в тексте. → (новый текст, число замен).

    regex=False — подстрока; case=False — регистр не учитывается.
    Некорректная регулярка поднимает re.error, вызывающий её ловит.
    """
    if not find:
        return (text, 0)
    flags = 0 if case else re.IGNORECASE
    pattern = find if regex else re.escape(find)
    new, n = re.subn(pattern, repl if regex else repl.replace("\\", "\\\\"),
                     text or "", flags=flags)
    return (new, n)
