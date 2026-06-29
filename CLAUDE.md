# safe to put on? — maintainer guide

A static, single-page web app (`index.html`, no build step, hosted on GitHub
Pages) that lets someone check how worried to be about a skincare/makeup
ingredient, or look up a whole product and see its ingredients scored by safety
tier. Data lives in two JSON files under `database/`.

## ⛔ The one rule that controls cost

**Never open `database/ingredients.json` or `database/products.json` with a
Read tool.** They are a couple hundred thousand tokens each — reading either
blows the context window and burns huge usage for no reason.

Everything routine is done through **`scripts/db.py`**, which streams the files
through Python and prints only small summaries. For anything ad-hoc, write a
short `python3 - <<'PY' … PY` snippet in Bash that loads the file, does the
work, and prints a few lines — never echo the file contents.

The expensive part of "add a brand" is the **web research**, not the database
size. The DB could be 10× bigger and a brand-add would cost the same.

## Repo layout

- `index.html` — the entire app (HTML + CSS + JS inline). Responsive; uses
  `clamp()` and media queries at 760px (desktop) and 400px (small phone).
- `database/ingredients.json` — `{meta, ingredients:[…]}`, one object per line.
- `database/products.json` — `{meta, products:[…]}`, one object per line.
- `scripts/db.py` — maintenance CLI (verify / normalize / new-ingredients /
  add-ingredients / add-products). Run `python3 scripts/db.py -h`.
- `assets/` — PWA icons + manifest.

## Data model

**Product** (`products.json`): exactly `{name, brand, category, ingredients}`
where `ingredients` is an array of INCI strings in label order. (A few have an
optional `barcode`.) One entry per distinct *formula* — multi-shade makeup is
collapsed to one representative product, not one per shade.

**Ingredient** (`ingredients.json`): exactly
`{name, tier, precautionary, category, aliases, note, sources}`.
- `tier`: `1` Stop/Check (drug-class active or toxic metal), `2` Worth Reducing
  (endocrine-disruptor or sensitizer concern), `3` All Clear (benign/beneficial).
- `precautionary`: `true` when evidence is uncertain/mixed (policy: sort to the
  **more severe** tier and mark precautionary).
- `category`: one of exactly 15 — Botanical / essential oil · Emulsifier /
  texture · Emollient / oil · Active / treatment · Humectant · Surfactant /
  cleanser · Colorant / pigment · Antioxidant / vitamin · Fragrance / allergen ·
  Preservative · Hair / nail agent · Solvent / propellant · Drug / hormone ·
  UV filter · Toxic metal / contaminant.
- `sources`: list of `{label, url}`. For bulk-added ingredients we use a
  resolvable PubChem search URL: `https://pubchem.ncbi.nlm.nih.gov/#query=NAME`.

## How the matcher works (keep `index.html` and `scripts/db.py` in sync)

`index.html` scores a product by matching each ingredient token to the DB:
`norm(s)` = lowercase + non-alphanumerics→space + trim. A token "matches" an
ingredient iff its normalized name **or** an alias **equals** the token or
**starts with** it (score ≥ 75). Tokens are cleaned first: drop an `Active:`
prefix, drop `N%` percentages, strip `()*[].`. Unmatched tokens render as
"not in our database." `scripts/db.py` ports this exactly — if you change one,
change the other.

Because `norm` strips parentheses, store colorants as **separate** tokens
("Titanium Dioxide", "CI 77891") rather than one combined string; `db.py
normalize` does this split automatically.

## Adding a brand (the standard flow)

None of these load the DB into context.

1. Spawn a research subagent (general-purpose) to pull the brand's current
   lineup with INCI lists and **write** `scratchpad/brand_<x>.json` (array of
   `{name, brand, category, ingredients}`). Tell it: one entry per formula,
   accuracy-first (omit a product rather than fabricate an unverifiable list),
   split colorants into name + CI code, no "May Contain"/"+/-" literals. Have it
   return only a summary, not the JSON.
2. `python3 scripts/db.py normalize        scratchpad/brand_<x>.json`
3. `python3 scripts/db.py new-ingredients  scratchpad/brand_<x>.json`
   → writes `…newings.json`, the list of ingredients not yet in the DB.
4. Spawn classifier subagent(s) over that worklist → `scratchpad/<x>_out.json`,
   one `{name,tier,precautionary,category,aliases,note,sources}` per token. Use
   the conservative rubric below. No web needed — these are standard INCI names.
   **Review any tier-1 it produces**: tier-1 is reserved for genuine drug
   actives / toxic metals. Cross-check against how the DB already tiers a
   sibling (e.g. metal-powder colorants → tier-3 like `Bronze Powder`;
   salicylate salts → tier-2 like `Sodium Salicylate`).
5. `python3 scripts/db.py add-ingredients  scratchpad/<x>_out.json --version N.N`
6. `python3 scripts/db.py verify           scratchpad/brand_<x>.json` (expect ~0%)
7. `python3 scripts/db.py add-products     scratchpad/brand_<x>.json`
8. `python3 scripts/db.py verify --all`, then commit + merge.

### Classifier tier rubric (conservative)

- **Tier 1** — prescription/OTC drug-class actives, hormones, toxic metals/
  contaminants. Rare. Verify before trusting.
- **Tier 2** — sensitizer/allergen or endocrine concern: fragrance allergens,
  essential oils, many botanical/flower extracts, chemical UV filters, sulfites,
  ethanolamines, some preservatives, quaternary ammonium compounds.
- **Tier 3** — benign: silicones, esters/synthetic emollients, non-sensitizing
  plant oils & butters, waxes, starches, humectants, peptides, most polymers,
  inert minerals and mineral/CI colorants.

## Sourcing note (network policy)

The environment's egress policy **blocks** direct fetches to `incidecoder.com`,
`sephora.com`, `ulta.com`, `paulaschoice.com`, etc. (403 at the proxy). Use
**WebSearch**, whose results surface those same pages' content. Don't try to
route around the policy. INCI ingredient *order* doesn't affect tier scoring —
only display — so an alphabetized list is still usable if label order is
unavailable (note it when you do).

## Previewing UI changes

Serve locally and screenshot with the pre-installed Chromium:
```
python3 -m http.server 8731 --bind 127.0.0.1 &
# drive with playwright-core, executablePath:
#   /opt/pw-browsers/chromium-*/chrome-linux/chrome  (args: --no-sandbox)
```
Check at iPhone (~390px) and desktop (~1280px) widths. The embedded Google Form
iframe must stay responsive (class `gform`: width 100%, fluid height).

## Git workflow

- Work on branch `claude/beauty-db-product-data-ozavra`.
- Commit identity must be `Claude <noreply@anthropic.com>` or commits show as
  Unverified. If needed: `git config user.email noreply@anthropic.com &&
  git config user.name Claude`.
- Standing instruction: **commit and merge to `main` directly** once a change is
  verified (no draft-and-wait).
- PRs are squash-merged, which orphans the old branch tip. Before the next push,
  the local branch is reset onto latest `main` (`git checkout -B …`), so the
  remote branch will reject a normal push. **Don't force-push** (it's blocked).
  Instead `git rebase origin/claude/beauty-db-product-data-ozavra` (its tree is
  identical to `main`) and push fast-forward.
- The repo has **no CI**; there are no checks to wait on.
