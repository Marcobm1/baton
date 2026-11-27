"""UI translations must be complete: same keys in every language, no empty text."""

from baton.i18n import STRINGS, strings

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


def test_no_hardcoded_visible_baton_and_dynamic_app_title():
    # Visible prose uses {app} so BATON_TITLE (default "Huo") updates every page;
    # uppercase BATON_* env var names stay intact.
    hardcoded = [
        f"{lang}.{key}"
        for lang, table in STRINGS.items()
        for key, text in table.items()
        if "Baton" in text
    ]
    assert not hardcoded, f"hardcoded 'Baton' in UI strings (use {{app}}): {hardcoded}"

    es_default = strings("es")
    en_default = strings("en")
    assert es_default["auth_heading"] == "Desbloquear Huo"
    assert en_default["auth_heading"] == "Unlock Huo"
    assert "BATON_ACCESS_KEY" in es_default["auth_hint"]
    assert "{port}" in es_default["m_err_not_ours"] and "Huo" in es_default["m_err_not_ours"]
    assert "%s" in es_default["from_project"] and "Huo" in es_default["from_project"]

    custom = strings("es", "MiHub")
    assert custom["restart_now"] == "Reiniciar MiHub"
    assert custom["chat_empty"].endswith("entre PC y móvil.") and "MiHub" in custom["chat_empty"]
    # Brace characters in BATON_TITLE must not break substitution.
    weird = strings("en", "{Custom}")
    assert weird["auth_heading"] == "Unlock {Custom}"
