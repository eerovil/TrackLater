FROM node:20-bullseye-slim AS codex_cli

RUN npm install -g @openai/codex

FROM ubuntu:20.04

RUN apt-get update && apt-get -y install \
        software-properties-common \
  && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get -y install --reinstall \
        ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN arch=$(arch | sed s/aarch64/arm64/ | sed s/x86_64/amd64/); \
    echo "arch: $arch"; \
    if [ "$arch" = "arm64" ]; then echo "libz1" > archpackages.txt; else echo "lib32z1-dev" > archpackages.txt; fi;

RUN sed -i 's|http://archive.ubuntu.com|ftp://mirrors.nic.funet.fi|g' /etc/apt/sources.list
RUN apt-get update && apt-get -y install \
        gettext \
        build-essential \
        curl \
        git \
        libmemcached-dev \
        libmysqlclient-dev \
        subversion \
        libxml2 \
        libxml2-dev \
        libxslt1.1 \
        libxslt1-dev \
        libssl-dev \
        libffi-dev \
        vim \
        software-properties-common \
        python3-mysqldb \
        python3-crypto \
        python3-dev \
        python3-pip \
        python3 \
        $(cat archpackages.txt) \
  && rm -rf /var/lib/apt/lists/*

RUN mkdir /code
WORKDIR /code
COPY ./requirements.txt /code/
RUN pip3 install -r requirements.txt

# Node + Codex CLI (used by the AI-backed local populate, engine="codex").
# Copying the official Node image's /usr/local tree avoids relying on Ubuntu's
# obsolete Node package or the NodeSource apt repository during this build.
# Auth comes from the host Codex auth cache mounted in docker-compose.
COPY --from=codex_cli /usr/local/ /usr/local/
RUN mkdir -p /root/.codex

RUN mkdir /root/.ssh && ln -s /root/.ssh-mount/id_rsa /root/.ssh/id_rsa && chown -R root:root /root/.ssh

CMD FLASK_APP=tracklater FLASK_DEBUG=1 flask run --host=0.0.0.0  --port=5000
