#!/bin/bash

python3 scanning.py --window 5m --groups binance_spot,binance_perp,okx_perp,bybit_perp --quotes USDT \
 --exclude-stable-base --order-size 100 --no-debug --top 10 --filter-mode soft
