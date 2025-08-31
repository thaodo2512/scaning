#!/bin/bash

python3 scanning.py --window 2h --groups binance_spot,binance_perp,okx_perp,bybit_perp --quotes USDT \
 --exclude-stable-base --order-size 100 --no-debug --top 10 --filter-mode soft --email-to tinhhayho@gmail.com,thaodo2512@gmail.com \
 --email-from tinh.nguyentrung1999@gmail.com \
 --snapshot-interval 2h --snapshot-top 10 --email-subject "Interest Screener Snapshot"

