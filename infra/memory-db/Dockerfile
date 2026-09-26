# Postgres 16 + Apache AGE (граф/openCypher) + pgvector (вектора) в одном образе.
# База — официальный образ Apache AGE (Postgres 16). Поверх собираем pgvector из исходников.
FROM apache/age:release_PG16_1.6.0

ARG PGVECTOR_VERSION=v0.8.0

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
        ca-certificates \
        curl; \
    curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/${PGVECTOR_VERSION}.tar.gz" -o /tmp/pgvector.tar.gz; \
    mkdir -p /tmp/pgvector; \
    tar -xzf /tmp/pgvector.tar.gz -C /tmp/pgvector --strip-components=1; \
    cd /tmp/pgvector; \
    make OPTFLAGS=""; \
    make install; \
    cd /; \
    rm -rf /tmp/pgvector /tmp/pgvector.tar.gz; \
    apt-get purge -y --auto-remove build-essential postgresql-server-dev-16 curl; \
    rm -rf /var/lib/apt/lists/*

# Инициализация поставляется внутри образа, а не bind-mount'ом. Так первый запуск
# не зависит от POSIX-прав исходного checkout на сервере развёртывания.
COPY --chown=postgres:postgres init/ /docker-entrypoint-initdb.d/

USER postgres
