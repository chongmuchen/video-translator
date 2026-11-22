"""Apply sentence-local readings through Kokoro's existing Chinese frontend."""

from __future__ import annotations

import logging
from copy import copy

from ..models import Pronunciation

LOG = logging.getLogger(__name__)
_MARKER = "\ue000"


def _normalized_readings(text, readings, normalize):
    """Map positions through number/punctuation normalization without guessing."""
    if _MARKER in text:
        LOG.warning("文本包含读音定位符，使用默认读音。")
        return {}
    marked = text
    ordered = sorted(readings, key=lambda reading: reading.start)
    for reading in reversed(ordered):
        marked = marked[:reading.start] + _MARKER + marked[reading.start:]
    normalized = normalize(text)
    parts = normalize(marked).split(_MARKER)
    if len(parts) != len(ordered) + 1 or "".join(parts) != normalized:
        LOG.warning("数字或标点规范化影响了注音位置，使用默认读音。")
        return {}
    positions = {}
    offset = len(parts[0])
    for reading, part in zip(ordered, parts[1:]):
        if not part.startswith(reading.char):
            LOG.warning("规范化后无法定位 %s 的读音，使用默认读音。", reading.char)
            return {}
        positions[offset] = reading
        offset += len(part)
    return positions


def _initial_final(reading):
    from pypinyin import Style, pinyin
    from pypinyin.style._utils import get_finals, get_initials

    candidates = pinyin(
        reading.char, style=Style.TONE3, heteronym=True, neutral_tone_with_five=True
    )[0]
    if reading.pinyin not in candidates:
        LOG.warning("%s 的读音 %s 不在本地词典中，使用默认读音。", reading.char, reading.pinyin)
        return None
    syllable = reading.pinyin[:-1].replace("v", "ü")
    initial = get_initials(syllable, strict=True)
    final = get_finals(syllable, strict=True).replace("ü", "v")
    if not final:
        LOG.warning("Kokoro 无法转换 %s 的读音 %s，使用默认读音。", reading.char, reading.pinyin)
        return None
    if final == "i" and initial in {"z", "c", "s"}:
        final = "ii"
    elif final == "i" and initial in {"zh", "ch", "sh", "r"}:
        final = "iii"
    return initial, final + reading.pinyin[-1]


def _frontend(frontend, text, readings):
    # Copy per request. No mutation of shared dictionaries or another voice's frontend.
    local = copy(frontend)
    tones = copy(frontend.tone_modifier)
    local.tone_modifier = tones
    cursor = 0
    active = {}

    def initials_finals(word):
        nonlocal cursor, active
        initials, finals = frontend._get_initials_finals(word)
        start = text.find(word, cursor)
        active = {}
        if start < 0 or len(initials) != len(word) or len(finals) != len(word):
            LOG.warning("无法定位分词 %s，使用默认读音。", word)
            return initials, finals
        cursor = start + len(word)
        for position, reading in readings.items():
            if start <= position < cursor:
                phones = _initial_final(reading)
                if phones is not None:
                    index = position - start
                    initials[index], finals[index] = phones
                    active[index] = phones[1]
        return initials, finals

    def neutral_tones(word, pos, finals):
        finals = frontend.tone_modifier._neural_sandhi(word, pos, finals)
        # Explicit readings take priority over blanket neutralization of 的/地/得.
        # The existing modified_tone then performs third-tone sandhi as usual.
        for index, final in active.items():
            finals[index] = final
        return finals

    local._get_initials_finals = initials_finals
    tones._neural_sandhi = neutral_tones
    return local(text)


class PronunciationG2P:
    """Wrap a single pipeline call, tracking its normal sentence chunks."""

    def __init__(self, base, text: str, readings: list[Pronunciation]):
        self.base = base
        self.text = text
        self.readings = readings
        self.cursor = 0

    def __call__(self, chunk):
        import cn2an

        start = self.text.find(chunk, self.cursor)
        if start < 0 or self.base.frontend is None:
            LOG.warning("无法定位配音分块，使用默认读音。")
            return self.base(chunk)
        self.cursor = start + len(chunk)
        readings = [
            reading.model_copy(update={"start": reading.start - start})
            for reading in self.readings
            if start <= reading.start < self.cursor
        ]
        if not readings:
            return self.base(chunk)

        def normalize(value):
            return self.base.map_punctuation(cn2an.transform(value, "an2cn"))

        positions = _normalized_readings(chunk, readings, normalize)
        if not positions:
            return self.base(chunk)
        normalized = normalize(chunk)
        cursor = 0

        def annotated_frontend(chinese):
            nonlocal cursor
            at = normalized.find(chinese, cursor)
            if at < 0:
                LOG.warning("无法定位中文配音片段，使用默认读音。")
                return self.base.frontend(chinese)
            cursor = at + len(chinese)
            selected = {
                position - at: reading for position, reading in positions.items()
                if at <= position < cursor
            }
            return _frontend(self.base.frontend, chinese, selected)

        local = copy(self.base)
        local.frontend = annotated_frontend
        return local(chunk)
