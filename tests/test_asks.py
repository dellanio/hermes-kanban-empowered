from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)

from ke.asks import Ask, ask_template, parse_ask


def test_parse_ask_pt():
    a = parse_ask("Preciso: ligar a placa no USB\nPor quê: o teste só roda com ela\nComo responder: responda feito")
    assert isinstance(a, Ask)
    assert a.need.startswith("ligar") and a.reply == "responda feito"
    assert a.why == "o teste só roda com ela"


def test_parse_ask_english_labels_and_case_insensitive():
    a = parse_ask("need: plug the board\nWHY: test needs it\nreply: say done")
    assert (a.need, a.why, a.reply) == ("plug the board", "test needs it", "say done")


def test_parse_ask_porque_variant_and_crlf_and_surrounding_text():
    text = "Contexto livre\r\nPreciso: x\r\n  Porque: y\r\nComo responder: z\r\nfim"
    a = parse_ask(text)
    assert (a.need, a.why, a.reply) == ("x", "y", "z")


def test_parse_ask_missing_field_is_none():
    assert parse_ask("Preciso: x\nPor quê: y") is None


def test_parse_ask_empty_value_is_none():
    assert parse_ask("Preciso:\nPor quê: y\nComo responder: z") is None
    assert parse_ask("Preciso:   \nPor quê: y\nComo responder: z") is None


def test_parse_ask_non_string_is_none():
    assert parse_ask(None) is None
    assert parse_ask(42) is None
    assert parse_ask("") is None


def test_template_shows_three_labeled_lines():
    template = ask_template()
    for label in ("Need:", "Why:", "How to reply:"):
        assert label in template


def test_parse_ask_accepts_the_english_how_to_reply_label():
    assert parse_ask("Need: a\nWhy: b\nHow to reply: c") == Ask("a", "b", "c")
    assert parse_ask("Need: a\nWhy: b\nReply: c") == Ask("a", "b", "c")  # the short label still works


def test_the_template_in_every_language_is_parseable(pt_br):  # noqa: F811
    from ke import messages

    for template in (messages.EN["guard.template"], ask_template()):
        filled = "\n".join(line.split("<")[0] + "x" for line in template.splitlines()[1:])
        assert parse_ask(filled) == Ask("x", "x", "x"), template
