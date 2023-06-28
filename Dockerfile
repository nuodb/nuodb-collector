FROM python:3.9.16-slim-bullseye

COPY  --chown=1000:0 nuocd /opt/nuocd
WORKDIR /opt/nuocd
COPY --chown=1000:0 requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update -y && \
    apt-get install -y gettext-base apt-transport-https curl gnupg procps && \
    # influxdata-archive_compat.key GPG Fingerprint: 9D539D90D3328DC7D6C8D3B9D8FF8E1F7DF8B07E
    curl -s https://repos.influxdata.com/influxdata-archive_compat.key > influxdata-archive_compat.key && \
    echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c && cat influxdata-archive_compat.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null && \
    echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list && \
    apt-get update -y && \
    useradd -d /opt/nuocd -g 0 -u 1000 -s /bin/false telegraf && \
    apt-get -y install telegraf && \
    apt-get clean

RUN chmod g+w /etc
COPY --chown=telegraf:0 conf/telegraf.conf  /etc/telegraf/telegraf.conf
COPY --chown=telegraf:0 conf/nuodb.conf     /etc/telegraf/telegraf.d/static/nuodb.conf
COPY --chown=telegraf:0 conf/outputs.conf   /etc/telegraf/telegraf.d/dynamic/outputs.conf

COPY --chown=telegraf:0 bin/nuocd           /usr/local/bin/nuocd

USER 1000:0

CMD ["telegraf", "--config", "/etc/telegraf/telegraf.conf", "--config-directory", "/etc/telegraf/telegraf.d"]

