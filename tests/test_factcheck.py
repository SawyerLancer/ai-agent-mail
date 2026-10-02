from app.factcheck import added, extract


def keys(text, kind):
    return [f.key for f in extract(text) if f.kind == kind]


# --- суммы ---

def test_money_with_spaces_and_rub():
    assert keys("Счёт на 150 000 ₽", "money") == keys("Счёт на 150000 руб.", "money")


def test_money_nbsp_and_narrow_nbsp():
    assert added("Итого 150 000 ₽", ["сумма 150 000 рублей"]) == []


def test_money_thousands_word():
    assert added("бюджет 15 тыс. руб.", ["бюджет 15 000"]) == []


def test_money_million_decimal_comma():
    assert added("около 1,5 млн ₽", ["1 500 000 рублей"]) == []


def test_money_added_is_reported_as_written():
    assert added("Стоимость 20 000 ₽, как договаривались", ["Стоимость 15 000 ₽"]) == ["20 000"]


def test_list_numbering_is_not_a_fact():
    assert added("1. Подписать договор\n2. Оплатить", ["подпишите и оплатите"]) == []


# --- даты ---

def test_date_dotted_full_and_short_year():
    assert added("до 12.03.26", ["срок 12.03.2026"]) == []


def test_date_iso_vs_text():
    assert added("встреча 2026-03-12", ["встретимся 12 марта"]) == []


def test_date_text_with_ordinal_and_year():
    assert keys("к 5-го мая 2026 года", "date") == [(5, 5, 2026)]


def test_date_year_checked_only_if_in_both():
    assert added("12.03", ["12.03.2026"]) == []
    assert added("12.03.2027", ["12.03.2026"]) == ["12.03.2027"]


def test_date_added():
    assert added("Пришлём 14 марта", ["Пришлите на этой неделе"]) == ["14 марта"]


def test_decimal_dot_is_not_a_date():
    assert keys("рост 1.5 раза", "date") == []


def test_date_slash():
    assert keys("до 01/04/2026", "date") == [(1, 4, 2026)]


def test_year_from_source_date_is_known():
    assert added("в 2026 году", ["договор от 12.03.2026"]) == []


# --- время ---

def test_time_formats():
    assert keys("в 14:30", "time") == [(14, 30)]
    assert keys("в 14.30 ч", "time") == [(14, 30)]
    assert keys("в 9 часов", "time") == [(9, 0)]


def test_time_added():
    assert added("созвонимся в 15:00", ["созвонимся в 14:00"]) == ["15:00"]


# --- e-mail и URL ---

def test_email_case_insensitive():
    assert added("пишите на Ivan@Example.ru", ["ivan@example.ru"]) == []


def test_email_added():
    assert added("копия на boss@example.ru", ["ivan@example.ru"]) == ["boss@example.ru"]


def test_url_trailing_punct():
    assert added("Ссылка: https://example.ru/doc.", ["https://example.ru/doc"]) == []


def test_url_added():
    assert added("Оплата тут: https://pay.evil.ru/x", ["пришлите счёт"]) == ["https://pay.evil.ru/x"]


def test_no_sources():
    assert added("Добрый день! Спасибо.", []) == []
