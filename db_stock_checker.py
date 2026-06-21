#!/usr/bin/env python3
"""
Dragon Ball Fusion World / Masters Stock Checker
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Checks 8 stores every time it runs and emails iconciatori@gmail.com
when any Fusion World or Masters product transitions from out-of-stock to in-stock.
Each alert includes the product name, price, stock quantity, and pre-order status.

SETUP (one-time)
─────────────────
1. Install dependencies:
       pip3 install requests beautifulsoup4 lxml

2. Create a Gmail App Password:
       • Go to myaccount.google.com → Security → 2-Step Verification (enable if needed)
       • Then: myaccount.google.com → Security → App passwords
       • Name it "DB Stock Checker", copy the 16-char password

3. Run the install script which handles everything else:
       bash install.sh

Or run once manually:
       python3 db_stock_checker.py
"""

import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ── Configuration ────────────────────────────────────────────────────────────

EMAIL_TO = "iconciatori@gmail.com"
EMAIL_FROM = "iconciatori@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "stock_state.json"

# A product title must contain "dragon ball" + at least one of these
RELEVANT_KEYWORDS = ["fusion world", "masters"]

# Search queries sent to each store
SEARCH_TERMS = ["dragon ball fusion world", "dragon ball masters"]

# Title substrings that indicate a pre-order
PREORDER_PHRASES = ["pre-order", "pre order", "presale", "pre sale", "[pre-order]", "(pre-order)", "expected release"]

REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Site Definitions ─────────────────────────────────────────────────────────

SHOPIFY_SITES = [
    ("Hobbiesville",           "https://hobbiesville.com"),
    ("Double Infinity Gaming", "https://doubleinfinitygaming.com"),
    ("Holo Horse Games",       "https://www.holohorsegames.com"),
    ("Spieda Games",           "https://spiedagames.com"),
]

