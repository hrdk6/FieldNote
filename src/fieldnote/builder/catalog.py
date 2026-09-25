"""Workspace library: ready-made starting points for common markets, industries and technologies.

A template is only a description (topic + example players per region). Choosing one runs the normal
workspace builder, so the model can add players and sources, and every source is verified before the
workspace is saved. Player lists are examples of well-known companies in each region, not rankings;
regions without a list let the model choose the players (and need an LLM key to do so).
"""

from __future__ import annotations

from dataclasses import dataclass, field

REGIONS: tuple[str, ...] = ("India", "United States", "United Kingdom", "Global")
CATEGORIES: tuple[str, ...] = (
    "Commerce & retail",
    "Mobility & travel",
    "Consumer tech",
    "Finance",
    "Software & AI",
    "Food & drink",
    "Health & wellness",
    "Media & entertainment",
    "Energy, home & telecom",
    "Education",
)


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    category: str
    topic: str
    blurb: str
    players: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def players_for(self, region: str) -> tuple[str, ...]:
        return self.players.get(region, ())

    def description(self, region: str) -> str:
        """Builder input, e.g. "Food delivery apps in India: Swiggy, Zomato, Magicpin"."""
        region = " ".join(region.split()) or "Global"
        where = "worldwide" if region.lower() in ("global", "worldwide") else f"in {region}"
        names = self.players_for("Global" if region.lower() == "worldwide" else region)
        base = f"{self.topic[:1].upper()}{self.topic[1:]} {where}"
        return f"{base}: {', '.join(names)}" if names else base


def _t(id: str, title: str, category: str, topic: str, blurb: str, **players: tuple[str, ...]) -> Template:
    keys = {"india": "India", "us": "United States", "uk": "United Kingdom", "global_": "Global"}
    return Template(id, title, category, topic, blurb, {keys[k]: v for k, v in players.items()})


