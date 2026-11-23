"""UI translations must be complete: same keys in every language, no empty text."""

from baton.i18n import STRINGS

# The one allowed empty string: "es" leaves "ago" empty because quotas.html
# renders "{{ t.seen }} {{ age }} {{ t.ago }}" and Spanish already puts
# "hace" before the time ("lectura de hace 2h"), so no word follows it.
INTENTIONALLY_EMPTY = {("es", "ago")}


def test_same_keys_in_every_language():
    en = set(STRINGS["en"])
    for lang, table in STRINGS.items():
        missing = sorted(en - set(table))
        assert not missing, f"{lang}: keys missing (present in en): {missing}"
        extra = sorted(set(table) - en)
        assert not extra, f"{lang}: keys not present in en: {extra}"


def test_no_empty_text():
    empty = [
        f"{lang}.{key}"
        for lang, table in STRINGS.items()
        for key, text in table.items()
        if not text.strip() and (lang, key) not in INTENTIONALLY_EMPTY
    ]
    assert not empty, (
        "empty UI strings (translate them, or add them to INTENTIONALLY_EMPTY"
        " with a reason): " + ", ".join(empty)
    )
