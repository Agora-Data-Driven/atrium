"""Product-title -> product-category classifier for Honey Tribe.

The client's category taxonomy lives in one hand-curated sheet ("Honey Tribe Product Trend
Analysis"). That sheet names only ~240 of the ~580 titles that appear in the order history, but
its labels are driven by the GARMENT-TYPE WORD in the title ("... kimono" -> Outerwear,
"... palazzo" -> Bottoms - Pants). So the rule is LEARNED from the sheet rather than hand-copied,
frozen into `assets/categories.json`, and applied identically by the local builder and the live
export job — one taxonomy, two callers, no drift.

Order of resolution:
  1. exact title match from the sheet;
  2. the garment-type token — a token in >= 2 distinct titles AND >= 60% pure. Purity matters:
     "denim" spans jackets, skirts and palazzos, so it predicts nothing and is rejected here;
  3. `FALLBACK_TOKENS`, for type words the sheet happens not to contain;
  4. "Uncategorized" — a real bucket in the client's own taxonomy, not a failure state.

`learn()` is used at build time; `Classifier` is what both callers use at run time.
"""
import collections
import json
import os
import re

MIN_TITLES = 2      # a type word appears across several products; a product name appears once
MIN_PURITY = 0.6    # ...and predicts one category most of the time

# Garment-type words the trend sheet never happens to contain, mapped into ITS taxonomy.
FALLBACK_TOKENS = [
    (("choker", "necklace", "earring", "earrings", "ring", "bracelet", "anklet", "bangle",
      "pendant", "jewelry", "chain"), "Jewelry"),
    (("kimono", "duster", "blazer", "cardigan", "jacket", "coat", "cape", "poncho",
      "shrug", "vest"), "Outerwear / Layering"),
    (("jumpsuit", "romper", "playsuit", "catsuit"), "Jumpsuits / Rompers"),
    (("shorts",), "Bottoms - Shorts"),
    (("skirt", "skirts"), "Bottoms - Skirts"),
    (("denim", "jeans", "joggers", "chinos", "trousers", "pants", "palazzo", "leggings",
      "drawstring", "drawstrings", "harem"), "Bottoms - Pants"),
    (("dress", "maxidress", "gown", "tunic", "kaftan", "caftan"), "Dresses"),
    (("top", "blouse", "shirt", "tee", "crop", "bodysuit", "bralette", "cami", "corset",
      "camisole", "halter"), "Tops"),
    (("set", "sets", "two-piece", "2-piece", "co-ord"), "Two-Piece Sets"),
    (("bag", "hat", "scarf", "belt", "headwrap", "turban", "clutch", "purse", "sunglasses",
      "mask", "gloves", "socks"), "Accessories"),
]

# Buckets that carry no garment signal — never learned FROM, still assignable by exact match.
_NON_SIGNAL = ("Uncategorized", "Non-Product / Shipping", "Gift Card")

_STOP = {"in", "the", "of", "and", "a", "with", "&", "-", "to"}

# Line items that are not merchandise at all.
NON_PRODUCT = re.compile(r"route|shipping protection|package protection|gift card|tip|donation", re.I)

_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "categories.json")


def tokens(title):
    return [t for t in re.split(r"[^\w']+", str(title).lower()) if t and t not in _STOP]


def learn(rows):
    """Build the model from trend rows: dicts with `t` (title), `cat`, and `u` (units)."""
    exact = collections.defaultdict(collections.Counter)
    tok_cats = collections.defaultdict(collections.Counter)
    tok_titles = collections.defaultdict(set)
    for r in rows:
        title = str(r["t"]).lower().strip()
        exact[title][r["cat"]] += r.get("u") or 1
        if r["cat"] in _NON_SIGNAL:
            continue
        for tok in tokens(title):
            tok_cats[tok][r["cat"]] += r.get("u") or 1
            tok_titles[tok].add(title)

    learned = {}
    for tok, c in tok_cats.items():
        if len(tok_titles[tok]) < MIN_TITLES:
            continue
        top, n = c.most_common(1)[0]
        if n / float(sum(c.values())) >= MIN_PURITY:
            learned[tok] = top

    return {
        "exact": {k: v.most_common(1)[0][0] for k, v in exact.items()},
        "tokens": learned,
    }


def save(model, path=None):
    path = path or _MODEL_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1, sort_keys=True, ensure_ascii=False)
    return path


def load(path=None):
    path = path or _MODEL_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {"exact": {}, "tokens": {}}


class Classifier(object):
    def __init__(self, model=None):
        model = model if model is not None else load()
        self.exact = model.get("exact") or {}
        self.tokens = model.get("tokens") or {}

    def classify(self, title):
        key = str(title).lower().strip()
        hit = self.exact.get(key)
        if hit and hit != "Uncategorized":
            return hit
        toks = tokens(key)
        # Prefer the LAST matching token — "vi dress in dark denim" is a dress, not a bottom.
        for tok in reversed(toks):
            if tok in self.tokens:
                return self.tokens[tok]
        for tok in reversed(toks):
            for words, cat in FALLBACK_TOKENS:
                if tok in words:
                    return cat
        return hit or "Uncategorized"
