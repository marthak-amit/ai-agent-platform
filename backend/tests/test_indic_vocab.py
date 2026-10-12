"""Script-safe tokenizer and native-script product/colour aliases."""

from app.services import indic_vocab as iv
from app.services.catalogue_service import search_products_with_scores


def test_tokenize_keeps_indic_words_whole():
    """Vowel signs stay inside the word; punctuation, hyphens and emoji separate words."""
    assert iv.tokenize("ચણિયા-ચોળી પણ વેચી રહ્યા છો?") == ["ચણિયા", "ચોળી", "પણ", "વેચી", "રહ્યા", "છો"]
    assert iv.tokenize("साड़ी 😊 चाहिए") == ["साड़ी", "चाहिए"]


def test_user_example_expands_to_chaniya_choli():
    """The customer's own example sentence maps to the English catalogue word."""
    assert iv.expand_query("શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?") == ["chaniya choli"]


def test_expand_query_other_scripts_and_roman_spellings():
    """Saree / kurti in several scripts and Roman spellings, plus colours."""
    assert iv.expand_query("तुमच्याकडे साडी आहे का") == ["saree"]
    assert iv.expand_query("இந்த புடவை விலை") == ["saree"]
    assert iv.expand_query("lal sadi chahiye") == ["saree", "red"]
    assert iv.expand_query("hello") == []


def test_canonical_color():
    """Colour words in native and Roman spellings resolve to one English name."""
    assert iv.canonical_color("ગુલાબી") == "pink"
    assert iv.canonical_color("gulabi wala") == "pink"
    assert iv.canonical_color("नीला") == "blue"
    assert iv.canonical_color("table") is None


class _P:
    """Minimal product stand-in for the search functions."""

    def __init__(self, sku, name, category, description=""):
        self.sku, self.name, self.category, self.description = sku, name, category, description
        self.is_active, self.price, self.has_variants, self.variants, self.stock = True, 100.0, False, [], 5


def test_native_script_query_finds_english_named_products():
    """A Gujarati query used to tokenise to nothing and match arbitrary products; now it finds the right one."""
    products = [_P("A1", "Green Cotton Saree", "Saree"), _P("K1", "Printed Kurti", "Kurti"), _P("C1", "Navratri Chaniya Choli", "Chaniya Choli")]
    scored = search_products_with_scores(products, "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?")
    assert scored and scored[0][1].sku == "C1"
    assert search_products_with_scores(products, "मुझे साड़ी चाहिए")[0][1].sku == "A1"


def test_color_aliases_lists_roman_and_native_spellings():
    """color_aliases('red') includes Roman 'lal' and Devanagari 'लाल', all mapping back to 'red'."""
    pairs = dict(iv.color_aliases("red"))
    assert pairs.get("lal") == "red" and pairs.get("लाल") == "red"
    assert iv.color_aliases("not-a-colour") == []


def test_variant_breakdown_accepts_roman_colour_aliases():
    """'2 neela M and 1 gulabi XXL' parses into Blue/Pink lines; a lone 'neela' is not a split; 'black' is invalid."""
    from app.services import conversation_flow as cf

    vi = {"available_colors": ["Blue", "Pink"], "available_sizes": ["M", "XXL"], "needs_color": True, "needs_size": True}
    assert cf.parse_variant_breakdown("neela", vi) is None
    parsed = cf.parse_variant_breakdown("2 neela M and 1 gulabi XXL", vi)
    assert [(i["color"], i["size"], i["qty"]) for i in parsed.items] == [("Blue", "M", 2), ("Pink", "XXL", 1)]
    assert cf.parse_variant_breakdown("2 black M and 1 XXL", vi).invalid == ["Black"]
