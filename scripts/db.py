#!/usr/bin/env python3
"""
db.py — maintenance CLI for the "safe to put on?" databases.

WHY THIS EXISTS
  database/ingredients.json (~350k tokens) and database/products.json (~175k
  tokens) are far too large to read into an assistant's context. NEVER open them
  with a Read tool. This CLI does every routine operation by streaming the files
  through Python and printing only small summaries, so the heavy data never
  enters context.

The matcher here is a faithful port of the one in index.html (norm + clean +
the >=75 score threshold = exact / alias / name-startswith / alias-startswith).
Keep the two in sync if either changes.

USAGE
  python scripts/db.py verify [--all] [FILE ...]
      --all      structural + unknown-rate check over the whole live database
      FILE ...   same checks for one or more brand product files (array of
                 {name,brand,category,ingredients})

  python scripts/db.py normalize FILE
      In place: split combined colorant tokens ("Iron Oxides (CI 77491, CI
      77492)" -> "Iron Oxides","CI 77491","CI 77492") and reduce multilingual
      synonym tokens to a known segment. Idempotent. Run before diffing/merging.

  python scripts/db.py new-ingredients FILE
      Print every ingredient token in FILE that is NOT yet known to the
      database, deduped with counts, and write the token list to
      FILE.newings.json (feed this to the classifier).

  python scripts/db.py add-ingredients ENTRIES.json [--version X.Y]
      Validate + dedupe classified ingredient entries and append them to
      database/ingredients.json (text-append, clean additive diff). Updates the
      meta "updated" date; bumps meta "version" if --version is given.

  python scripts/db.py add-products FILE [--allow-existing-brand]
      Validate + dedupe brand products and append them to
      database/products.json. Refuses a brand already present unless
      --allow-existing-brand. Updates the meta "updated" date.

TYPICAL "add a brand" FLOW (none of these load the DB into your context):
  1. research agent writes scratchpad/brand_x.json
  2. python scripts/db.py normalize        scratchpad/brand_x.json
  3. python scripts/db.py new-ingredients  scratchpad/brand_x.json   # -> worklist
  4. classifier writes scratchpad/x_out.json  (tier/category/note per entry)
  5. python scripts/db.py add-ingredients  scratchpad/x_out.json
  6. python scripts/db.py verify           scratchpad/brand_x.json   # expect ~0%
  7. python scripts/db.py add-products     scratchpad/brand_x.json
  8. python scripts/db.py verify --all
"""
import argparse, json, os, re, sys, datetime, bisect, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ING_PATH = os.path.join(ROOT, "database", "ingredients.json")
PROD_PATH = os.path.join(ROOT, "database", "products.json")

ING_KEYS = ["name", "tier", "precautionary", "category", "aliases", "note", "sources"]
PROD_REQUIRED = {"name", "brand", "category", "ingredients"}
VALID_CATEGORIES = {
    "Botanical / essential oil", "Emulsifier / texture", "Emollient / oil",
    "Active / treatment", "Humectant", "Surfactant / cleanser",
    "Colorant / pigment", "Antioxidant / vitamin", "Fragrance / allergen",
    "Preservative", "Hair / nail agent", "Solvent / propellant",
    "Drug / hormone", "UV filter", "Toxic metal / contaminant",
}

# ---------------------------------------------------------------- matcher port
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

