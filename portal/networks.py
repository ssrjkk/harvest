"""Каталог поддерживаемых тестнет-сетей для портала.

Каждая сеть: конфиг фермера, своя БД, chain_id, валюта, кран, активности,
описание и награды. Портал переключает сеть и запускает фарм по кнопке.
"""

# Имя конфига по умолчанию для новой установки.
DEFAULT_NETWORK = "vibevibe"

NETWORKS = [
    {
        "slug": "vibevibe",
        "name": "vibe/vibe",
        "chain_id": 46630,
        "currency": "ETH",
        "explorer": "https://explorer.testnet.chain.robinhood.com",
        "faucet": "https://faucet.testnet.chain.robinhood.com",
        "config": "config_vibevibe.yaml",
        "db": "farming_vibevibe.db",
        "tagline": "Permissionless launchpad на Robinhood Chain",
        "description": "Любой может запустить токен и торговать им на bonding curve. "
        "Активным тестнет-участникам выделено 5% сапплая токена.",
        "rewards": "5% сапплая VIBE · торговля, стейкинг, клейм наград",
        "guide": [
            "Пополни ETH с крана Robinhood",
            "Жми «Фармить» — бот торгует и стейкает",
            "Скачай кошельки с ключами",
        ],
        "activities": [
            "Покупка/продажа на bonding curve (buy/sell)",
            "Стейкинг и клейм наград (stake/claimRewards)",
            "Трансферы между кошельками",
        ],
        "note": "Запущена платформой, 5% сапплая участникам",
    },
    {
        "slug": "robinhood",
        "name": "Robinhood Chain",
        "chain_id": 46630,
        "currency": "ETH",
        "explorer": "https://explorer.testnet.chain.robinhood.com",
        "faucet": "https://faucet.testnet.chain.robinhood.com",
        "config": "config_robinhood.yaml",
        "db": "farming_robinhood.db",
        "tagline": "Базовый L2-тестнет Robinhood",
        "description": "Arbitrum Orbit L2 от Robinhood. Базовые активности: трансферы, "
        "взаимодействие с контрактами (swap/mint), стейкинг.",
        "rewards": "Базовый тестнет-эирдроп и опыт",
        "guide": ["Пополни ETH с крана", "Жми «Фармить»", "Следи за метриками"],
        "activities": [
            "Трансферы между кошельками",
            "Торговля на bonding curve (swap/mint)",
            "Стейкинг / клейм наград",
        ],
        "note": "Базовая сеть фермера",
    },
    {
        "slug": "flop",
        "name": "Flop Labs",
        "chain_id": 99999,
        "currency": "FLOP",
        "explorer": "",
        "faucet": "",
        "config": "config_flop.yaml",
        "db": "farming_flop.db",
        "tagline": "Launchpad Flop Labs",
        "description": "Платформа запуска токенов. Активности: compute, validate, stake — "
        "отрабатываются контрактными вызовами.",
        "rewards": "Тестнет-баллы и эирдроп",
        "guide": ["Впиши реальный chain_id и RPC", "Пополни токены", "Жми «Фармить»"],
        "activities": [
            "Compute / validate / stake",
            "Трансферы",
        ],
        "note": "chain_id — заглушка, впишите реальный",
    },
    {
        "slug": "arc",
        "name": "Arc (Minara.Fun)",
        "chain_id": 5042002,
        "currency": "ARC",
        "explorer": "https://explorer.testnet.arc.network",
        "faucet": "",
        "config": "config_arc.yaml",
        "db": "farming_arc.db",
        "tagline": "Launchpad Minara.Fun",
        "description": "Платформа с запуском токенов, торговлей и ликвидностью. "
        "Активности: launch, trade, add_liquidity.",
        "rewards": "Тестнет-эирдроп за активности",
        "guide": ["Пополни ARC", "Жми «Фармить»", "Мониторь метрики"],
        "activities": [
            "Запуск / торговля / добавление ликвидности",
            "Трансферы",
        ],
        "note": "Launchpad-активности",
    },
]


def network_by_slug(slug: str) -> dict | None:
    for n in NETWORKS:
        if n["slug"] == slug:
            return n
    return None
