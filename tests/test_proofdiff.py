from app.proofdiff import compare


def test_no_changes():
    d = compare("Добрый день, всё в силе.", "Добрый день, всё в силе.")
    assert d.unchanged and d.changed_share == 0


def test_spelling_fix():
    d = compare("Счёт прийдёт завтра", "Счёт придёт завтра")
    assert d.changes == [("прийдёт", "придёт")]


def test_comma_shown_with_word_before():
    d = compare("Добрый день спасибо", "Добрый день, спасибо")
    assert d.changes == [("день", "день,")]


def test_share_flags_rewrite():
    d = compare("Пришлите пожалуйста счёт", "Будем рады получить от вас документы")
    assert d.looks_rewritten


def test_single_typo_in_short_text_is_not_rewrite():
    d = compare("Счёт прийдёт завтра", "Счёт придёт завтра")
    assert d.changed_share > 0.3 and not d.looks_rewritten
