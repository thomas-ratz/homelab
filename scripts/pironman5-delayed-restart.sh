#!/bin/bash
# Delayed restart of pironman5 service after boot
# Waits for system to settle after RCU stalls

DELAY=60  # seconds to wait after boot

echo "$(date): Waiting ${DELAY}s for system to settle..."
sleep $DELAY

echo "$(date): Restarting pironman5 service..."
systemctl restart pironman5

echo "$(date): pironman5 restart complete"
