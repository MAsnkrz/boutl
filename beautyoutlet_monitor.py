"""
Beauty Outlet Monitor
Monitors https://www.beautyoutlet.co.uk/ for the brands:
  Revolution, Maybelline, L'Oreal, Rimmel

Uses Shopify's public collection JSON API (no auth needed):
  https://www.beautyoutlet.co.uk/collections/{handle}/products.json

In-stock status comes from Shopify's `available` flag on each variant.
Exact stock counts are not available on this storefront — cart-probing
was tried and removed, as it was too slow (thousands of cart API calls
per run) and caused snapshot corruption from workflow timeouts.

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Back in stock (was OOS, now available)

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
    # instead of True/False on some/all variants. We treat `True` as
    # definitely in stock, and treat `None`/`False` as "assume in stock
    # unless explicitly False" to avoid false negatives, since we have
    # no reliable way to get exact stock counts on this storefront
    # (cart-probing was removed — too slow/fragile across the full catalogue).
    available_variants = [v for v in variants if v.get("available")]
    variant = available_variants[0] if available_variants else (variants[0] if variants else {})

    price         = variant.get("price", "")
    compare_price = variant.get("compare_at_price", "")
    sku           = variant.get("sku", "")

    if variants:
        if any(v.get("available") is True for v in variants):
            in_stock = True
        elif all(v.get("available") is False for v in variants):
            in_stock = False
        else:
            # All None / mixed unknown — assume in stock rather than
            # risk false "out of stock" / false "back in stock" spam
            in_stock = True
    else:
        in_stock = True

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
        "in_stock":   in_stock,
        "stock":      None,   # exact stock not available on this storefront
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
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot file is corrupted ({e}) — backing it up and starting fresh.")
            try:
                backup_name = f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}"
                os.rename(SNAPSHOT_FILE, backup_name)
                print(f"  [!] Corrupted file saved as {backup_name}")
            except OSError as backup_err:
                print(f"  [!] Could not back up corrupted file: {backup_err}")
            return {}
    return {}


def save_snapshot(data):
    """Write atomically — write to a temp file then rename, so a crash
    mid-write never leaves a corrupted snapshot.json behind."""
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, SNAPSHOT_FILE)


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
    else:
        print(f"  {len(all_products)} products fetched, {len(new_ids)} new")

    for i, product in enumerate(all_products, 1):
        pid = product["id"]
        # in_stock is already set in parse_product from the Shopify `available`
        # flag — no cart-probing (too slow/fragile across the full catalogue,
        # and exact stock counts aren't available on this storefront anyway).

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

        if i % 25 == 0:
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
