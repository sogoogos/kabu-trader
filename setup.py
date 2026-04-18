from setuptools import setup, find_packages

setup(
    name="kabu-trader",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "yfinance>=0.2.31",
        "pandas>=2.0.0",
        "numpy>=1.24.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "kabu-trader=kabu_trader.cli:main",
        ],
    },
)
