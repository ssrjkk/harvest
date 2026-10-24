"""Каталог поддерживаемых тестнет-сетей для портала.

Каждая сеть: конфиг фермера, своя БД, chain_id, валюта, кран, активности
и честное описание. Портал переключает сеть и запускает прогон по кнопке.
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
        "faucet": "https://faucet.testnet.chain.robinhood.com/claim",
        "config": "config_vibevibe.yaml",
        "db": "farming_vibevibe.db",
        "tagline": "Permissionless launchpad на Robinhood Chain",
        "tagline_en": "Permissionless launchpad on Robinhood Chain",
        "description": "Любой может запустить токен и торговать им на bonding curve. "
        "Условия платформы меняются, гарантий нет — только практика.",
        "description_en": "Anyone can launch a token and trade it on the bonding curve. "
        "Platform rules change, no guarantees — practice only.",
        "rewards": "Правила платформы · без гарантий",
        "rewards_en": "Platform rules · no guarantees",
        "guide": [
            "Пополни ETH с крана Robinhood",
            "Жми «Запустить» — бот торгует и стейкает",
            "Скачай кошельки с ключами",
        ],
        "activities": [
            "Покупка/продажа на bonding curve (buy/sell)",
            "Стейкинг и клейм наград (stake/claimRewards)",
            "Трансферы между кошельками",
        ],
        "activities_en": [
            "Bonding-curve buy/sell",
            "Staking and reward claims (stake/claimRewards)",
            "Wallet-to-wallet transfers",
        ],
        "note": "Запущена платформой · условия меняются",
    },
    {
        "slug": "robinhood",
        "name": "Robinhood Chain",
        "chain_id": 46630,
        "currency": "ETH",
        "explorer": "https://explorer.testnet.chain.robinhood.com",
        "faucet": "https://faucet.testnet.chain.robinhood.com/claim",
        "config": "config_robinhood.yaml",
        "db": "farming_robinhood.db",
        "tagline": "Базовый L2-тестнет Robinhood",
        "tagline_en": "Base Robinhood L2 testnet",
        "description": "Arbitrum Orbit L2 от Robinhood. Базовые активности: трансферы, "
        "взаимодействие с контрактами (swap/mint), стейкинг.",
        "description_en": "Robinhood Arbitrum Orbit L2. Basics: transfers, "
        "contract calls (swap/mint), staking.",
        "rewards": "Публичных гарантий нет · сеть для практики",
        "rewards_en": "No public guarantees · practice network",
        "guide": ["Пополни ETH с крана", "Жми «Запустить»", "Следи за метриками"],
        "activities": [
            "Трансферы между кошельками",
            "Торговля на bonding curve (swap/mint)",
            "Стейкинг / клейм наград",
        ],
        "activities_en": [
            "Wallet transfers",
            "Bonding-curve trading (swap/mint)",
            "Staking / reward claims",
        ],
        "note": "Базовая сеть стенда",
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
        "tagline_en": "Flop Labs launchpad",
        "description": "Платформа запуска токенов. Активности: compute, validate, stake — "
        "отрабатываются контрактными вызовами.",
        "description_en": "Token launch platform. Activities: compute, validate, stake "
        "via contract calls.",
        "rewards": "Заглушка · наград не обещаем",
        "rewards_en": "Stub · no rewards promised",
        "guide": ["Впиши реальный chain_id и RPC", "Пополни токены", "Жми «Запустить»"],
        "activities": [
            "Compute / validate / stake",
            "Трансферы",
        ],
        "activities_en": [
            "Compute / validate / stake",
            "Transfers",
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
        "tagline_en": "Minara.Fun launchpad",
        "description": "Платформа с запуском токенов, торговлей и ликвидностью. "
        "Активности: launch, trade, add_liquidity.",
        "description_en": "Token launches, trading and liquidity. "
        "Activities: launch, trade, add_liquidity.",
        "rewards": "Правила платформы · без гарантий",
        "rewards_en": "Platform rules · no guarantees",
        "guide": ["Пополни ARC", "Жми «Запустить»", "Мониторь метрики"],
        "activities": [
            "Запуск / торговля / добавление ликвидности",
            "Трансферы",
        ],
        "activities_en": [
            "Launch / trade / add liquidity",
            "Transfers",
        ],
        "note": "Launchpad-активности",
    },
]


def network_by_slug(slug: str) -> dict | None:
    for n in NETWORKS:
        if n["slug"] == slug:
            return n
    return None
