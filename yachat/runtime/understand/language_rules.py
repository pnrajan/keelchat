"""Language rules shared by the language pre-processor and tests."""

from __future__ import annotations

import difflib
import re

from yachat.capabilities.nodes.common import NO, YES

MARKERS: dict[str, set[str]] = {
    "en": {
        "i",
        "the",
        "is",
        "are",
        "want",
        "looking",
        "for",
        "job",
        "my",
        "name",
        "hello",
        "hi",
        "please",
        "and",
        "of",
        "to",
        "what",
        "about",
        "do",
        "you",
        "can",
        "not",
        "benefits",
        "thanks",
        "which",
        "closer",
        "more",
        "pays",
        "start",
        "monday",
        "have",
        "work",
        "tomorrow",
        "near",
        "me",
        "actually",
        "meant",
        "address",
    },
    "es": {
        "hola",
        "busco",
        "trabajo",
        "el",
        "la",
        "que",
        "es",
        "una",
        "por",
        "para",
        "con",
        "gracias",
        "quiero",
        "mi",
        "me",
        "soy",
        "dirección",
        "nombre",
        "empleo",
        "puesto",
        "tienen",
        "ofrecen",
        "vivo",
        "calle",
        "puedo",
        "seguir",
        "español",
        "enfermera",
        "cuál",
        "más",
        "cerca",
        "perdón",
    },
    "fr": {
        "bonjour",
        "je",
        "cherche",
        "emploi",
        "les",
        "et",
        "est",
        "vous",
        "nous",
        "merci",
        "oui",
        "à",
        "du",
        "pour",
        "suis",
        "mon",
        "adresse",
        "travail",
        "voudrais",
        "avez",
        "un",
    },
    "de": {"ich", "suche", "arbeit", "der", "die", "das", "und", "ist", "nicht", "bitte", "danke", "ja", "nein", "mein", "stelle"},
    "pt": {"olá", "procuro", "emprego", "trabalho", "os", "as", "é", "não", "obrigado", "sim", "meu", "nome", "morada"},
}
NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese"}
LANG_WORDS = {
    "english": "en",
    "inglés": "en",
    "ingles": "en",
    "spanish": "es",
    "español": "es",
    "espanol": "es",
    "french": "fr",
    "francés": "fr",
    "german": "de",
    "alemán": "de",
    "portuguese": "pt",
    "portugués": "pt",
}
SWITCH_PATTERNS = (
    r"\b(?:seguir|hablar|continuar|cambiar)\s+(?:en|a|al)\s+(\w+)",
    r"\b(?:switch|continue|speak|talk|go on)\s+(?:to|in)\s+(\w+)",
    r"^(?:en|in)\s+(\w+)(?:,?\s*(?:por favor|please))?\s*[?.!]*$",
)
UNSUPPORTED = {
    "en": "Sorry, I can only help in {langs}.",
    "es": "Lo siento, solo puedo ayudar en {langs}.",
    "fr": "Désolé, je ne peux aider qu'en {langs}.",
    "de": "Entschuldigung, ich kann nur auf {langs} helfen.",
    "pt": "Desculpe, só posso ajudar em {langs}.",
}
CONFIRM = {
    "en": "Shall we continue in English? (yes/no)",
    "es": "¿Seguimos en español? (sí/no)",
    "fr": "On continue en français ? (oui/non)",
    "de": "Sollen wir auf Deutsch weitermachen? (ja/nein)",
    "pt": "Continuamos em português? (sim/não)",
}
SWITCHED = {
    "en": "Okay, continuing in English.",
    "es": "Perfecto, seguimos en español.",
    "fr": "D'accord, on continue en français.",
    "de": "Gut, weiter auf Deutsch.",
    "pt": "Certo, continuamos em português.",
}
KEPT = {"en": "Okay, we'll stay in {lang}.", "es": "De acuerdo, seguimos en {lang}."}
NO_EVIDENCE_FIELDS = ("name", "address", "dirección", "nombre")


def _words(text: str) -> list[str]:
    return re.findall(r"[\wáéíóúñüàèìòùçâêîôû]+", text.lower())


def scores(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for lang, markers in MARKERS.items():
        n = 0
        for w in _words(text):
            if w in markers or (len(w) > 4 and difflib.get_close_matches(w, markers, n=1, cutoff=0.85)):
                n += 1
        out[lang] = n
    return out


def is_bare_reply(text: str) -> bool:
    t = text.lower().strip(" .!¡¿?")
    return t in YES or t in NO or len(t.split()) <= 2


def evidence(text: str, current: str, open_field: str | None) -> str | None:
    """A language different from `current` this message confidently supports, or None (no evidence / mixed / bare)."""
    if is_bare_reply(text) or (open_field and any(k in open_field for k in NO_EVIDENCE_FIELDS)):
        return None
    sc = scores(text)
    best = max(sc, key=lambda k: sc[k])
    if sc[best] < 2 or best == current:
        return None
    if sc.get(current, 0) >= 1:
        return None  # mixed message keeps the session language
    return best


def explicit_request(text: str) -> str | None:
    low = text.lower()
    for pat in SWITCH_PATTERNS:
        m = re.search(pat, low)
        if m and m.group(1) in LANG_WORDS:
            return LANG_WORDS[m.group(1)]
    return None


def unsupported_reply(reply_lang: str, supported: list[str]) -> str:
    langs = ", ".join(NAMES.get(x, x) for x in supported)
    return UNSUPPORTED.get(reply_lang, UNSUPPORTED["en"]).format(langs=langs)
