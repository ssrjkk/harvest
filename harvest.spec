# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для HARVEST — Robinhood Chain Farmer.

Сборка:  pyinstaller harvest.spec
Результат: dist/harvest/harvest.exe
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# === Входная точка ===
entry_point = 'main.py'

# === Данные (ABI, шаблон конфига) ===
# ВНИМАНИЕ: всё, что здесь перечислено, попадает в exe-дистрибутив.
# НЕ кладите секреты в конфиги — только шаблоны без токенов/ключей.
# Боевые конфиги (RPC с API-ключом, deploy_salt, пароли) НЕ бандлим:
# живой config.yaml кладётся рядом с harvest.exe (см. справку в build.bat),
# секреты выдаются через env (FARMER_RPC_URL и т.п.).
datas = [
    ('abi', 'abi'),
    ('config.example.yaml', '.'),
] + collect_data_files('eth_account')  # wordlist/*.txt для генерации мнемоник (english и др.)

# === Hidden imports (web3, aiohttp, cryptography, aiosqlite) ===
hiddenimports = [
    'web3',
    'web3.providers',
    'web3.providers.rpc',
    'web3.providers.eth_tester',
    'web3.middleware',
    'web3.middleware.geth_poa',
    'web3.contract',
    'web3.types',
    'web3._utils',
    'web3._utils.transactions',
    'web3._utils.method_formatters',
    'web3._utils.rpc_abi',
    'eth_account',
    'eth_account.account',
    'eth_account.signers',
    'eth_account.signers.local',
    'eth_account.datastructures',
    'eth_keys',
    'eth_keys.datatypes',
    'eth_utils',
    'eth_abi',
    'aiohttp',
    'aiohttp.client',
    'aiohttp.connector',
    'aiohttp.typedefs',
    'aiosqlite',
    'cryptography',
    'cryptography.hazmat',
    'cryptography.hazmat.primitives',
    'cryptography.hazmat.primitives.ciphers',
    'cryptography.hazmat.primitives.ciphers.aead',
    'cryptography.hazmat.primitives.ciphers.algorithms',
    'cryptography.hazmat.primitives.ciphers.modes',
    'cryptography.hazmat.primitives.hashes',
    'cryptography.hazmat.primitives.kdf.pbkdf2',
    'cryptography.hazmat.primitives.padding',
    'cryptography.hazmat.backends',
    'colorama',
    'rich',
    'rich.console',
    'rich.table',
    'rich.panel',
    'rich.text',
    'rich.live',
    'rich.progress',
    'rich.layout',
    'rich.tree',
    'fake_useragent',
    'yaml',
    'eth_keys',
    'rlp',
]

# Исключаем тяжёлые/ненужные модули
excludes = [
    'tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
    'IPython', 'jupyter', 'notebook', 'sphinx',
    'pytest', '_pytest',
    'setuptools',
    'pkg_resources',
]

a = Analysis(
    [entry_point],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='harvest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX вызывает ложные срабатывания AV/EDR и иногда портит exe; gzip и так внутри
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,  # смотри комментарий выше (EXE)
    upx_exclude=[],
    name='harvest',
)