TEMPLATES: tuple[Template, ...] = (
    # ---- Commerce & retail ---------------------------------------------------------------------------
    _t("quick_commerce", "Quick commerce", "Commerce & retail", "quick-commerce grocery delivery",
       "Delivery times, dark-store expansion, pricing and app complaints.",
       india=("Blinkit", "Zepto", "Swiggy Instamart", "BigBasket", "Flipkart Minutes"),
       us=("Gopuff", "Instacart", "DoorDash", "Amazon Fresh")),
    _t("food_delivery", "Food delivery apps", "Commerce & retail", "food delivery apps",
       "Fees, restaurant partnerships, delivery quality and promotions.",
       india=("Swiggy", "Zomato", "Magicpin"), us=("DoorDash", "Uber Eats", "Grubhub"),
       uk=("Deliveroo", "Just Eat", "Uber Eats"), global_=("DoorDash", "Uber Eats", "Meituan", "Delivery Hero")),
    _t("online_marketplaces", "Online marketplaces", "Commerce & retail", "online marketplaces",
       "Sales events, seller policies, delivery speed and pricing moves.",
       india=("Amazon India", "Flipkart", "Meesho", "Myntra"), us=("Amazon", "Walmart", "eBay", "Temu", "Shein"),
       uk=("Amazon UK", "eBay", "Argos", "Temu"), global_=("Amazon", "Alibaba", "Temu", "Shein")),
    _t("fashion_ecommerce", "Fashion e-commerce", "Commerce & retail", "online fashion retail",
       "Collections, discounts, returns experience and fit complaints.",
       india=("Myntra", "Ajio", "Nykaa Fashion", "Tata CLiQ"), us=("Shein", "Zara", "H&M", "Uniqlo"),
       uk=("ASOS", "Next", "Zara", "H&M"), global_=("Shein", "Zara", "H&M", "Uniqlo")),
    _t("beauty", "Beauty & personal care", "Commerce & retail", "beauty and personal care brands",
       "Launches, ingredient trends, pricing and product reviews.",
       india=("Nykaa", "Purplle", "Mamaearth", "Sugar Cosmetics"),
       us=("Sephora", "Ulta Beauty", "e.l.f. Cosmetics", "L'Oréal"),
       global_=("L'Oréal", "Estée Lauder", "Unilever", "Shiseido")),
    _t("home_furniture", "Furniture & mattresses", "Commerce & retail", "furniture and mattress brands",
       "Pricing, delivery and assembly issues, quality and warranty.",
       india=("Pepperfry", "Urban Ladder", "IKEA India", "Wakefit"), us=("Wayfair", "IKEA", "Ashley Furniture"),
       uk=("IKEA", "Dunelm", "DFS")),
    # ---- Mobility & travel ---------------------------------------------------------------------------
    _t("electric_cars", "Electric cars", "Mobility & travel", "electric cars",
       "Launches, prices, range claims versus owner reports, charging.",
       india=("Tata Motors", "Mahindra", "MG Motor", "BYD", "Hyundai"),
       us=("Tesla", "Rivian", "Ford", "General Motors", "Hyundai"), uk=("Tesla", "BYD", "MG", "Kia"),
       global_=("Tesla", "BYD", "Volkswagen", "Hyundai")),
    _t("ride_hailing", "Ride-hailing", "Mobility & travel", "ride-hailing apps",
       "Fares, driver supply, safety incidents and regulation.",
       india=("Uber", "Ola", "Rapido", "Namma Yatri"), us=("Uber", "Lyft", "Waymo"), uk=("Uber", "Bolt", "FreeNow"),
       global_=("Uber", "Didi", "Grab", "Bolt")),
    _t("airlines", "Airlines", "Mobility & travel", "airlines",
       "Fares, routes, delays, baggage and service complaints.",
       india=("IndiGo", "Air India", "Akasa Air", "SpiceJet"),
       us=("Delta", "United Airlines", "American Airlines", "Southwest"),
       uk=("British Airways", "easyJet", "Ryanair", "Virgin Atlantic")),
    _t("ev_charging", "EV charging networks", "Mobility & travel", "EV charging networks",
       "Network growth, pricing per kWh, reliability and app experience.",
       india=("Tata Power EZ Charge", "Statiq", "ChargeZone", "Jio-bp pulse"),
       us=("ChargePoint", "Tesla Supercharger", "Electrify America", "EVgo"),
       uk=("Pod Point", "Ionity", "Osprey", "InstaVolt")),
    _t("online_travel", "Online travel booking", "Mobility & travel", "online travel booking platforms",
       "Pricing, refunds, loyalty programmes and booking complaints.",
       india=("MakeMyTrip", "Cleartrip", "ixigo", "EaseMyTrip"), us=("Expedia", "Booking.com", "Airbnb", "Priceline"),
       uk=("Booking.com", "Expedia", "Airbnb", "Trainline"), global_=("Booking.com", "Expedia", "Airbnb", "Trip.com")),
    # ---- Consumer tech -------------------------------------------------------------------------------
    _t("premium_smartphones", "Premium smartphones", "Consumer tech", "premium smartphones",
       "Launches, pricing, camera and battery reviews, software updates.",
       india=("Apple iPhone", "Samsung Galaxy", "Google Pixel", "OnePlus", "Vivo"),
       us=("Apple iPhone", "Samsung Galaxy", "Google Pixel", "Motorola"),
       uk=("Apple iPhone", "Samsung Galaxy", "Google Pixel"), global_=("Apple iPhone", "Samsung Galaxy", "Google Pixel", "Xiaomi")),
    _t("wearables", "Smartwatches & wearables", "Consumer tech", "smartwatches and fitness wearables",
       "Health features, battery life, pricing and accuracy complaints.",
       india=("boAt", "Noise", "Fire-Boltt", "Apple Watch"),
       us=("Apple Watch", "Samsung Galaxy Watch", "Garmin", "Fitbit", "Whoop"),
       global_=("Apple Watch", "Samsung Galaxy Watch", "Garmin", "Fitbit")),
    _t("laptops", "Laptops", "Consumer tech", "laptops",
       "Launches, pricing, performance, battery life and build quality.",
       global_=("Apple MacBook", "Dell", "HP", "Lenovo", "ASUS"), india=("HP", "Lenovo", "Dell", "ASUS", "Apple MacBook")),
    _t("gaming_consoles", "Gaming consoles", "Consumer tech", "gaming consoles and handhelds",
       "Prices, exclusive games, subscriptions and hardware issues.",
       global_=("PlayStation", "Xbox", "Nintendo Switch", "Steam Deck")),
    _t("smart_home", "Smart home devices", "Consumer tech", "smart speakers and smart home devices",
       "Assistant features, privacy concerns, pricing and compatibility.",
       global_=("Amazon Echo", "Google Nest", "Apple HomePod", "Philips Hue")),
    # ---- Finance -------------------------------------------------------------------------------------
    _t("digital_payments", "Digital payments", "Finance", "digital payments apps",
       "Transaction growth, fees, outages, fraud and rewards.",
       india=("PhonePe", "Google Pay", "Paytm", "CRED"), us=("PayPal", "Venmo", "Cash App", "Zelle", "Apple Pay"),
       uk=("PayPal", "Revolut", "Wise", "Apple Pay"), global_=("PayPal", "Wise", "Revolut", "Alipay")),
    _t("digital_banks", "Digital banks", "Finance", "digital banks",
       "Customer growth, fees, account freezes and app reviews.",
       uk=("Monzo", "Starling", "Revolut", "Chase UK"), us=("Chime", "SoFi", "Varo", "Current"),
       global_=("Revolut", "Nubank", "N26", "Monzo")),
    _t("stock_trading_apps", "Stock trading apps", "Finance", "stock trading apps",
       "Pricing, outages, new products, regulation and user complaints.",
       india=("Zerodha", "Groww", "Upstox", "Angel One"), us=("Robinhood", "Charles Schwab", "Fidelity", "Webull"),
       uk=("Trading 212", "Freetrade", "Hargreaves Lansdown", "eToro")),
    _t("bnpl", "Buy now pay later", "Finance", "buy now pay later services",
       "Regulation, fees and late charges, merchant partnerships.",
       us=("Affirm", "Klarna", "Afterpay"), uk=("Klarna", "Clearpay", "PayPal Pay in 3"),
       india=("Simpl", "LazyPay", "Amazon Pay Later")),
    _t("crypto_exchanges", "Crypto exchanges", "Finance", "cryptocurrency exchanges",
       "Listings, fees, security incidents and regulation.",
       india=("CoinDCX", "CoinSwitch", "Mudrex"), us=("Coinbase", "Kraken", "Crypto.com"),
       global_=("Binance", "Coinbase", "OKX", "Kraken")),
    _t("insurance_apps", "Insurance apps", "Finance", "online insurance providers",
       "Premiums, claim settlement experience and new products.",
       india=("Policybazaar", "Acko", "Digit Insurance", "InsuranceDekho"), us=("Lemonade", "Root", "Hippo", "Progressive")),
    # ---- Software & AI -------------------------------------------------------------------------------
    _t("generative_ai", "Generative AI assistants", "Software & AI", "generative AI assistants and chatbots",
       "Model launches, pricing, enterprise adoption and user reviews (players chosen by the model)."),
    _t("ai_coding", "AI coding assistants", "Software & AI", "AI coding assistants",
       "Features, pricing, developer sentiment and enterprise deals.",
       global_=("GitHub Copilot", "Cursor", "Windsurf", "Tabnine", "Replit")),
    _t("crm_software", "CRM software", "Software & AI", "CRM software",
       "Product launches, AI features, pricing and customer reviews.",
       global_=("Salesforce", "HubSpot", "Zoho CRM", "Microsoft Dynamics 365", "Pipedrive")),
    _t("cloud_platforms", "Cloud platforms", "Software & AI", "public cloud platforms",
       "Outages, pricing changes, AI infrastructure and region launches.",
       global_=("AWS", "Microsoft Azure", "Google Cloud", "Oracle Cloud")),
    _t("cybersecurity", "Cybersecurity", "Software & AI", "cybersecurity platforms",
       "Breaches, product launches, acquisitions and analyst coverage.",
       global_=("CrowdStrike", "Palo Alto Networks", "Zscaler", "Fortinet", "SentinelOne")),
    _t("work_management", "Work management tools", "Software & AI", "work management and collaboration tools",
       "AI features, pricing, integrations and user complaints.",
       global_=("Asana", "Monday.com", "Jira", "Notion", "ClickUp")),
    _t("hr_payroll", "HR & payroll software", "Software & AI", "HR and payroll software",
       "Compliance updates, pricing, integrations and reviews.",
       india=("Keka", "Zoho People", "greytHR", "Darwinbox"), us=("Workday", "ADP", "Gusto", "Rippling")),
    # ---- Food & drink --------------------------------------------------------------------------------
    _t("coffee_chains", "Coffee chains", "Food & drink", "coffee chains",
       "Store openings, menu launches, pricing and service reviews.",
       india=("Tata Starbucks", "Blue Tokai", "Third Wave Coffee", "Café Coffee Day"),
       us=("Starbucks", "Dunkin'", "Dutch Bros", "Peet's Coffee"), uk=("Costa Coffee", "Starbucks", "Caffè Nero", "Pret A Manger")),
    _t("fast_food", "Quick-service restaurants", "Food & drink", "quick-service restaurant chains",
       "Menu launches, value deals, delivery and hygiene complaints.",
       india=("McDonald's", "Domino's", "KFC", "Burger King", "Subway"),
       us=("McDonald's", "Chick-fil-A", "Taco Bell", "Wendy's", "Burger King")),
    _t("soft_drinks", "Soft & energy drinks", "Food & drink", "soft drinks and energy drinks",
       "Launches, pricing, marketing campaigns and health regulation.",
       india=("Coca-Cola", "PepsiCo", "Campa Cola", "Red Bull"), global_=("Coca-Cola", "PepsiCo", "Red Bull", "Monster Energy")),
    # ---- Health & wellness ---------------------------------------------------------------------------
    _t("online_pharmacy", "Online pharmacies", "Health & wellness", "online pharmacies",
       "Delivery times, discounts, regulation and service complaints.",
       india=("Tata 1mg", "PharmEasy", "Apollo 24|7", "Netmeds"), us=("CVS", "Walgreens", "Amazon Pharmacy", "GoodRx")),
    _t("fitness_apps", "Fitness apps", "Health & wellness", "fitness and nutrition apps",
       "Features, subscription pricing, community and user reviews.",
       india=("cult.fit", "HealthifyMe", "Strava"), global_=("Strava", "Peloton", "MyFitnessPal", "Fitbit")),
    _t("telehealth", "Telehealth", "Health & wellness", "telehealth services",
       "Consultation pricing, prescriptions, regulation and patient reviews.",
       us=("Teladoc", "Hims & Hers", "Ro", "Amwell"), india=("Practo", "Tata 1mg", "Apollo 24|7")),
    # ---- Media & entertainment -----------------------------------------------------------------------
    _t("video_streaming", "Video streaming", "Media & entertainment", "video streaming services",
       "Price changes, content slate, sports rights and subscriber sentiment.",
       india=("JioHotstar", "Netflix", "Amazon Prime Video", "SonyLIV", "ZEE5"),
       us=("Netflix", "Disney+", "HBO Max", "Hulu", "Peacock", "Paramount+"),
       uk=("Netflix", "BBC iPlayer", "Disney+", "ITVX", "Prime Video")),
    _t("music_streaming", "Music streaming", "Media & entertainment", "music streaming services",
       "Pricing, catalogue and artist payouts, features and churn.",
       india=("Spotify", "JioSaavn", "Gaana", "YouTube Music"),
       global_=("Spotify", "Apple Music", "YouTube Music", "Amazon Music")),
    # ---- Energy, home & telecom ---------------------------------------------------------------------
    _t("rooftop_solar", "Rooftop solar", "Energy, home & telecom", "rooftop solar installers and panel brands",
       "Subsidies, pricing per kW, installation quality and service.",
       india=("Tata Power Solar", "Adani Solar", "Waaree", "Loom Solar"), us=("Sunrun", "Tesla Energy", "Enphase")),
    _t("home_energy", "Home energy suppliers", "Energy, home & telecom", "home energy suppliers",
       "Tariffs, price caps, smart meters and customer service.",
       uk=("Octopus Energy", "British Gas", "EDF", "E.ON Next", "OVO")),
    _t("property_portals", "Property portals", "Energy, home & telecom", "property listing portals",
       "Listing volumes, pricing trends, fees and user complaints.",
       india=("99acres", "MagicBricks", "Housing.com", "NoBroker"), us=("Zillow", "Redfin", "Realtor.com"),
       uk=("Rightmove", "Zoopla", "OnTheMarket")),
    _t("home_services", "Home services apps", "Energy, home & telecom", "home services marketplaces",
       "Pricing, worker supply, service quality and complaints.",
       india=("Urban Company", "NoBroker", "Housejoy"), us=("Angi", "Thumbtack", "TaskRabbit")),
    _t("telecom", "Mobile networks", "Energy, home & telecom", "mobile network operators",
       "Tariff changes, 5G rollout, coverage and outage complaints.",
       india=("Jio", "Airtel", "Vodafone Idea", "BSNL"), us=("Verizon", "AT&T", "T-Mobile"),
       uk=("EE", "Vodafone", "O2", "Three")),
    # ---- Education -----------------------------------------------------------------------------------
    _t("edtech", "Online learning", "Education", "online learning platforms",
       "Course launches, pricing, outcomes and learner reviews.",
       india=("Physics Wallah", "BYJU'S", "Unacademy", "upGrad"), global_=("Coursera", "Udemy", "Khan Academy", "edX")),
    _t("language_learning", "Language learning apps", "Education", "language learning apps",
       "Features, AI tutors, subscription pricing and reviews.",
       global_=("Duolingo", "Babbel", "Busuu", "Rosetta Stone")),
)  # fmt: skip

_BY_ID = {t.id: t for t in TEMPLATES}


def get_template(template_id: str) -> Template:
    try:
        return _BY_ID[template_id]
    except KeyError as exc:
        raise KeyError(f"unknown template {template_id!r}; see `fieldnote workspace templates`") from exc


def search(query: str = "", category: str | None = None) -> list[Template]:
    """Templates matching a free-text query (title, topic, blurb, players) and optional category."""
    words = [w for w in query.lower().split() if w]
    out = []
    for t in TEMPLATES:
        if category and t.category != category:
            continue
        hay = " ".join([t.title, t.topic, t.blurb, t.category, *(p for ps in t.players.values() for p in ps)]).lower()
        if all(w in hay for w in words):
            out.append(t)
    return out
