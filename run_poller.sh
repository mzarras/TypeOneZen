#!/bin/zsh
# TypeOneZen Dexcom Poller - cron wrapper
export PATH="/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="$HOME/Library/Python/3.9/lib/python/site-packages"

cd ~/TypeOneZen
/usr/bin/python3 poller.py >> ~/TypeOneZen/logs/cron.log 2>&1