HTML_SITES = [
    {
        "name": "Forge and Fire Gaming",
        "search_url": "https://forgeandfiregaming.com/search?q={query}&type=product",
        "oos_phrases": ["sold out", "out of stock"],
    },
    {
        "name": "ToyWiz",
        "search_url": "https://toywiz.com/search.php?search_query={query}",
        "oos_phrases": ["out of stock", "sold out", "unavailable"],
    },
    {
        "name": "Game Nerdz",
        "search_url": "https://www.gamenerdz.com/search.php?search_query={query}",
        "oos_phrases": ["out of stock", "sold out"],
    },
    {
        "name": "Better Loot",
        "search_url": "https://www.better-loot.com/search?q={query}&type=product",
        "oos_phrases": ["sold out", "out of stock"],
    },
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_relevant(title: str) -> bool:
    t = title.lower()
    return "dragon ball" in t and any(kw in t for kw in RELEVANT_KEYWORDS)


def detect_preorder(title: str) -> bool:
    t = title.lower()
    return any(phrase in t for phrase in PREORDER_PHRASES)


def parse_price(raw) -> str | None:
    """Normalise a price value to a '$X.XX' string, or None."""
    if raw is None:
        return None
    s = str(raw).strip().lstrip("$").replace(",", "")
    try:
        return f"${float(s):.2f}"
    except ValueError:
        return None


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── Shopify JSON API ─────────────────────────────────────────────────────────

def _shopify_get(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = requests.get(
            url, params=params,
            headers={**HEADERS, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  ⚠ Request error ({url}): {e}")
        return None


def shopify_suggest(base_url: str, term: str) -> list[dict]:
    """Predictive Search API — returns basic product info including price."""
    data = _shopify_get(
        f"{base_url}/search/suggest.json",
        params={"q": term, "resources[type]": "product", "resources[limit]": "50"},
    )
    if not data:
        return []
    return data.get("resources", {}).get("results", {}).get("products", [])


def shopify_product_detail(base_url: str, handle: str) -> dict | None:
    """Fetch full product JSON to get per-variant inventory quantities."""
    data = _shopify_get(f"{base_url}/products/{handle}.json")
    return data.get("product") if data else None


def check_shopify(name: str, base_url: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for p in shopify_suggest(base_url, term):
            title = p.get("title", "")
            if not is_relevant(title):
                continue

            handle = p.get("handle") or p.get("url", "").rsplit("/", 1)[-1]
            url = p.get("url", "")
            if url and not url.startswith("http"):
                url = base_url + url

            available = bool(p.get("available", False))

            # Price from suggest response
            price = parse_price(p.get("price") or p.get("price_min"))

            found[handle] = {
                "title": title,
                "url": url,
                "available": available,
                "price": price,
                "quantity": None,        # filled in below for in-stock items
                "is_preorder": detect_preorder(title),
                "site": name,
                "base_url": base_url,
                "handle": handle,
            }

    # For in-stock items, fetch full product detail to get stock quantity
    for key, product in found.items():
        if product["available"]:
            detail = shopify_product_detail(base_url, product["handle"])
            if detail:
                # Sum inventory across all available variants
                qty = sum(
                    v.get("inventory_quantity", 0)
                    for v in detail.get("variants", [])
                    if v.get("available")
                )
                product["quantity"] = qty if qty > 0 else None
                # Prefer price from detail if missing
                if not product["price"] and detail.get("variants"):
                    product["price"] = parse_price(detail["variants"][0].get("price"))

    # Clean up internal fields before returning
    for p in found.values():
        p.pop("base_url", None)
        p.pop("handle", None)

    return found


# ── HTML / BigCommerce parsing ───────────────────────────────────────────────

_CARD_CLASSES = {"product", "item", "card", "result", "listing", "grid"}
_PRICE_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d{1,2})?")


def _looks_like_product_card(tag) -> bool:
    cls = " ".join(tag.get("class", [])).lower()
    return any(c in cls for c in _CARD_CLASSES)


def _extract_price_from_text(text: str) -> str | None:
    m = _PRICE_RE.search(text)
    return m.group(0).replace(" ", "") if m else None


def check_html_site(site: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        url = site["search_url"].format(query=quote_plus(term))
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            log(f"  ⚠ HTTP error for {site['name']}: {e}")
            continue

        soup = BeautifulSoup(r.text, "lxml")
        oos_phrases = site["oos_phrases"]

        cards = [t for t in soup.find_all(["article", "li", "div"]) if _looks_like_product_card(t)]
        if not cards:
            cards = soup.find_all(["article", "section"])

        for card in cards:
            heading = card.find(["h2", "h3", "h4", "h5"])
            if not heading:
                el = card.find(class_=lambda c: c and "title" in c.lower() if c else False)
                heading = el
            if not heading:
                continue

            title = heading.get_text(strip=True)
            if not is_relevant(title):
                continue

            link = card.find("a", href=True)
            product_url = ""
            if link:
                href = link["href"]
                product_url = href if href.startswith("http") else urljoin(url, href)

            card_text = card.get_text(separator=" ")
            out_of_stock = any(p in card_text.lower() for p in oos_phrases)
            price = _extract_price_from_text(card_text)

            key = product_url or title
            found[key] = {
                "title": title,
                "url": product_url,
                "available": not out_of_stock,
                "price": price,
                "quantity": None,    # not typically shown on listing pages
                "is_preorder": detect_preorder(title),
                "site": site["name"],
            }

    return found


# ── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email Alert ──────────────────────────────────────────────────────────────

def _item_row_html(item: dict) -> str:
    preorder_badge = (
        '<span style="font-size:11px;background:#FFF3E0;color:#E65100;padding:2px 8px;'
        'border-radius:6px;font-weight:500;white-space:nowrap">Pre-order</span> '
        if item.get("is_preorder") else ""
    )

    if item.get("quantity") is not None:
        qty_label = f"In stock · {item['quantity']} left"
        qty_color = "#2E7D32" if item["quantity"] > 3 else "#C62828"
    else:
        qty_label = "Pre-order · available" if item.get("is_preorder") else "In stock · qty unconfirmed"
        qty_color = "#2E7D32"

    price_str = item.get("price") or "—"
    title_str = f"{preorder_badge}{item['title']}"

    return f"""
    <tr>
      <td style="padding:14px 16px;border-bottom:1px solid #f0f0f0;vertical-align:top">
        <div style="font-size:14px;font-weight:500;color:#111;line-height:1.4;margin-bottom:6px">{title_str}</div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
          <span style="font-size:12px;color:#888">{item['site']}</span>
          <span style="font-size:11px;background:#E8F5E9;color:{qty_color};padding:2px 8px;border-radius:6px;font-weight:500">{qty_label}</span>
        </div>
        <a href="{item['url']}" style="font-size:13px;color:#1A73E8;text-decoration:none">{item['url'][:72]}{'…' if len(item['url']) > 72 else ''}</a>
      </td>
      <td style="padding:14px 16px;border-bottom:1px solid #f0f0f0;vertical-align:top;text-align:right;white-space:nowrap">
        <span style="font-size:16px;font-weight:500;color:#111">{price_str}</span>
      </td>
    </tr>"""


def send_alert(newly_available: list[dict]):
    count = len(newly_available)
    subject = f"🟢 {count} Dragon Ball item{'s' if count > 1 else ''} back in stock"

    # ── Plain text
    lines = [f"Dragon Ball items now in stock ({count} total):\n"]
    for item in newly_available:
        tag = "[PRE-ORDER] " if item.get("is_preorder") else ""
        qty = f" · {item['quantity']} left" if item.get("quantity") else ""
        price = f" · {item['price']}" if item.get("price") else ""
        lines.append(f"• {tag}{item['title']}")
        lines.append(f"  {item['site']}{price}{qty}")
        lines.append(f"  {item['url']}\n")
    lines.append(f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    body_text = "\n".join(lines)

    # ── HTML
    rows = "".join(_item_row_html(item) for item in newly_available)
    body_html = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
             max-width:640px;margin:32px auto;padding:0 16px;color:#111;background:#fff">

  <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
    <div style="width:36px;height:36px;border-radius:50%;background:#E8F5E9;
                display:flex;align-items:center;justify-content:center;
                font-size:18px">🟢</div>
    <div>
      <div style="font-size:20px;font-weight:500">Dragon Ball back in stock</div>
      <div style="font-size:13px;color:#888">{count} item{'s' if count > 1 else ''} just became available — act fast</div>
    </div>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0"
         style="border:1px solid #e8e8e8;border-collapse:collapse;border-radius:10px;
                overflow:hidden;font-size:14px">
    <tr style="background:#fafafa">
      <th style="padding:10px 16px;text-align:left;border-bottom:1px solid #e8e8e8;
                 font-size:12px;color:#888;font-weight:500">Product</th>
      <th style="padding:10px 16px;text-align:right;border-bottom:1px solid #e8e8e8;
                 font-size:12px;color:#888;font-weight:500">Price</th>
    </tr>
    {rows}
  </table>

  <p style="font-size:12px;color:#bbb;margin-top:20px">
    Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · DB Stock Checker
  </p>

</body>
</html>"""

    if not GMAIL_APP_PASSWORD:
        log("⚠  GMAIL_APP_PASSWORD not set — printing alert instead:")
        print(body_text)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            smtp.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log(f"✅ Email sent → {EMAIL_TO} ({count} item{'s' if count > 1 else ''})")
    except smtplib.SMTPAuthenticationError:
        log("❌ Gmail auth failed — check your App Password")
    except Exception as e:
        log(f"❌ Failed to send email: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'═' * 60}")
    print(f"  Dragon Ball Stock Checker  —  {now}")
    print(f"{'═' * 60}")

    state = load_state()
    current: dict[str, dict] = {}
    newly_available: list[dict] = []

    for name, base_url in SHOPIFY_SITES:
        log(f"Checking {name} (Shopify)…")
        products = check_shopify(name, base_url)
        current[name] = products
        log(f"  → {len(products)} Dragon Ball product(s) found")

    for site in HTML_SITES:
        log(f"Checking {site['name']} (HTML)…")
        products = check_html_site(site)
        current[site["name"]] = products
        log(f"  → {len(products)} Dragon Ball product(s) found")

    print()
    log("Stock comparison:")
    for site_name, products in current.items():
        prev = state.get(site_name, {})
        for key, product in products.items():
            was = prev.get(key, {}).get("available")
            now_avail = product["available"]
            icon = "🟢" if now_avail else "🔴"
            tag = " [PRE-ORDER]" if product.get("is_preorder") else ""
            price = f" {product['price']}" if product.get("price") else ""
            short = product["title"][:50] + ("…" if len(product["title"]) > 50 else "")
            log(f"  {icon} [{site_name}]{price}{tag} {short}")

            if was is False and now_avail:
                log(f"     ✨ NEWLY IN STOCK!")
                newly_available.append(product)

    print()
    if newly_available:
        log(f"🚨 {len(newly_available)} item(s) newly in stock — sending alert…")
        send_alert(newly_available)
    else:
        log("✓ No new stock changes")

    save_state(current)
    log(f"State saved → {STATE_FILE.name}\n")


if __name__ == "__main__":
    main()