def clean_tokens(ingredients):
    """Port of scoreProduct()'s token cleaning in index.html."""
    out = []
    for t in (ingredients or []):
        t = str(t)
        t = re.sub(r"^\s*active\s*:?", "", t, flags=re.I)
        t = re.sub(r"\b\d+(\.\d+)?\s*%", " ", t)
        t = re.sub(r"[()*\[\].]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 1:
            out.append(t)
    return out

class Matcher:
    """Replicates bestMatch(): a token is 'known' iff some ingredient name or
    alias equals it or starts with it (normalized)."""
    def __init__(self, ingredients):
        keys = set()
        for i in ingredients:
            keys.add(norm(i["name"]))
            for a in i.get("aliases", []):
                keys.add(norm(a))
        self.exact = keys
        self.sorted = sorted(keys)

    def known(self, token):
        nq = norm(token)
        if not nq:
            return True
        if nq in self.exact:
            return True
        idx = bisect.bisect_left(self.sorted, nq)  # first key >= nq starts-with nq if any does
        return idx < len(self.sorted) and self.sorted[idx].startswith(nq)

# ----------------------------------------------------- ingredient-list cleanup
_CI = re.compile(r"[Cc][Ii]\s*\d{4,5}")

def _split_colorant(token):
    t = str(token).strip()
    codes = _CI.findall(t)
    if codes:
        name = _CI.sub("", t)
        name = re.sub(r"[()\[\]/,]", " ", name)
        name = re.sub(r"\s+", " ", name).strip(" ,/")
        res = []
        if name and re.search(r"[A-Za-z]{2,}", name):
            res.append(name)
        res += ["CI " + re.sub(r"\D", "", c) for c in codes]
        return res
    m = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", t)
    if m:
        return [m.group(1).strip(), m.group(2).strip()]
    return [t]

def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        x = x.strip()
        if x and x.lower() not in seen:
            seen.add(x.lower()); out.append(x)
    return out

def normalize_ingredients(ings, matcher):
    # 1) split combined colorant / parenthetical-synonym tokens
    step = []
    for t in ings:
        step += _split_colorant(t)
    step = _dedupe(step)
    # 2) reduce multilingual synonym tokens to a known segment
    out = []
    for t in step:
        if matcher.known(t):
            out.append(t); continue
        if "/" in t or "," in t:
            segs = [s.strip() for s in re.split(r"[/,]", t) if s.strip()]
            kept = [s for s in segs if matcher.known(s)]
            if kept:
                out += kept; continue
        out.append(t)
    return _dedupe(out)

# --------------------------------------------------------------------- io util
def load(path):
    with open(path) as f:
        return json.load(f)

def text_append_array(path, json_lines, anchor="\n  ]\n}"):
    """Insert pre-rendered one-per-line JSON objects before the array close,
    matching the existing one-object-per-line, ensure_ascii=True style."""
    txt = open(path).read()
    idx = txt.rfind(anchor)
    if idx == -1:
        raise SystemExit(f"could not find array anchor in {path}")
    block = ",\n" + ",\n".join(json_lines)
    return txt[:idx] + block + txt[idx:]

def today():
    return datetime.date.today().isoformat()

# ------------------------------------------------------------------- commands
def cmd_verify(args):
    matcher = Matcher(load(ING_PATH)["ingredients"])
    targets = []
    if args.all:
        targets.append(("database/products.json", load(PROD_PATH)["products"]))
    for f in args.files:
        targets.append((f, load(f)))
    if not targets:
        raise SystemExit("nothing to verify: pass --all or one or more FILE")

    rc = 0
    for label, products in targets:
        issues, names = [], collections.Counter()
        tot = unk = 0
        unk_tokens = collections.Counter()
        for p in products:
            if not PROD_REQUIRED <= set(p.keys()):
                issues.append(f"missing keys: {p.get('name')!r}")
            if not isinstance(p.get("ingredients"), list) or len(p.get("ingredients", [])) < 1:
                issues.append(f"empty/invalid ingredients: {p.get('name')!r}")
            names[(p.get("brand"), str(p.get("name")).lower())] += 1
            for tk in clean_tokens(p.get("ingredients")):
                tot += 1
                if not matcher.known(tk):
                    unk += 1; unk_tokens[tk] += 1
        dups = [k for k, c in names.items() if c > 1]
        rate = (unk / tot * 100) if tot else 0.0
        print(f"[{label}] products={len(products)} unknown={unk}/{tot} ({rate:.2f}%) "
              f"dups={len(dups)} issues={len(issues)}")
        for d in dups[:10]:
            print(f"    DUP {d}")
        for i in issues[:10]:
            print(f"    ISSUE {i}")
        if unk_tokens and not args.all:
            for t, c in unk_tokens.most_common(15):
                print(f"    unknown x{c}: {t}")
        if issues or dups or rate > 6:
            rc = 1
    # ingredient DB self-check
    if args.all:
        ing = load(ING_PATH)["ingredients"]
        bad = [i.get("name") for i in ing if set(i.keys()) != set(ING_KEYS)]
        dupi = [n for n, c in collections.Counter(i["name"].lower() for i in ing).items() if c > 1]
        print(f"[database/ingredients.json] entries={len(ing)} schema_bad={len(bad)} dup_names={len(dupi)}")
        if bad or dupi:
            rc = 1
    return rc

def cmd_normalize(args):
    matcher = Matcher(load(ING_PATH)["ingredients"])
    data = load(args.file)
    for p in data:
        p["ingredients"] = normalize_ingredients(p["ingredients"], matcher)
    json.dump(data, open(args.file, "w"), ensure_ascii=False, indent=0)
    print(f"normalized {len(data)} products in {args.file}")
    return 0

def cmd_new_ingredients(args):
    matcher = Matcher(load(ING_PATH)["ingredients"])
    data = load(args.file)
    unk = collections.Counter()
    for p in data:
        view = normalize_ingredients(p["ingredients"], matcher)
        for tk in clean_tokens(view):
            if not matcher.known(tk):
                unk[tk] += 1
    out = args.file + ".newings.json"
    json.dump([t for t, _ in unk.most_common()], open(out, "w"), ensure_ascii=False, indent=1)
    print(f"{len(unk)} new ingredient tokens (worklist -> {out})")
    for t, c in unk.most_common(40):
        print(f"  x{c}  {t}")
    return 0

def cmd_add_ingredients(args):
    db = load(ING_PATH)
    existing = set()
    for i in db["ingredients"]:
        existing.add(norm(i["name"]))
        for a in i.get("aliases", []):
            existing.add(norm(a))
    entries = load(args.entries)
    added, seen, skipped, problems = [], set(), 0, []
    for e in entries:
        if set(e.keys()) != set(ING_KEYS):
            problems.append(f"keys {e.get('name')!r}"); continue
        if e["tier"] not in (1, 2, 3):
            problems.append(f"tier {e.get('name')!r}={e['tier']}")
        if e["category"] not in VALID_CATEGORIES:
            problems.append(f"category {e.get('name')!r}={e['category']!r}")
        nn = norm(e["name"])
        if nn in existing or nn in seen:
            skipped += 1; continue
        seen.add(nn)
        e = {k: e[k] for k in ING_KEYS}
        e["tier"] = int(e["tier"]); e["precautionary"] = bool(e["precautionary"])
        added.append(e)
    if problems:
        print("VALIDATION PROBLEMS (nothing written):")
        for p in problems[:30]:
            print("   ", p)
        return 1
    before = len(db["ingredients"])
    lines = ["    " + json.dumps(e, ensure_ascii=True) for e in added]
    txt = text_append_array(ING_PATH, lines)
    txt = re.sub(r'("updated":\s*")[^"]+(")', r"\g<1>" + today() + r"\2", txt, count=1)
    if args.version:
        txt = re.sub(r'("version":\s*")[^"]+(")', r"\g<1>" + args.version + r"\2", txt, count=1)
    parsed = json.loads(txt)
    assert len(parsed["ingredients"]) == before + len(added)
    open(ING_PATH, "w").write(txt)
    dist = collections.Counter(e["tier"] for e in added)
    print(f"ingredients.json: {before} -> {len(parsed['ingredients'])} "
          f"(+{len(added)}, skipped {skipped}) tiers={dict(dist)} "
          f"version={parsed['meta']['version']} updated={parsed['meta']['updated']}")
    return 0

def cmd_add_products(args):
    db = load(PROD_PATH)
    existing_keys = {(p["brand"], p["name"].lower()) for p in db["products"]}
    existing_brands = {p["brand"] for p in db["products"]}
    new = load(args.file)
    brands = {p["brand"] for p in new}
    clash = brands & existing_brands
    if clash and not args.allow_existing_brand:
        raise SystemExit(f"brand already present: {sorted(clash)} (use --allow-existing-brand)")
    added, skipped, problems = [], 0, []
    for p in new:
        if not PROD_REQUIRED <= set(p.keys()):
            problems.append(f"keys {p.get('name')!r}"); continue
        if not isinstance(p["ingredients"], list) or len(p["ingredients"]) < 1:
            problems.append(f"empty ingredients {p.get('name')!r}")
        k = (p["brand"], p["name"].lower())
        if k in existing_keys:
            skipped += 1; continue
        existing_keys.add(k)
        added.append({k2: p[k2] for k2 in ("name", "brand", "category", "ingredients")})
    if problems:
        print("VALIDATION PROBLEMS (nothing written):")
        for p in problems[:30]:
            print("   ", p)
        return 1
    added.sort(key=lambda p: (p["brand"].lower(), p["name"].lower()))
    before = len(db["products"])
    lines = ["    " + json.dumps(p, ensure_ascii=True) for p in added]
    txt = text_append_array(PROD_PATH, lines)
    txt = re.sub(r'("updated":\s*")[^"]+(")', r"\g<1>" + today() + r"\2", txt, count=1)
    parsed = json.loads(txt)
    assert len(parsed["products"]) == before + len(added)
    open(PROD_PATH, "w").write(txt)
    print(f"products.json: {before} -> {len(parsed['products'])} (+{len(added)}, skipped {skipped}) "
          f"brands={len({p['brand'] for p in parsed['products']})}")
    return 0

def main():
    ap = argparse.ArgumentParser(description="safe-to-put-on database maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify"); v.add_argument("--all", action="store_true"); v.add_argument("files", nargs="*"); v.set_defaults(fn=cmd_verify)
    n = sub.add_parser("normalize"); n.add_argument("file"); n.set_defaults(fn=cmd_normalize)
    g = sub.add_parser("new-ingredients"); g.add_argument("file"); g.set_defaults(fn=cmd_new_ingredients)
    a = sub.add_parser("add-ingredients"); a.add_argument("entries"); a.add_argument("--version"); a.set_defaults(fn=cmd_add_ingredients)
    p = sub.add_parser("add-products"); p.add_argument("file"); p.add_argument("--allow-existing-brand", action="store_true"); p.set_defaults(fn=cmd_add_products)
    args = ap.parse_args()
    sys.exit(args.fn(args))

if __name__ == "__main__":
    main()
