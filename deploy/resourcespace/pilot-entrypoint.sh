#!/bin/bash
set -euo pipefail

# Persist only the installer-owned configuration, not the entire PHP include tree.
install -d -o www-data -g www-data -m 0750 /frameart-config
if [[ ! -e /frameart-config/config.php ]]; then
    install -o www-data -g www-data -m 0640 /dev/null /frameart-config/config.php
fi
ln -sfn /frameart-config/config.php /var/www/html/include/config.php

# setup.php writes its configuration on first installation. Restart afterwards
# to add the safety policy. No StaticSync job is installed in either phase.
if [[ -s /frameart-config/config.php ]] && \
   ! grep -Fq "require '/frameart/safety.php';" /frameart-config/config.php; then
    printf '\nrequire '\''/frameart/safety.php'\'';\n' >> /frameart-config/config.php
fi
chown www-data:www-data /frameart-config/config.php
chmod 0640 /frameart-config/config.php
exec /entrypoint.sh
