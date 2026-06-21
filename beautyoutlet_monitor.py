"""
Beauty Outlet Monitor
Monitors https://www.beautyoutlet.co.uk/ for the brands:
  Revolution, Maybelline, L'Oreal, Rimmel

Uses Shopify's public collection JSON API (no auth needed):
  https://www.beautyoutlet.co.uk/collections/{handle}/products.json

Exact stock is probed via Shopify's cart API: attempting to add 501 units
to the cart causes Shopify to silently cap the quantity at whatever is
actually in stock (per Shopify's own docs). We read back the capped amount,
then remove the line again. Used selectively (new listings, back-in-stock
transitions) rather than on every product every run to stay efficient.

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Back in stock (was OOS, now available) — with exact stock count

Does NOT alert on: price increases, going OOS.

Deps: pip install requests
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.beautyoutlet.co.uk"
SNAPSHOT_FILE  = "snapshot_beautyoutlet.json"
BASELINE_FLAG  = "baseline_done_beautyoutlet.txt"
REQUEST_DELAY  = 1.5
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Target brand collections — Shopify collection handles
BRAND_COLLECTIONS = {
    "Revolution": "revolution",
    "Maybelline": "maybelline",
    "L'Oreal":    "loreal",
    "Rimmel":     "rimmel",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Discord embed colours
COLOUR_NEW     = 0xE91E8C   # pink — new listing
COLOUR_RESTOCK = 0x3498DB   # blue — restock
COLOUR_BACK    = 0x9B59B6   # purple — back in stock
# Price drop colours are tiered by severity — see notify_price_change()

# ---------------------------------------------------------------------------
# SHOPIFY API
# ---------------------------------------------------------------------------

def fetch_collection_page(handle, page, limit=250, retries=3):
    """Fetch one page of a Shopify collection's products.json."""
    url = f"{BASE_URL}/collections/{handle}/products.json"
    params = {"limit": limit, "page": page}
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            return data.get("products", [])
        except Exception as e:
            print(f"  [!] Fetch error ({handle} page {page}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    return []


def fetch_all_brand_products(handle, brand_name):
    """Paginate through every product in a brand collection."""
    all_products = []
    page = 1
    while True:
        items = fetch_collection_page(handle, page)
        if not items:
            break
        for item in items:
            all_products.append(parse_product(item, brand_name))
        print(f"    {brand_name} page {page}: {len(items)} products (total: {len(all_products)})")
        if len(items) < 250:
            break
        page += 1
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))
    return all_products


def parse_product(item, brand_name):
    """Parse a Shopify product JSON object into our format."""
    variants = item.get("variants", [])

    # Note: some Shopify stores (like this one) return `available: None`
    # instead of True/False on every variant, making the flag useless as
    # a gate. We keep it only as a soft hint; real stock always comes
    # from the cart-probe (get_stock_via_cart), which is unconditional.
    available_variants = [v for v in variants if v.get("available")]
    variant = available_variants[0] if available_variants else (variants[0] if variants else {})

    price         = variant.get("price", "")
    compare_price = variant.get("compare_at_price", "")
    sku           = variant.get("sku", "")

    # Soft hint only — True only if explicitly True, never trusted alone
    available_hint = any(v.get("available") is True for v in variants) if variants else False

    images = item.get("images", [])
    image  = images[0].get("src", "") if images else ""

    handle = item.get("handle", "")

    return {
        "id":         str(item.get("id", "")),
        "variant_id": str(variant.get("id", "")) if variant else "",
        "handle":     handle,
        "title":      item.get("title", ""),
        "url":        f"{BASE_URL}/products/{handle}",
        "image":      image,
        "sku":        sku or "",
        "brand":      brand_name,
        "price":      price,
        "compare_price": compare_price if compare_price and compare_price != price else "",
        "in_stock":   None,   # always determined later via cart probe
        "available_hint": available_hint,
        "stock":      None,   # filled in later via cart probe
        "vendor":     item.get("vendor", ""),
        "product_type": item.get("product_type", ""),
    }


def fetch_all_target_brands():
    """Fetch products across all 4 target brand collections."""
    all_products = []
    seen_ids = set()
    for brand_name, handle in BRAND_COLLECTIONS.items():
        print(f"  Fetching {brand_name} ({handle})...")
        products = fetch_all_brand_products(handle, brand_name)
        for p in products:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_products.append(p)
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))
    return all_products


def get_stock_via_cart(variant_id, probe_qty=501, retries=2):
    """
    Probe real stock by attempting to add `probe_qty` units to the cart.
    Shopify caps the added quantity to whatever is actually in stock
    (per Shopify's own Cart API docs: "the cart will instead add the
    maximum available quantity"). We then remove the line again so
    nothing lingers in a persistent cart.

    Returns:
      int  -> the actual stock count if it's below the probe quantity
      ">={probe_qty}-1" sentinel -> stock is at or above the cap (effectively "500+")
      None -> could not determine (request failed, OOS variant, etc.)
    """
    if not variant_id:
        return None

    add_url    = f"{BASE_URL}/cart/add.js"
    change_url = f"{BASE_URL}/cart/change.js"

    for attempt in range(retries):
        try:
            r = SESSION.post(
                add_url,
                json={"items": [{"id": int(variant_id), "quantity": probe_qty}]},
                timeout=15,
            )

            if r.status_code == 422:
                # Shopify returns 422 with a message describing the cap, e.g.
                # "Only 7 left for <product> ..." — parse the number if present.
                try:
                    err = r.json()
                    msg = err.get("message", "") or err.get("description", "")
                except Exception:
                    msg = r.text
                m = re.search(r"only\s+(\d+)\s+left", msg, re.IGNORECASE)
                if m:
                    return int(m.group(1))
                # Sold out entirely
                if "sold out" in msg.lower() or "not available" in msg.lower():
                    return 0
                return None

            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
            added_qty = None
            for it in items:
                if str(it.get("variant_id")) == str(variant_id):
                    added_qty = it.get("quantity")
                    break

            # Clean up — remove what we just added so the cart stays empty
            line_key = None
            for it in items:
                if str(it.get("variant_id")) == str(variant_id):
                    line_key = it.get("key") or it.get("id")
                    break
            if line_key:
                try:
                    SESSION.post(change_url, json={"id": line_key, "quantity": 0}, timeout=10)
                except Exception:
                    pass

            if added_qty is None:
                return None
            if added_qty >= probe_qty:
                return f">={probe_qty - 1}"
            return added_qty

        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  [!] Stock probe error (variant {variant_id}): {e}")
    return None

# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def effective_price(product):
    return product.get("price") or "0"


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def selleramp_url(barcode_or_sku, cost_price_str):
    if not barcode_or_sku:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode_or_sku}&sas_cost_price={cost_price_str}"
    )

# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    sku      = product.get("sku", "")
    brand    = product.get("brand", "")
    in_stock = product.get("in_stock", True)
    stock    = product.get("stock")
    price    = product.get("price", "")
    sas_url  = selleramp_url(sku, effective_price(product))

    if stock is not None:
        stock_val = f"**{stock}** units" if isinstance(stock, int) else f"**{stock}** units"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🏷️ Brand", "value": brand if brand else "-", "inline": True},
        {"name": "🔖 SKU",   "value": f"`{sku}`" if sku else "-", "inline": True},
        {"name": "📊 Stock", "value": stock_val, "inline": True},
    ]
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def _price_display(product):
    price   = product.get("price", "")
    compare = product.get("compare_price", "")
    if compare:
        return f"£{compare} -> **£{price}**"
    return f"**£{price}**" if price else "-"


def notify_new(product):
    fields = [
        {"name": "💰 Price", "value": _price_display(product), "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Beauty Outlet Monitor • beautyoutlet.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, pct_change):
    """
    pct_change is a fraction (e.g. 0.05 = 5% drop). Always a drop —
    price increases are no longer tracked.
    """
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour = 0x00C853
        tier   = "🔥"
    elif pct_change >= 0.10:
        colour = 0x2ECC71
        tier   = "💰"
    else:
        colour = 0x82E0AA
        tier   = "💵"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Beauty Outlet Monitor • beautyoutlet.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    fields = [
        {"name": "💰 Price", "value": _price_display(product), "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Beauty Outlet Monitor • beautyoutlet.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(data):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def snapshot_entry(product):
    return {
        "title":         product.get("title", ""),
        "url":           product.get("url", ""),
        "image":         product.get("image", ""),
        "sku":           product.get("sku", ""),
        "brand":         product.get("brand", ""),
        "price":         product.get("price", ""),
        "compare_price": product.get("compare_price", ""),
        "in_stock":      product.get("in_stock", True),
        "stock":         product.get("stock"),
        "variant_id":    product.get("variant_id", ""),
        "first_seen":    product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Only fires alerts for:
      - Back in stock (was OOS, now has stock) — takes priority
      - Price drop (decreased by more than 1% AND more than £0.02)
    No alerts for: price increases, going OOS.
    """
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    for key in ("image", "sku"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
        return

    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change > 0.01 and abs_change > 0.02:
            notify_price_change(product, old_price, new_price, pct_change)
            time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Beauty Outlet...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    all_products = fetch_all_target_brands()
    if not all_products:
        print("  [!] No products fetched")
        return

    current_ids = {p["id"] for p in all_products}
    new_ids     = current_ids - known_ids

    if is_first_run:
        print(f"  First run — building baseline from {len(all_products)} products (no alerts)...")
        print(f"  Probing exact stock for every product via cart API — this will take a while...")
    else:
        print(f"  {len(all_products)} products fetched, {len(new_ids)} new")

    for i, product in enumerate(all_products, 1):
        pid = product["id"]

        # Always probe stock via cart — this store's `available` flag returns
        # None instead of True/False, so it can't be trusted as a gate.
        if product.get("variant_id"):
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            stock = get_stock_via_cart(product["variant_id"])
            product["stock"] = stock
            if isinstance(stock, str) and stock.startswith(">="):
                product["in_stock"] = True
            elif isinstance(stock, int):
                product["in_stock"] = stock > 0
            else:
                # Probe failed — fall back to the soft hint rather than assuming OOS
                product["in_stock"] = product.get("available_hint", True)
        else:
            product["in_stock"] = product.get("available_hint", True)

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
        elif pid in new_ids:
            if product.get("in_stock", True):
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
        else:
            old = snapshot[pid]
            check_changes(product, old)
            entry = snapshot_entry(product)
            entry["first_seen"] = old.get("first_seen", entry["first_seen"])
            snapshot[pid] = entry

        if i % 50 == 0:
            save_snapshot(snapshot)
            print(f"  Auto-saved at {i}/{len(all_products)}")

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products recorded. No alerts sent.")
    else:
        print(f"  Snapshot saved ({len(snapshot)} products tracked)")


def main():
    print("=" * 55)
    print("  Beauty Outlet Monitor")
    print(f"  Brands: {', '.join(BRAND_COLLECTIONS.keys())}")
    print("  Tracking: new listings, price drops, back in stock")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
